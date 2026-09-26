"""No usage traces: only the requested report and (if enabled) the database are written."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from conftest import make_project

RUNNER = Path(__file__).resolve().parent / "trace_runner.py"
WATCHED = ("home", "cwd", "tmp", "xdg_cache", "xdg_config", "xdg_state", "xdg_data", "doc")


def snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_only_the_report_and_the_database_are_written(tmp_path):
    dirs = {name: tmp_path / name for name in WATCHED}
    for path in dirs.values():
        path.mkdir()
    paper = str(make_project(dirs["doc"]))
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(dirs["home"]),
        "TMPDIR": str(dirs["tmp"]),
        "XDG_CACHE_HOME": str(dirs["xdg_cache"]),
        "XDG_CONFIG_HOME": str(dirs["xdg_config"]),
        "XDG_STATE_HOME": str(dirs["xdg_state"]),
        "XDG_DATA_HOME": str(dirs["xdg_data"]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def run(*args: str) -> set[str]:
        before = snapshot(tmp_path)
        result = subprocess.run([sys.executable, str(RUNNER), paper, *args], cwd=dirs["cwd"], env=env,
                                capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr
        return snapshot(tmp_path) - before

    assert run() == set()
    assert run("-o", "report.md") == {"cwd/report.md"}
    created = run("--abstract-db", "store.sqlite")
    assert "cwd/store.sqlite" in created
    assert created <= {"cwd/store.sqlite", "cwd/store.sqlite-wal", "cwd/store.sqlite-shm"}
