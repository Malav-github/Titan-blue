"""Start the Titan API on localhost.   uv run python serve.py [--port 8000]"""

import argparse
import sys

sys.path.insert(0, "src")

import uvicorn  # noqa: E402

from api.server import DIST_DIR, TOKEN_FILE, create_app, load_token  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    load_token()
    print(f"Titan API on http://127.0.0.1:{args.port}  (session token in {TOKEN_FILE})")
    if (DIST_DIR / "index.html").exists():
        print(f"Open the app: http://127.0.0.1:{args.port}/")
    else:
        print("The app is not built yet: cd frontend && npm install && npm run build   (or run `npm run dev` there for development)")
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
