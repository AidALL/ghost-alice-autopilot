from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "project_runtime.py"
RUNNER = REPO_ROOT / "scripts" / "run_project_tests.py"
FRESH_INSTALL = REPO_ROOT / "scripts" / "fresh_install_e2e.py"


def _load(path: Path, name: str):
    assert path.is_file(), f"missing implementation: {path}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def test_child_environment_is_repo_local_without_mutating_parent(tmp_path: Path, monkeypatch) -> None:
    runtime_module = _load(HELPER, "project_runtime_child_env")
    external = tmp_path / "CreatorTemp"
    monkeypatch.setenv("TEMP", str(external))
    monkeypatch.setenv("TMP", str(external))
    monkeypatch.setenv("TMPDIR", str(external))
    before = dict(os.environ)

    with runtime_module.ProjectRuntime(REPO_ROOT, "child-env") as runtime:
        first = runtime.child_environment({"HARNESS_MARKER": "first"})
        second = runtime.child_environment()
        run_root = runtime.root

        assert first["HARNESS_MARKER"] == "first"
        for key in ("TEMP", "TMP", "TMPDIR"):
            assert Path(first[key]).resolve() == runtime.temp_dir.resolve()
        assert Path(first["PYTHONPYCACHEPREFIX"]).resolve() == runtime.pycache_dir.resolve()
        assert _is_within(runtime.root, REPO_ROOT / ".tmp")
        assert first["TMPDIR"] == second["TMPDIR"]
        assert dict(os.environ) == before

    assert not run_root.exists()
    assert dict(os.environ) == before


def test_runtime_roots_are_unique(tmp_path: Path) -> None:
    runtime_module = _load(HELPER, "project_runtime_unique")

    with (
        runtime_module.ProjectRuntime(tmp_path, "parallel") as first,
        runtime_module.ProjectRuntime(tmp_path, "parallel") as second,
    ):
        assert first.root != second.root
        assert first.root.is_dir()
        assert second.root.is_dir()


