"""Keep WSL awake while Titan runs.

WSL shuts its virtual machine down when nothing is using it, and the SearXNG container and the code sandbox go
with it. Docker brings the container back only when something wakes WSL again, so searches in between fail. A
sleeping process inside WSL keeps the machine up for as long as the server is running.
"""

import subprocess


def keep_awake(popen=subprocess.Popen) -> subprocess.Popen | None:
    """Start the keep-alive process; returns it (call .terminate() to stop) or None if WSL is not available."""
    try:
        return popen(["wsl", "-e", "sleep", "infinity"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None
