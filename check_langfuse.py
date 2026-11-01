"""Check the Langfuse connection and send one small traced call. Run: uv run python check_langfuse.py"""

import sys

sys.path.insert(0, "src")

import tracing  # noqa: E402


def main() -> int:
    status = tracing.check_connection()
    print(f"Host: {status['host']}")
    if not status["ok"]:
        print(f"FAIL  {status['error'] or 'credentials rejected'}")
        return 1
    print("OK    credentials accepted")

    from llm import get_chat_model

    reply = get_chat_model().invoke(
        "Reply with the single word: pong", config={"callbacks": tracing.callbacks(), "run_name": "titan-connection-check"}
    )
    tracing.flush()
    print(f"OK    model replied {reply.content.strip()[:40]!r}; trace 'titan-connection-check' sent")
    print("Open your Langfuse project -> Tracing to see it (can take a few seconds).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
