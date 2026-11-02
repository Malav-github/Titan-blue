"""Compare local models on Titan's real tasks.

  uv run python eval_models.py                                  # the default candidate list
  uv run python eval_models.py --models qwen3:8b gemma4:e4b     # chosen models
  uv run python eval_models.py --tasks support relevance        # chosen tasks (faster)
  uv run python eval_models.py --show a.json b.json              # reprint saved reports, merged
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import model_eval  # noqa: E402
from config import DATA_DIR  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=model_eval.DEFAULT_MODELS)
    ap.add_argument("--tasks", nargs="+", default=list(model_eval.TASKS), choices=model_eval.TASKS)
    ap.add_argument("--show", nargs="+", metavar="FILE", help="reprint saved reports (several are merged)")
    args = ap.parse_args()

    if args.show:
        merged = {}
        for f in args.show:
            merged.update(json.loads(Path(f).read_text(encoding="utf-8")))   # later files win for the same model
        print(model_eval.format_report(merged))
        return 0

    results = model_eval.evaluate(args.models, tasks=tuple(args.tasks))
    out_dir = Path(DATA_DIR) / "evals"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print("\n" + model_eval.format_report(results))
    for model, r in results.items():
        if r["distinct_errors"]:
            print(f"\n{model} errors: {r['distinct_errors']}")
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
