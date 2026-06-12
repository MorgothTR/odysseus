import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

from src import bg_jobs
from src.agent_tools import TOOL_TAGS
from src.process_manager import manage_processes
from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS, ToolIndex
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.tool_security import (
    NON_ADMIN_BLOCKED_TOOLS,
    PLAN_MODE_READONLY_TOOLS,
    _PLAN_MODE_KNOWN_MUTATORS,
)


@pytest.fixture
def jobs_dir(tmp_path, monkeypatch):
    jobs = tmp_path / "bg_jobs"
    store = tmp_path / "bg_jobs.json"
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs)
    monkeypatch.setattr(bg_jobs, "_STORE", store)
    return tmp_path


def _python_service_command(marker: str, sleep_s: int = 60) -> str:
    py = Path(sys.executable).as_posix()
    return f'"{py}" -u -c "import time; print(\'{marker}\'); time.sleep({sleep_s})"'


def _wait_for(predicate, timeout_s: float = 15.0, interval_s: float = 0.25):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_manage_processes_is_registered_as_builtin_tool():
    import src.agent_loop as agent_loop

    schema_names = {schema["function"]["name"] for schema in FUNCTION_TOOL_SCHEMAS}

    assert "manage_processes" in TOOL_TAGS
    assert "manage_processes" in schema_names
    assert "manage_processes" in BUILTIN_TOOL_DESCRIPTIONS
    assert "manage_processes" in agent_loop.TOOL_SECTIONS
    assert "manage_processes" in NON_ADMIN_BLOCKED_TOOLS
    # It spawns/kills processes — must be a plan-mode mutator, never read-only.
    assert "manage_processes" in _PLAN_MODE_KNOWN_MUTATORS
    assert "manage_processes" not in PLAN_MODE_READONLY_TOOLS


def test_manage_processes_function_call_preserves_structured_args():
    args = {"action": "start", "command": "npm run dev", "cwd": "C:/Projects/example", "name": "dev"}

    block = function_call_to_tool_block("manage_processes", json.dumps(args))

    assert block is not None
    assert block.tool_type == "manage_processes"
    assert json.loads(block.content) == args


def test_manage_processes_aliases_are_accepted():
    block = function_call_to_tool_block("processes", json.dumps({"action": "list"}))

    assert block is not None
    assert block.tool_type == "manage_processes"


def test_manage_processes_keyword_hint_surfaces_tool():
    matching_hints = [
        tools for keywords, tools in ToolIndex._KEYWORD_HINTS.items()
        if "dev server" in keywords
    ]

    assert matching_hints
    assert "manage_processes" in matching_hints[0]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_unknown_action_is_rejected(jobs_dir):
    result = asyncio.run(manage_processes(json.dumps({"action": "restart"})))
    assert result["exit_code"] == 1
    assert "unknown action" in result["error"]


def test_logs_requires_id(jobs_dir):
    result = asyncio.run(manage_processes(json.dumps({"action": "logs"})))
    assert result["exit_code"] == 1
    assert "id" in result["error"]


def test_stop_unknown_id_errors(jobs_dir):
    result = asyncio.run(manage_processes(json.dumps({"action": "stop", "id": "nope"})))
    assert result["exit_code"] == 1
    assert "nope" in result["error"]


def test_start_requires_command(jobs_dir):
    result = asyncio.run(manage_processes(json.dumps({"action": "start"}), session_id="s1"))
    assert result["exit_code"] == 1
    assert "command" in result["error"]


def test_start_requires_session(jobs_dir):
    result = asyncio.run(manage_processes(json.dumps({"action": "start", "command": "sleep 5"})))
    assert result["exit_code"] == 1
    assert "session" in result["error"]


def test_start_cwd_confined_to_workspace(jobs_dir, tmp_path):
    result = asyncio.run(
        manage_processes(
            json.dumps({"action": "start", "command": "sleep 5", "cwd": "C:/Windows"}),
            session_id="s1",
            workspace=str(tmp_path),
        )
    )
    assert result["exit_code"] == 1


def test_bare_list_word_is_accepted(jobs_dir):
    result = asyncio.run(manage_processes("list"))
    assert result["exit_code"] == 0
    assert "No background jobs" in result["output"]


# ---------------------------------------------------------------------------
# Service lifecycle (spawns a real detached process)
# ---------------------------------------------------------------------------

def test_service_lifecycle_start_logs_list_stop(jobs_dir, tmp_path):
    marker = "svc-marker-up"
    start = asyncio.run(
        manage_processes(
            json.dumps({
                "action": "start",
                "command": _python_service_command(marker),
                "cwd": str(tmp_path),
                "name": "test-svc",
            }),
            session_id="s1",
            workspace=str(tmp_path),
        )
    )
    assert start["exit_code"] == 0, start
    job_id = start["process"]["id"]

    rec = bg_jobs.get(job_id)
    assert rec and rec["status"] == "running"
    assert rec["service"] is True
    assert rec["name"] == "test-svc"

    # Log file is written continuously — marker should appear while running.
    assert _wait_for(lambda: marker in (bg_jobs.tail(job_id, lines=50) or ""))

    logs = asyncio.run(manage_processes(json.dumps({"action": "logs", "id": job_id})))
    assert logs["exit_code"] == 0
    assert marker in logs["output"]

    listing = asyncio.run(manage_processes(json.dumps({"action": "list"})))
    assert listing["exit_code"] == 0
    assert job_id in listing["output"]
    assert "service" in listing["output"]

    stopped = asyncio.run(manage_processes(json.dumps({"action": "stop", "id": job_id})))
    assert stopped["exit_code"] == 0

    rec = bg_jobs.get(job_id)
    assert rec["status"] == "stopped"
    # A deliberate stop must NOT trigger a crash follow-up to the agent.
    assert rec["followed_up"] is True
    assert all(r["id"] != job_id for r in bg_jobs.pending_followups())


