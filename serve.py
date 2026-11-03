"""Start the Titan API and web app on localhost.   uv run python serve.py [--port 8000]"""

import argparse
import sys
import time

sys.path.insert(0, "src")

import uvicorn  # noqa: E402

import config  # noqa: E402
from api import probes  # noqa: E402
from api.server import DIST_DIR, TOKEN_FILE, create_app, load_token  # noqa: E402
from api.wsl import keep_awake  # noqa: E402


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

    holder = None if args.no_keep_wsl_awake else keep_awake()
    if "searxng" in config.SEARCH_PROVIDERS:
        for _ in range(15 if holder else 1):          # waking WSL and Docker can take a few seconds
            problem = probes.searxng()
            if not problem:
                break
            time.sleep(1)
            probes.clear_cache()
        print("SearXNG: ready" if not problem else f"SearXNG: NOT READY - {problem}")
    try:
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="info")
    finally:
        if holder:
            holder.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