def test_cleanup_retries_transient_permission_error(tmp_path: Path, monkeypatch) -> None:
    runtime_module = _load(HELPER, "project_runtime_cleanup_retry")
    reports: list[str] = []
    original_rmtree = runtime_module.shutil.rmtree
    attempts = 0

    def transiently_locked(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("pycache is still locked")
        original_rmtree(path)

    monkeypatch.setattr(runtime_module.shutil, "rmtree", transiently_locked)

    with runtime_module.ProjectRuntime(tmp_path, "cleanup-retry", reporter=reports.append) as runtime:
        run_root = runtime.root

    assert attempts == 2
    assert not run_root.exists()
    assert reports == []


def test_cleanup_retries_transient_windows_directory_not_empty_error(tmp_path: Path, monkeypatch) -> None:
    runtime_module = _load(HELPER, "project_runtime_cleanup_windows_retry")
    reports: list[str] = []
    original_rmtree = runtime_module.shutil.rmtree
    attempts = 0

    def transiently_not_empty(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            error = OSError("runtime directory is not empty")
            error.winerror = 145
            raise error
        original_rmtree(path)

    monkeypatch.setattr(runtime_module.shutil, "rmtree", transiently_not_empty)

    with runtime_module.ProjectRuntime(tmp_path, "cleanup-windows-retry", reporter=reports.append) as runtime:
        run_root = runtime.root

    assert attempts == 2
    assert not run_root.exists()
    assert reports == []


def test_persistent_cleanup_permission_error_preserves_successful_system_exit(tmp_path: Path, monkeypatch) -> None:
    runtime_module = _load(HELPER, "project_runtime_cleanup_persistent")
    reports: list[str] = []
    attempts = 0
    sleeps: list[float] = []

    def persistently_locked(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError(f"still locked: {path}")

    monkeypatch.setattr(runtime_module.shutil, "rmtree", persistently_locked)
    monkeypatch.setattr(runtime_module.time, "sleep", sleeps.append)

    with pytest.raises(SystemExit) as caught:
        with runtime_module.ProjectRuntime(tmp_path, "cleanup-persistent", reporter=reports.append) as runtime:
            run_root = runtime.root
            raise SystemExit(0)

    assert caught.value.code == 0
    assert attempts == len(runtime_module._CLEANUP_RETRY_DELAYS_SECONDS) + 1
    assert sleeps == list(runtime_module._CLEANUP_RETRY_DELAYS_SECONDS)
    assert run_root.is_dir()
    assert reports == [f"[project-runtime] could not clean temp root (PermissionError): {run_root}"]


def test_unrelated_cleanup_oserror_does_not_retry_or_replace_successful_system_exit(tmp_path: Path, monkeypatch) -> None:
    runtime_module = _load(HELPER, "project_runtime_cleanup_unrelated")
    reports: list[str] = []
    attempts = 0
    sleeps: list[float] = []

    def unrelated_failure(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        error = OSError(f"unrelated cleanup failure: {path}")
        error.winerror = 123
        raise error

    monkeypatch.setattr(runtime_module.shutil, "rmtree", unrelated_failure)
    monkeypatch.setattr(runtime_module.time, "sleep", sleeps.append)

    with pytest.raises(SystemExit) as caught:
        with runtime_module.ProjectRuntime(tmp_path, "cleanup-unrelated", reporter=reports.append) as runtime:
            run_root = runtime.root
            raise SystemExit(0)

    assert caught.value.code == 0
    assert attempts == 1
    assert sleeps == []
    assert run_root.is_dir()
    assert reports == [f"[project-runtime] could not clean temp root (OSError): {run_root}"]


@pytest.mark.parametrize("code", [None, 0, False])
def test_successful_system_exit_codes_remove_runtime_root_without_report(
    tmp_path: Path,
    code: object,
) -> None:
    runtime_module = _load(HELPER, "project_runtime_system_exit_success")
    reports: list[str] = []

    with pytest.raises(SystemExit) as caught:
        with runtime_module.ProjectRuntime(
            tmp_path,
            "system-exit-success",
            reporter=reports.append,
        ) as runtime:
            run_root = runtime.root
            raise SystemExit(code)

    assert caught.value.code == code
    assert not run_root.exists()
    assert reports == []


@pytest.mark.parametrize("code", [7, True, "failure", 0.0])
def test_unsuccessful_system_exit_codes_preserve_and_report_runtime_root(
    tmp_path: Path,
    code: object,
) -> None:
    runtime_module = _load(HELPER, "project_runtime_system_exit_failure")
    reports: list[str] = []

    with pytest.raises(SystemExit) as caught:
        with runtime_module.ProjectRuntime(
            tmp_path,
            "system-exit-failure",
            reporter=reports.append,
        ) as runtime:
            run_root = runtime.root
            raise SystemExit(code)

    assert caught.value.code == code
    assert run_root.is_dir()
    assert any("exception=SystemExit" in message and str(run_root) in message for message in reports)


def test_nonzero_child_preserves_and_reports_runtime_root(tmp_path: Path, monkeypatch) -> None:
    runtime_module = _load(HELPER, "project_runtime_nonzero")
    reports: list[str] = []

    class Completed:
        returncode = 7

    monkeypatch.setattr(runtime_module.subprocess, "run", lambda *args, **kwargs: Completed())

    with runtime_module.ProjectRuntime(tmp_path, "nonzero", reporter=reports.append) as runtime:
        run_root = runtime.root
        completed = runtime.run(["example-command"])

    assert completed.returncode == 7
    assert run_root.is_dir()
    assert any("returncode=7" in message and str(run_root) in message for message in reports)


def test_timeout_preserves_and_reports_runtime_root(tmp_path: Path, monkeypatch) -> None:
    runtime_module = _load(HELPER, "project_runtime_timeout")
    reports: list[str] = []

    def time_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(runtime_module.subprocess, "run", time_out)

    with pytest.raises(subprocess.TimeoutExpired):
        with runtime_module.ProjectRuntime(tmp_path, "timeout", reporter=reports.append) as runtime:
            run_root = runtime.root
            runtime.run(["slow-command"], timeout=0.01)

    assert run_root.is_dir()
    assert any("timeout" in message and str(run_root) in message for message in reports)


def test_exception_preserves_and_reports_runtime_root(tmp_path: Path) -> None:
    runtime_module = _load(HELPER, "project_runtime_exception")
    reports: list[str] = []

    with pytest.raises(RuntimeError, match="boom"):
        with runtime_module.ProjectRuntime(tmp_path, "exception", reporter=reports.append) as runtime:
            run_root = runtime.root
            raise RuntimeError("boom")

    assert run_root.is_dir()
    assert any("exception=RuntimeError" in message and str(run_root) in message for message in reports)


def test_fresh_install_harness_propagates_project_runtime_environment(tmp_path: Path, monkeypatch) -> None:
    runtime_module = _load(HELPER, "project_runtime_fresh")
    fresh = _load(FRESH_INSTALL, "fresh_install_e2e_project_runtime")
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return Completed()

    monkeypatch.setattr(runtime_module.subprocess, "run", fake_run)

    with runtime_module.ProjectRuntime(tmp_path, "fresh-harness") as runtime:
        assert fresh.run_command(["docker", "version"], runtime=runtime) == 0
        expected_temp = runtime.temp_dir.resolve()

    child_env = captured["env"]
    assert isinstance(child_env, dict)
    assert Path(child_env["TMPDIR"]).resolve() == expected_temp
    assert Path(child_env["PYTHONPYCACHEPREFIX"]).resolve().parent == expected_temp.parent


def test_canonical_runner_gives_pytest_repo_local_tmp_path(tmp_path: Path, monkeypatch) -> None:
    assert RUNNER.is_file(), f"missing implementation: {RUNNER}"
    runtime_module = _load(HELPER, "project_runtime_runner")
    monkeypatch.setitem(sys.modules, "project_runtime", runtime_module)
    runner = _load(RUNNER, "run_project_tests_cache")
    project_root = tmp_path / "project"
    project_root.mkdir()
    report = tmp_path / "child-runtime.json"
    monkeypatch.setattr(runner, "REPO_ROOT", project_root)
    probe = tmp_path / "project_runtime_probe.py"
    probe.write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "from pathlib import Path",
                "",
                "def test_child_tmp_path(tmp_path, pytestconfig):",
                f"    repo = Path({json.dumps(str(project_root))}).resolve()",
                f"    report = Path({json.dumps(str(report))}).resolve()",
                "    project_tmp = (repo / '.tmp').resolve()",
                "    child_temp = Path(os.environ['TMPDIR']).resolve()",
                "    report.write_text(json.dumps({'runtime_root': str(child_temp.parent)}), encoding='utf-8')",
                "    assert child_temp.is_relative_to(project_tmp)",
                "    assert tmp_path.resolve().is_relative_to(child_temp)",
                "    assert Path(os.environ['TEMP']).resolve() == child_temp",
                "    assert Path(os.environ['TMP']).resolve() == child_temp",
                "    assert Path(os.environ['PYTHONPYCACHEPREFIX']).resolve().is_relative_to(project_tmp)",
                "    assert not pytestconfig.pluginmanager.hasplugin('cacheprovider')",
                "    assert not (repo / '.pytest_cache').exists()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    poisoned = dict(os.environ)
    poisoned["TEMP"] = str(tmp_path / "CreatorTemp")
    poisoned["TMP"] = str(tmp_path / "CreatorTemp")
    poisoned["TMPDIR"] = str(tmp_path / "CreatorTemp")
    for key, value in poisoned.items():
        monkeypatch.setenv(key, value)
    before = dict(os.environ)

    returncode = runner.main([str(probe), "-q"])

    assert returncode == 0
    assert dict(os.environ) == before
    assert report.is_file()
    runtime_root = Path(json.loads(report.read_text(encoding="utf-8"))["runtime_root"])
    assert not runtime_root.exists()
    assert not (project_root / ".pytest_cache").exists()


@pytest.mark.parametrize(
    ("launcher_name", "arguments"),
    [
        ("run_project_tests.py", ["tests/test_probe.py", "-q"]),
        ("fresh_install_e2e.py", ["--help"]),
        ("live_semantic_e2e.py", ["--help"]),
    ],
)
def test_direct_launcher_imports_do_not_write_scripts_pycache(tmp_path: Path, launcher_name: str, arguments: list[str]) -> None:
    project = tmp_path / Path(launcher_name).stem
    scripts_dir = project / "scripts"
    tests_dir = project / "tests"
    scripts_dir.mkdir(parents=True)
    tests_dir.mkdir()
    shutil.copy2(HELPER, scripts_dir / "project_runtime.py")
    shutil.copy2(REPO_ROOT / "scripts" / launcher_name, scripts_dir / launcher_name)
    (tests_dir / "test_probe.py").write_text("def test_probe():\n    assert True\n", encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)

    result = subprocess.run([sys.executable, str(scripts_dir / launcher_name), *arguments], cwd=project, env=environment, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)

    assert result.returncode == 0, result.stderr + result.stdout
    assert not (scripts_dir / "__pycache__").exists()
