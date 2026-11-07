import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_offline_migration_script_emits_each_schema_once():
    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql+asyncpg://doc_user:doc_pass@localhost/hyperion_doc"
    env["SECRET_KEY"] = "offline-migration-test-only"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=BACKEND_ROOT, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    for table in ["users", "media", "detections", "video_detections"]:
        assert len(re.findall(rf"(?m)^CREATE TABLE {table} \(", result.stdout)) == 1
    assert result.stdout.count("BEGIN;") == 1
    assert result.stdout.count("COMMIT;") == 1


@pytest.mark.parametrize("port", [None, "9123"])
@pytest.mark.skipif(os.name != "posix", reason="The Docker entrypoint uses a POSIX shell")
def test_docker_command_expands_port_and_executes_server_as_main_process(tmp_path, port):
    instruction = next(
        line.removeprefix("CMD ")
        for line in (BACKEND_ROOT / "Dockerfile").read_text().splitlines()
        if line.startswith("CMD ")
    )
    command = json.loads(instruction)
    assert command[:2] == ["sh", "-c"]
    assert command[2].startswith("exec uvicorn ")

    # Execute only a local stand-in, never the application or its DB workers.
    stand_in = tmp_path / "uvicorn"
    stand_in.write_text('#!/bin/sh\nprintf "%s\\n" "$$" "$@"\n')
    stand_in.chmod(0o700)
    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env["PATH"]
    if port is None:
        env.pop("PORT", None)
    else:
        env["PORT"] = port
    with subprocess.Popen(command, env=env, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE) as process:
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        lines = stdout.splitlines()
        assert int(lines[0]) == process.pid
    assert lines[1:] == ["main:app", "--host", "0.0.0.0", "--port", port or "8000"]


def test_docker_context_excludes_local_credentials_and_virtual_environments():
    patterns = set((BACKEND_ROOT / ".dockerignore").read_text().splitlines())
    assert {".env", ".env.*", ".git/", "venv/", ".venv/"} <= patterns
