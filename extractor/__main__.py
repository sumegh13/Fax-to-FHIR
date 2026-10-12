"""Run the Claude extractor over generated faxes, writing predictions for the evaluator.

    python -m extractor --limit 3                 # quick check
    python -m extractor --run haiku               # all faxes -> data/predictions/haiku/
    python -m evaluate report haiku

Faxes that already have a prediction are skipped, so an interrupted run resumes.
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
import anthropic

from .claude import extract

load_dotenv()  # .env at the repo root; real environment variables win

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="haiku")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    out = ROOT / "data" / "predictions" / args.run
    out.mkdir(parents=True, exist_ok=True)
    faxes = sorted((ROOT / "data" / "faxes").glob("*.png"))
    faxes = [f for f in faxes if not f.name.endswith(".boxes.png")]
    todo = [f for f in faxes if not (out / f"{f.stem}.json").exists()]
    done = len(faxes) - len(todo)
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} faxes to read ({done} already done)")

    client = anthropic.Anthropic()
    total, failed = 0.0, 0
    with ThreadPoolExecutor(args.workers) as pool:
        jobs = {pool.submit(extract, f, f.stem, client): f for f in todo}
        for i, job in enumerate(as_completed(jobs), 1):
            f = jobs[job]
            try:
                pred = job.result()
            except anthropic.RateLimitError:
                failed += 1
                print(f"[{i}/{len(todo)}] {f.stem}: rate limited, rerun to retry")
                continue
            except (anthropic.APIError, RuntimeError) as e:
                failed += 1
                print(f"[{i}/{len(todo)}] {f.stem}: {type(e).__name__}: {e}")
                continue
            (out / f"{f.stem}.json").write_text(json.dumps(pred, indent=1))
            total += pred["usage"]["cost_usd"]
            low = sum(fl["confidence"] < 0.8 for fl in pred["fields"])
            print(f"[{i}/{len(todo)}] {f.stem}: {len(pred['fields'])} fields, {low} below 0.8, "
                  f"${pred['usage']['cost_usd']:.4f}")
    print(f"done: {len(todo) - failed} read, {failed} failed, ${total:.3f} spent")


if __name__ == "__main__":
    main()
