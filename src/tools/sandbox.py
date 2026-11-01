"""Run untrusted code in a throwaway Docker container inside WSL: no network, read-only, capped resources."""

import subprocess
import uuid

IMAGE = "python:3.13-slim"
MAX_OUTPUT_CHARS = 10_000


def _docker(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["wsl", "-e", "docker", *args], **kwargs)


def _run(command: list[str], stdin: str, timeout: int) -> dict:
    name = f"titan-sbx-{uuid.uuid4().hex[:12]}"
    args = [
        "run", "--rm", "-i", "--name", name,
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m",
        "--memory", "512m", "--cpus", "1", "--pids-limit", "128",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "65534:65534", "-e", "HOME=/tmp", "-w", "/tmp",
        IMAGE, *command,
    ]
    # Bytes, not text: Windows text mode would turn \n into \r\n and break shell scripts
    try:
        proc = _docker(args, input=stdin.encode("utf-8"), capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _docker(["kill", name], capture_output=True)
        return {"exit_code": None, "timed_out": True, "stdout": "", "stderr": f"Timed out after {timeout}s"}

    def decode(b: bytes) -> str:
        return b.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS]

    return {
        "exit_code": proc.returncode,
        "timed_out": False,
        "stdout": decode(proc.stdout),
        "stderr": decode(proc.stderr),
    }


def run_python(code: str, timeout: int = 30, stdin: str = "") -> dict:
    # The real stdin carries the code itself, so input data is injected as sys.stdin
    if stdin:
        code = f"import io, sys; sys.stdin = io.StringIO({stdin!r})\n{code}"
    return _run(["python", "-"], code, timeout)


def run_shell(script: str, timeout: int = 30) -> dict:
    return _run(["sh", "-s"], script, timeout)
