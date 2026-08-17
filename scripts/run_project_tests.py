#!/usr/bin/env python3
"""Run addon pytest with all process temp state rooted under this repository."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

sys.dont_write_bytecode = True

from project_runtime import ProjectRuntime


REPO_ROOT = Path(__file__).resolve().parents[1]


def main(argv: Sequence[str] | None = None) -> int:
    pytest_args = list(sys.argv[1:] if argv is None else argv)
    if not pytest_args:
        pytest_args = ["tests"]
    with ProjectRuntime(REPO_ROOT, "pytest") as runtime:
        base_temp = runtime.temp_dir / "pytest"
        completed = runtime.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *pytest_args,
                "--basetemp",
                str(base_temp),
                "-p",
                "no:cacheprovider",
            ],
            cwd=str(REPO_ROOT),
        )
        return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
