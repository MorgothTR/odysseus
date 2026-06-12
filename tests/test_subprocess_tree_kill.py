import asyncio
import sys
import time
from pathlib import Path

from core.platform_compat import pid_alive
from src.tool_execution import _run_subprocess_streaming


def _wait_for(predicate, timeout_s: float = 10.0, interval_s: float = 0.25):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False


def test_timeout_kills_grandchildren(tmp_path):
    # Regression: proc.kill() on timeout/cancel killed only the shell; a
    # grandchild it had spawned (e.g. a dev server) survived as an orphan
    # with dead pipes and kept squatting on its port. The kill must take
    # out the whole tree.
    pidfile = tmp_path / "grandchild.pid"
    spawner = tmp_path / "spawner.py"
    spawner.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open(r'{pidfile}', 'w').write(str(p.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )

    async def run():
        proc = await asyncio.create_subprocess_shell(
            f'"{Path(sys.executable).as_posix()}" "{spawner.as_posix()}"',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # mirrors _direct_fallback's spawn kwargs
        )
        # Give the spawner time to create the grandchild, then time out.
        return await _run_subprocess_streaming(proc, timeout=4)

    stdout, stderr, rc, timed_out = asyncio.run(run())

    assert timed_out is True
    assert pidfile.exists(), f"spawner never started (stdout={stdout!r} stderr={stderr!r})"
    grandchild_pid = int(pidfile.read_text().strip())

    # Tree kill is asynchronous on Windows (taskkill) — allow a grace window.
    assert _wait_for(lambda: not pid_alive(grandchild_pid)), (
        f"grandchild {grandchild_pid} survived the timeout kill"
    )
