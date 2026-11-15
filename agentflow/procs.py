"""Scripts and tools run as child processes with an explicit, minimal environment.

Each script runs in its own process group, behind guard.py. The group is stopped when the script runs past
its own limit, when the item runs out of time, when the run is cancelled, and when the engine goes away.

The engine runs inside one container image (see Dockerfile), and that container is what separates scripts from
the machine it runs on. Inside it a script is a plain child process: it is given only the secrets its plugin was granted.
"""
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Callable


DONE_EARLY = 78  # a stage script exits with this to say: this item is finished, skip the remaining stages
TIMED_OUT = 124
NOT_STARTED = 126
STOPPED = 125    # the engine stopped the script because `stop` said so (the run was cancelled)
GUARD = str(Path(__file__).with_name("guard.py"))
GRACE = 2.0      # seconds a script gets to end by itself after being asked
POLL = 0.5       # how often a running script's limit and `stop` are looked at


async def run_proc(argv: list[str], cwd: Path, stdin: str = "", env: dict | None = None, timeout: int = 120,
                   stop: Callable[[], bool] | None = None) -> tuple[int, str]:
    """Returns (exit code, combined output). Arguments are separate values, never a shell string.
    `stop` is asked twice a second; when it answers yes the script is stopped and the code is STOPPED."""
    base = {"PATH": os.environ.get("PATH", ""), "LANG": os.environ.get("LANG", "C.UTF-8")}
    guard_end, engine_end = os.pipe()  # the guard reads its end; ours closes when this process ends, however it ends
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", GUARD, str(guard_end), *argv, cwd=cwd, env={**base, **(env or {})},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                pass_fds=(guard_end,), start_new_session=True)
        except OSError as e:
            return NOT_STARTED, f"could not start: {e}"
        finally:
            os.close(guard_end)
        talk = asyncio.ensure_future(proc.communicate(stdin.encode()))
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while True:
                done, _ = await asyncio.wait({talk}, timeout=POLL)
                if done:
                    out, _ = talk.result()
                    return proc.returncode, out.decode(errors="replace").strip()
                if asyncio.get_running_loop().time() >= deadline:
                    await _stop(proc, talk)
                    return TIMED_OUT, f"timed out after {timeout}s"
                if stop and stop():
                    await _stop(proc, talk)
                    return STOPPED, "stopped by the engine"
        except asyncio.CancelledError:  # the item ran out of time, or the engine is shutting down
            await _stop(proc, talk)
            raise
    finally:
        os.close(engine_end)


async def _stop(proc, talk) -> None:
    """Stop the script and everything it started: ask, wait a moment, then force what is left."""
    def send(sig) -> bool:
        try:
            os.killpg(proc.pid, sig)  # the guard is the group's leader, so the group has its process id
            return True
        except OSError:
            return False  # nothing is left in the group

    send(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), GRACE)
    except asyncio.TimeoutError:
        pass
    waited = 0.0
    while waited < GRACE and send(0):  # a script that did not end when asked is still in the group
        await asyncio.sleep(0.05)
        waited += 0.05
    send(signal.SIGKILL)
    await asyncio.wait({talk}, timeout=1)
    talk.cancel()
