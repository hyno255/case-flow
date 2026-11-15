"""Started in front of every script, so that a script does not outlive the engine.

The engine holds the write end of a pipe and passes the read end here. When the engine's end closes, which
also happens when the engine is killed with no chance to clean up, the read below returns and this process
stops the script and everything the script started: first by asking, then by force.

The engine stops the same group itself when an item runs out of time or its run is cancelled (procs.py).
A script that moves itself to another session escapes both; it ends when the container does.

Run as a file with the standard library only, so it needs nothing from the engine's environment.
"""
import os
import signal
import subprocess
import sys
import threading

GRACE = 2.0  # seconds a script gets to end by itself after being asked


def main() -> None:
    fd, argv = int(sys.argv[1]), sys.argv[2:]
    try:
        child = subprocess.Popen(argv)  # same group, same stdin and output as this process
    except OSError as e:
        print(f"could not start: {e}", file=sys.stderr)
        sys.exit(126)
    stopping = threading.Event()

    def finished() -> None:
        code = child.wait()
        if not stopping.is_set():
            os._exit(code if code >= 0 else 128 - code)  # the script's own exit code, or 128 + the signal

    threading.Thread(target=finished, daemon=True).start()
    try:
        while os.read(fd, 1):
            pass
    except OSError:
        pass
    stopping.set()  # the engine is gone
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # the request below is for the script, not for this process
    os.killpg(os.getpgrp(), signal.SIGTERM)
    try:
        child.wait(timeout=GRACE)
    except subprocess.TimeoutExpired:
        pass
    os.killpg(os.getpgrp(), signal.SIGKILL)  # whatever is left, this process included


if __name__ == "__main__":
    main()
