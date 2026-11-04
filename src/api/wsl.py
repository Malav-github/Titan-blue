"""Keep WSL awake while Titan runs.

WSL shuts its virtual machine down when nothing is using it, and the SearXNG container and the code sandbox go
with it. Docker brings the container back only when something wakes WSL again, so searches in between fail. A
sleeping process inside WSL keeps the machine up for as long as the server is running.

If the server is killed instead of stopped (closing the window, a crash), the helper outlives it. Its process id is
kept in a file, so the next start reuses that helper instead of adding another one.
"""

import subprocess
from pathlib import Path

import config

PID_FILE = config.DATA_DIR / "wsl_keepalive.pid"
COMMAND = ["wsl", "-e", "sleep", "infinity"]


def helper_alive(pid: int, run=subprocess.run) -> bool:
    """Is `pid` still a wsl.exe process? (An id can be reused by something else after the helper died.)"""
    try:
        out = run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "wsl.exe" in out.lower() and f'"{pid}"' in out


class Helper:
    def __init__(self, pid: int, proc=None, pid_file: Path = PID_FILE, run=subprocess.run):
        self.pid, self.proc, self.pid_file, self._run = pid, proc, pid_file, run

    def terminate(self) -> None:
        """Stop the helper and the child wsl.exe it starts (the whole process tree)."""
        try:
            self._run(["taskkill", "/PID", str(self.pid), "/T", "/F"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass
        if self.proc is not None:
            self.proc.terminate()                      # harmless if it is already gone
        self.pid_file.unlink(missing_ok=True)


def keep_awake(popen=subprocess.Popen, pid_file: Path = PID_FILE, alive=helper_alive, run=subprocess.run) -> Helper | None:
    """Make sure one keep-alive helper is running and return it (call .terminate() to stop it), or None without WSL."""
    try:
        old = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        old = None
    if old is not None and alive(old):
        return Helper(old, None, pid_file, run)        # reuse the one an earlier run left behind
    try:
        proc = popen(COMMAND, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    return Helper(proc.pid, proc, pid_file, run)
