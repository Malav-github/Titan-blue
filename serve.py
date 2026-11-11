"""Start the Titan API and web app on localhost.   uv run python serve.py [--port 8000]"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "src")

import uvicorn  # noqa: E402

import config  # noqa: E402
from api import probes  # noqa: E402
from api.server import DIST_DIR, TOKEN_FILE, create_app, load_token  # noqa: E402
from api.wsl import keep_awake  # noqa: E402


def report_searxng(waking: bool) -> None:
    for _ in range(15 if waking else 1):          # waking WSL and Docker can take a few seconds
        problem = probes.searxng()
        if not problem:
            break
        time.sleep(1)
        probes.clear_cache()
    print("SearXNG: ready" if not problem else f"SearXNG: NOT READY - {problem}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-keep-wsl-awake", action="store_true", help="do not hold WSL open (SearXNG and the sandbox stop when WSL idles)")
    args = ap.parse_args()
    load_token()
    print(f"Titan API on http://127.0.0.1:{args.port}  (session token in {TOKEN_FILE})")
    if (DIST_DIR / "index.html").exists():
        print(f"Open the app: http://127.0.0.1:{args.port}/")
    else:
        print("The app is not built yet: cd frontend && npm install && npm run build   (or run `npm run dev` there for development)")

    wait_for_port(args.port)                      # after a restart, the old process may still be letting go of it
    holder = None if args.no_keep_wsl_awake else keep_awake()
    if "searxng" in config.SEARCH_PROVIDERS:
        threading.Thread(target=report_searxng, args=(bool(holder),), daemon=True).start()   # the app must not wait for search
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="info", timeout_graceful_shutdown=5))
    restart = threading.Event()

    def request_restart():                        # the app's Restart button (POST /api/server/restart)
        restart.set()
        server.should_exit = True
    app.state.request_restart = request_restart
    try:
        server.run()
    finally:
        if holder:
            holder.terminate()
    if restart.is_set():
        print("Restarting with the current code...", flush=True)
        rebuild_app_if_stale()
        subprocess.Popen([sys.executable, *sys.argv], cwd=os.getcwd())     # a fresh process: new code, same options
    return 0


def rebuild_app_if_stale() -> None:
    """The app is served from frontend/dist: rebuild it when its source changed after the last build."""
    src, dist = DIST_DIR.parent / "src", DIST_DIR
    newest = lambda d: max((p.stat().st_mtime for p in d.rglob("*") if p.is_file()), default=0)   # noqa: E731
    if not src.exists() or (dist.exists() and newest(dist) >= newest(src)):
        return
    npm = shutil.which("npm") or next((str(p) for p in (Path(os.getenv("ProgramFiles", r"C:\Program Files")) / "nodejs" / "npm.cmd",)
                                       if p.exists()), None)
    if not npm:
        print("The app's source changed, but npm was not found to rebuild it; the previous build is served.", flush=True)
        return
    print("The app's source changed since it was built: building it...", flush=True)
    env = {**os.environ, "PATH": f"{Path(npm).parent}{os.pathsep}{os.environ.get('PATH', '')}"}     # npm needs node beside it
    done = subprocess.run([npm, "run", "build"], cwd=DIST_DIR.parent, env=env)
    if done.returncode:
        print("The app could not be built; the previous build is served.", flush=True)


def wait_for_port(port: int, seconds: float = 20) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
