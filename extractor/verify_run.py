"""Apply the zoomed second read to an existing prediction run.

    python -m extractor.verify_run --src haiku --dst haiku-verify
    python -m evaluate report haiku-verify
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from .verify import verify

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="haiku")
    ap.add_argument("--dst", default="haiku-verify")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    src, dst = ROOT / "data" / "predictions" / args.src, ROOT / "data" / "predictions" / args.dst
    dst.mkdir(parents=True, exist_ok=True)
    todo = [p for p in sorted(src.glob("*.json")) if not (dst / p.name).exists()]
    client = anthropic.Anthropic()
    extra, n_fields, n_agree, failed = 0.0, 0, 0, 0

    def run(p):
        pred = json.loads(p.read_text())
        return p, pred, verify(ROOT / "data" / "faxes" / f"{pred['fax_id']}.png", pred, client)

    with ThreadPoolExecutor(args.workers) as pool:
        for job in as_completed([pool.submit(run, p) for p in todo]):
            try:
                p, before, after = job.result()
            except (anthropic.APIError, RuntimeError) as e:
                failed += 1
                print(f"failed: {type(e).__name__}: {e}")
                continue
            (dst / p.name).write_text(json.dumps(after, indent=1))
            extra += after["usage"]["cost_usd"] - before["usage"]["cost_usd"]
            n_fields += after["verify"]["fields"]
            n_agree += after["verify"].get("agreed", 0)
    print(f"re-read {n_fields} fields in {len(todo) - failed} faxes, {n_agree} agreed, {failed} failed, "
          f"${extra:.3f} extra")


if __name__ == "__main__":
    main()