def test_duplicate_service_start_is_refused(jobs_dir, tmp_path):
    # Restart loops stack multiple binders on one port (Windows SO_REUSEADDR
    # allows it); the second identical start must point at the existing id.
    payload = json.dumps({
        "action": "start",
        "command": _python_service_command("dup-svc"),
        "cwd": str(tmp_path),
        "name": "dup-svc",
    })
    first = asyncio.run(manage_processes(payload, session_id="s1", workspace=str(tmp_path)))
    assert first["exit_code"] == 0, first
    job_id = first["process"]["id"]
    try:
        second = asyncio.run(manage_processes(payload, session_id="s1", workspace=str(tmp_path)))
        assert second["exit_code"] == 1
        assert "ALREADY RUNNING" in second["error"]
        assert job_id in second["error"]
        # Same command in a DIFFERENT folder is a different service — allowed.
        other = tmp_path / "other"
        other.mkdir()
        third = asyncio.run(
            manage_processes(
                json.dumps({
                    "action": "start",
                    "command": _python_service_command("dup-svc"),
                    "cwd": str(other),
                }),
                session_id="s1",
                workspace=str(tmp_path),
            )
        )
        assert third["exit_code"] == 0, third
        bg_jobs.stop(third["process"]["id"])
    finally:
        bg_jobs.stop(job_id)


def test_service_logs_are_unbuffered_without_dash_u(jobs_dir, tmp_path):
    # Services get PYTHONUNBUFFERED=1 so a python server's output reaches the
    # log file live; without it the tail reads "(no output yet)" and agents
    # misdiagnose a healthy idle server as broken.
    py = Path(sys.executable).as_posix()
    marker = "unbuffered-marker"
    rec = bg_jobs.launch(
        f'"{py}" -c "import time; print(\'{marker}\'); time.sleep(60)"',
        session_id="s1", cwd=str(tmp_path), service=True,
    )
    try:
        assert _wait_for(lambda: marker in (bg_jobs.tail(rec["id"], lines=10) or ""))
    finally:
        bg_jobs.stop(rec["id"])


def test_instant_crash_is_reported_at_start(jobs_dir, tmp_path):
    result = asyncio.run(
        manage_processes(
            json.dumps({"action": "start", "command": "exit 7", "cwd": str(tmp_path)}),
            session_id="s1",
            workspace=str(tmp_path),
        )
    )
    assert result["exit_code"] == 1
    assert "exited immediately" in result["error"]


def test_service_survives_relative_jobs_dir_with_project_cwd(tmp_path, monkeypatch):
    # Regression: in production DATA_DIR defaults to the RELATIVE "data", but
    # the wrapper script runs with the SERVICE's cwd (the user's project
    # folder). Before job files were anchored absolutely, the wrapper's
    # `> data/bg_jobs/x.log` redirect resolved inside the project folder,
    # failed, and every service "died" instantly with no log or exit code.
    backend_root = tmp_path / "backend-root"
    project = tmp_path / "user-project"
    backend_root.mkdir()
    project.mkdir()
    monkeypatch.chdir(backend_root)
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", Path("data") / "bg_jobs")
    monkeypatch.setattr(bg_jobs, "_STORE", backend_root / "bg_jobs.json")

    marker = "relative-dir-svc"
    rec = bg_jobs.launch(
        _python_service_command(marker),
        session_id="s1", cwd=str(project), service=True,
    )
    try:
        assert _wait_for(lambda: marker in (bg_jobs.tail(rec["id"], lines=20) or ""))
        assert (bg_jobs.get(rec["id"]) or {}).get("status") == "running"
    finally:
        bg_jobs.stop(rec["id"])


def test_service_exempt_from_runaway_reap(jobs_dir, tmp_path):
    # A service older than its max_runtime_s must stay running; a plain job
    # with the same age is reaped as a runaway.
    service = bg_jobs.launch(
        _python_service_command("reap-svc", sleep_s=120),
        session_id="s1", cwd=str(tmp_path), max_runtime_s=1, service=True,
    )
    job = bg_jobs.launch(
        _python_service_command("reap-job", sleep_s=120),
        session_id="s1", cwd=str(tmp_path), max_runtime_s=1, service=False,
    )
    try:
        assert _wait_for(lambda: "reap-svc" in (bg_jobs.tail(service["id"], lines=10) or ""))
        time.sleep(1.2)  # both records now exceed max_runtime_s=1

        jobs = bg_jobs.refresh()
        assert jobs[service["id"]]["status"] == "running"
        assert jobs[job["id"]]["status"] == "failed"
        assert jobs[job["id"]].get("timed_out") is True
    finally:
        bg_jobs.stop(service["id"])
        bg_jobs.stop(job["id"])
