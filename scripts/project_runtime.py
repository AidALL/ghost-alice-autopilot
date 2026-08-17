#!/usr/bin/env python3
"""Keep child-process temporary state inside a unique repository run root."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


_CONTROLLED_ENV_KEYS = {"TEMP", "TMP", "TMPDIR", "PYTHONPYCACHEPREFIX"}
_CLEANUP_RETRY_DELAYS_SECONDS = (0.05, 0.1, 0.2, 0.4)
_RETRYABLE_WINDOWS_CLEANUP_ERRORS = frozenset({5, 32, 145})


def _default_reporter(message: str) -> None:
    sys.stderr.write(message + "\n")


def _safe_label(label: str) -> str:
    normalized = "".join(character if character.isalnum() else "-" for character in label).strip("-")
    return normalized or "run"


def _is_successful_system_exit(exception: BaseException | None) -> bool:
    if not isinstance(exception, SystemExit):
        return False
    code = exception.code
    return code is None or (isinstance(code, int) and code == 0)


def _is_retryable_cleanup_error(error: OSError) -> bool:
    return isinstance(error, PermissionError) or getattr(error, "winerror", None) in _RETRYABLE_WINDOWS_CLEANUP_ERRORS


class ProjectRuntime:
    """Own a repo-local temp root and child-only environment for one run."""

    def __init__(
        self,
        project_root: Path,
        label: str,
        *,
        reporter: Callable[[str], None] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self._reporter = reporter or _default_reporter
        runtime_parent = self.project_root / ".tmp" / "runtime"
        runtime_parent.mkdir(parents=True, exist_ok=True)
        self.root = Path(
            tempfile.mkdtemp(
                prefix=f"{_safe_label(label)}-",
                dir=runtime_parent,
            )
        ).resolve()
        self.temp_dir = self.directory("temp")
        self.pycache_dir = self.directory("pycache")
        self._preserve_reasons: list[str] = []
        self._closed = False

    def __enter__(self) -> "ProjectRuntime":
        return self

    def __exit__(self, exception_type, exception, traceback) -> bool:
        if exception_type is not None and not _is_successful_system_exit(exception):
            self.preserve(f"exception={exception_type.__name__}")
        self.close()
        return False

    def directory(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as error:
            raise ValueError(f"runtime directory escapes run root: {relative_path}") from error
        path.mkdir(parents=True, exist_ok=True)
        return path

    def child_environment(
        self,
        overrides: Mapping[str, str] | None = None,
        *,
        base: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        if overrides:
            environment.update({str(key): str(value) for key, value in overrides.items()})
        for key in list(environment):
            if key.upper() in _CONTROLLED_ENV_KEYS:
                environment.pop(key)
        environment.update(
            {
                "TEMP": str(self.temp_dir),
                "TMP": str(self.temp_dir),
                "TMPDIR": str(self.temp_dir),
                "PYTHONPYCACHEPREFIX": str(self.pycache_dir),
            }
        )
        return environment

    def preserve(self, reason: str) -> None:
        if reason not in self._preserve_reasons:
            self._preserve_reasons.append(reason)

    def note_returncode(self, returncode: int) -> None:
        if returncode != 0:
            self.preserve(f"returncode={returncode}")

    def run(
        self,
        command: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[Any]:
        options = dict(kwargs)
        supplied_environment = options.pop("env", None)
        options["env"] = self.child_environment(base=supplied_environment)
        try:
            completed = subprocess.run(list(command), **options)
        except subprocess.TimeoutExpired:
            self.preserve("timeout")
            raise
        except BaseException as error:
            self.preserve(f"child-exception={type(error).__name__}")
            raise
        self.note_returncode(int(completed.returncode))
        return completed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._preserve_reasons:
            reasons = ", ".join(self._preserve_reasons)
            self._reporter(f"[project-runtime] preserved temp root ({reasons}): {self.root}")
            return
        cleanup_error: OSError | None = None
        for delay in (0.0, *_CLEANUP_RETRY_DELAYS_SECONDS):
            if delay:
                time.sleep(delay)
            try:
                shutil.rmtree(self.root)
                return
            except FileNotFoundError:
                return
            except OSError as error:
                cleanup_error = error
                if not _is_retryable_cleanup_error(error):
                    break
                if not self.root.exists():
                    return
        if cleanup_error is not None:
            self._reporter(f"[project-runtime] could not clean temp root ({type(cleanup_error).__name__}): {self.root}")


__all__ = ["ProjectRuntime"]
