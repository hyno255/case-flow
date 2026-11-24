"""Scripts and tools run as child processes with an explicit, minimal environment.

Each script runs in its own process group, behind guard.py. The group is stopped when the script runs past
its own limit, when the item runs out of time, when the run is cancelled, and when the engine goes away.

The engine is a program on this machine, and a script is a plain child process of it, run as the same user: it can do
what that user can. What the engine controls is what a script is told: the variables it names, and none of the engine's keys.
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


async def run_proc(argv: list[str], cwd: Path, stdin: str = "", env: dict | None = None, *, timeout: float, grace: float, poll: float,
                   stop: Callable[[], bool] | None = None) -> tuple[int, str]:
    """Returns (exit code, combined output). Arguments are separate values, never a shell string.
    `timeout` is the script's own limit in seconds; `stop` is asked every `poll` seconds, and when it answers yes the script is
    stopped and the code is STOPPED. A script that is stopped gets `grace` seconds to end by itself. Settings.proc() gives all three."""
    base = {"PATH": os.pathsep.join(filter(None, [os.environ.get("PATH", ""), str(Path(sys.executable).parent)])),  # so a script can call `agentflow history`
            "LANG": os.environ.get("LANG", "C.UTF-8")}
    guard_end, engine_end = os.pipe()  # the guard reads its end; ours closes when this process ends, however it ends
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-S", GUARD, str(guard_end), str(grace), *argv, cwd=cwd, env={**base, **(env or {})},
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
                done, _ = await asyncio.wait({talk}, timeout=poll)
                if done:
                    out, _ = talk.result()
                    return proc.returncode, out.decode(errors="replace").strip()
                if asyncio.get_running_loop().time() >= deadline:
                    await _stop(proc, talk, grace, poll)
                    return TIMED_OUT, f"timed out after {timeout:g}s"
                if stop and stop():
                    await _stop(proc, talk, grace, poll)
                    return STOPPED, "stopped by the engine"
        except asyncio.CancelledError:  # the item ran out of time, or the engine is shutting down
            await _stop(proc, talk, grace, poll)
            raise
    finally:
        os.close(engine_end)


async def _stop(proc, talk, grace: float, poll: float) -> None:
    """Stop the script and everything it started: ask, wait a moment, then force what is left."""
    def send(sig) -> bool:
        try:
            os.killpg(proc.pid, sig)  # the guard is the group's leader, so the group has its process id
            return True
        except OSError:
            return False  # nothing is left in the group

    send(signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        pass
    step, waited = min(poll, grace), 0.0
    while waited < grace and send(0):  # a script that did not end when asked is still in the group
        await asyncio.sleep(step)
        waited += step
    send(signal.SIGKILL)
    await asyncio.wait({talk}, timeout=grace)
    talk.cancel()
