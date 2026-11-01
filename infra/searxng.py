"""Start, stop or inspect Titan's private SearXNG (Docker inside WSL).

Usage: uv run python infra/searxng.py [start|stop|status]
"""

import secrets
import subprocess
import sys
from pathlib import Path

NAME = "titan-searxng"
IMAGE = "searxng/searxng:latest"
SETTINGS = Path(__file__).parent / "searxng" / "settings.yml"


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["wsl", "-e", "docker", *args], text=True, capture_output=True)


def wsl_path(path: Path) -> str:
    out = subprocess.run(["wsl", "-e", "wslpath", "-a", str(path.resolve())], text=True, capture_output=True)
    return out.stdout.strip()


def start() -> int:
    docker("rm", "-f", NAME)
    result = docker(
        "run", "-d", "--name", NAME, "--restart", "unless-stopped",
        "-p", "127.0.0.1:8080:8080",
        "-e", f"SEARXNG_SECRET={secrets.token_hex(32)}",
        "-v", f"{wsl_path(SETTINGS)}:/etc/searxng/settings.yml:ro",
        "--cap-drop", "ALL", "--cap-add", "CHOWN", "--cap-add", "SETGID", "--cap-add", "SETUID",
        "--security-opt", "no-new-privileges",
        IMAGE,
    )
    print(result.stdout or result.stderr)
    return result.returncode


def main(action: str) -> int:
    if action == "start":
        return start()
    if action == "stop":
        return docker("rm", "-f", NAME).returncode
    result = docker("ps", "-a", "--filter", f"name={NAME}", "--format", "{{.Names}}  {{.Status}}  {{.Ports}}")
    print(result.stdout or "not created")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "start"))
