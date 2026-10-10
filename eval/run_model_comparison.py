#!/usr/bin/env python3
"""Run the prompt benchmark for another model under a hard spending cap.

Runs `rhylthyme eval-prompts` one gold program at a time so spending can be
checked between programs. Every live program's cost (as the harness computes
it from the API's token usage) is appended to a ledger file; the ledger
persists, so re-running this script never double-spends and the cap holds
across invocations. A program is only started when

    spent so far + reserve for one more program <= cap

where the reserve is 1.5x the most expensive program seen so far for that
pattern (or a conservative first guess). Programs are taken round-robin
across domains and each program runs every prompt before the next program
starts, so a run the cap cuts short is still balanced and still paired.

Afterwards each (model, pattern) cell is re-scored from its cache, which
costs nothing, to produce one consolidated results.json per cell.

    python eval/run_model_comparison.py --model claude-sonnet-5 --cap 6.50
    python eval/run_model_comparison.py --model claude-sonnet-5 --cap 6.50 --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from itertools import zip_longest
from pathlib import Path

HERE = Path(__file__).resolve().parent
GOLD = HERE.parent.parent / "rhylthyme-examples" / "gold"
FIRST_GUESS = {
    "four-turn": 0.40,
    "baseline": 0.20,
}  # USD per program, before any is measured


def gold_slugs() -> list[str]:
    slugs = sorted(p.name for p in GOLD.iterdir() if p.is_dir())
    by_domain = defaultdict(list)
    for s in slugs:
        by_domain[s.split("-")[0]].append(s)
    rounds = zip_longest(*[by_domain[d] for d in sorted(by_domain)])
    return [s for group in rounds for s in group if s]


def load_ledger(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"entries": []}


def spent(ledger: dict) -> float:
    return round(sum(e["cost_usd"] for e in ledger["entries"]), 6)


def deepseek_balance() -> float | None:
    """The account's USD balance, straight from DeepSeek: what was really
    charged, as opposed to the ledger's peak-rate estimate."""
    import os
    import urllib.request

    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    req = urllib.request.Request(
        "https://api.deepseek.com/user/balance",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            infos = json.loads(resp.read().decode("utf-8")).get("balance_infos") or []
    except Exception:  # noqa: BLE001 - a guard that cannot read must stop the run
        return None
    for info in infos:
        if info.get("currency") == "USD":
            return float(info["total_balance"])
    return None


INFRA = re.compile(
    r"returned HTTP (429|5\d\d)|did not answer within|Connection|timed out|Rate limit|IncompleteRead|RemoteDisconnected",
    re.I,
)


def infra_error(results_file: Path) -> bool:
    """True when a program failed because the provider did (rate limit, 5xx,
    stall), not because of what the model wrote. Those are retried, never
    scored: they say nothing about the model."""
    try:
        data = json.loads(results_file.read_text())
        program = data.get("results", data)["programs"][0]
    except Exception:  # noqa: BLE001
        return True
    return bool(program.get("error")) and bool(INFRA.search(str(program["error"])))


def run_cli(args: list[str]) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "rhylthyme.cli.main", "eval-prompts", *args]
    return subprocess.run(cmd, capture_output=True, text=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--model", required=True)
    ap.add_argument(
        "--cap",
        type=float,
        required=True,
        help="Hard limit on total USD recorded in the ledger.",
    )
    ap.add_argument("--patterns", default="four-turn,baseline")
    ap.add_argument("--out", type=Path, default=HERE / "models")
    ap.add_argument(
        "--ledger", type=Path, default=HERE / "models" / "spend-ledger.json"
    )
    ap.add_argument(
        "--max-programs",
        type=int,
        default=0,
        help="Only the first N programs of the domain round-robin order.",
    )
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Per-call output ceiling passed to eval-prompts (its default is 16000). Reasoning "
        "models count their reasoning against it: DeepSeek and Sonnet lost programs at 16000.",
    )
    ap.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Programs to run at the same time (each runs all its prompts).",
    )
    ap.add_argument(
        "--first-guess",
        type=float,
        default=None,
        help="USD to reserve for the very first program of each prompt, before any cost is measured "
        "(default: 0.40 four-turn, 0.20 baseline, sized for a mid-tier model).",
    )
    ap.add_argument(
        "--real-budget",
        type=float,
        default=0.0,
        help="DeepSeek only: stop when the account balance has dropped this many USD "
        "since --start-balance (real charges; the ledger cap still applies as a backstop).",
    )
    ap.add_argument(
        "--start-balance",
        type=float,
        default=None,
        help="Balance before any of this model's runs (default: the balance now).",
    )
    ap.add_argument(
        "--real-reserve",
        type=float,
        default=0.15,
        help="Headroom kept under --real-budget for the next program and billing lag.",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(HERE.parent / "src"))
    from rhylthyme.cli.eval.llm import price_for

    if price_for(args.model) is None:
        sys.exit(
            f"No price known for {args.model}: every run would be recorded as $0 and the cap would never trip.\n"
            "Add it to PRICES_PER_MTOK in eval/llm.py, or set RHYLTHYME_EVAL_PRICE_IN and RHYLTHYME_EVAL_PRICE_OUT (USD per million tokens)."
        )
    args.out.mkdir(parents=True, exist_ok=True)
    # "vendor/model" ids (OpenRouter) must not nest directories.
    model_dir = args.model.replace("/", "__")
    ledger = load_ledger(args.ledger)
    slugs = gold_slugs()
    # A ledger entry whose program failed for infrastructure reasons is not
    # done: the money is counted, the program is run again.
    done = set()
    for e in ledger["entries"]:
        rf = (
            args.out
            / e["model"].replace("/", "__")
            / e["pattern"]
            / "per-program"
            / e["slug"]
            / "results.json"
        )
        if not infra_error(rf):
            done.add((e["model"], e["pattern"], e["slug"]))
    print(
        f"ledger: ${spent(ledger):.2f} spent of ${args.cap:.2f} cap; {len(slugs)} gold programs"
    )

    patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]
    if args.max_programs:
        slugs = slugs[: args.max_programs]

    def reserve_for(pattern: str) -> float:
        seen = [
            e["cost_usd"]
            for e in ledger["entries"]
            if e["model"] == args.model and e["pattern"] == pattern
        ]
        if seen:
            return 1.5 * max(seen)
        return (
            args.first_guess
            if args.first_guess is not None
            else FIRST_GUESS.get(pattern, 0.40)
        )

    start_balance = None
    if args.real_budget:
        if not args.model.startswith("deepseek-"):
            sys.exit(
                "--real-budget reads the DeepSeek balance API; it only works for deepseek-* models."
            )
        start_balance = (
            args.start_balance if args.start_balance is not None else deepseek_balance()
        )
        if start_balance is None:
            sys.exit(
                "Could not read the DeepSeek balance; refusing to run without the real-budget guard."
            )
        print(
            f"real budget: ${args.real_budget:.2f} from a starting balance of ${start_balance:.2f}"
        )

    # Slug outer, pattern inner: every program gets all its prompts or none,
    # so a run the cap cuts short is still a paired comparison. --jobs runs
    # several programs at once; the budget check then reserves for the batch.
    import threading
    from concurrent.futures import ThreadPoolExecutor

    lock = threading.Lock()

    def run_slug(slug: str, todo: list) -> bool:
        started = time.time()
        procs = {}
        for pattern in todo:
            cell = args.out / model_dir / pattern
            cmd = [
                sys.executable,
                "-m",
                "rhylthyme.cli.main",
                "eval-prompts",
                "--gold",
                str(GOLD),
                "--model",
                args.model,
                "--patterns",
                pattern,
                "--only",
                slug,
                "--out",
                str(cell / "per-program" / slug),
                "--cache-dir",
                str(cell / "cache"),
                "--format",
                "json",
                *(["--max-tokens", str(args.max_tokens)] if args.max_tokens else []),
            ]
            procs[pattern] = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
        ok = True
        for pattern, proc in procs.items():
            _out, err = proc.communicate()
            results_file = (
                args.out / model_dir / pattern / "per-program" / slug / "results.json"
            )
            if not results_file.exists():
                print(f"FAILED {pattern}/{slug}: {err.strip()[-400:]}", flush=True)
                ok = False
                continue
            totals = json.loads(results_file.read_text())["meta"]["totals"]
            cost = float(totals.get("cost_usd") or 0.0)
            entry = {
                "model": args.model,
                "pattern": pattern,
                "slug": slug,
                "cost_usd": cost,
                "input_tokens": totals.get("input_tokens"),
                "output_tokens": totals.get("output_tokens"),
                "calls": totals.get("calls"),
                "seconds": round(time.time() - started, 1),
            }
            if infra_error(results_file):
                entry["infra_error"] = (
                    True  # spent money is counted; the program is retried
                )
            with lock:
                ledger["entries"].append(entry)
                args.ledger.write_text(json.dumps(ledger, indent=1) + "\n")
                print(
                    f"{pattern:10} {slug:45} ${cost:.3f}  total ${spent(ledger):.2f}  ({entry['seconds']:.0f}s)",
                    flush=True,
                )
        return ok

    pending = [
        (s, [p for p in patterns if (args.model, p, s) not in done]) for s in slugs
    ]
    pending = [(s, todo) for s, todo in pending if todo]
    jobs = max(1, args.jobs)
    while pending:
        batch = []
        need = 0.0
        for slug, todo in pending[:jobs]:
            cost_next = sum(reserve_for(p) for p in todo)
            if spent(ledger) + need + cost_next > args.cap:
                break
            batch.append((slug, todo))
            need += cost_next
        if not batch:
            print(
                f"STOP before {pending[0][0]}: ${spent(ledger):.2f} spent + reserve > ${args.cap:.2f} cap"
            )
            break
        if start_balance is not None and not args.dry_run:
            now = deepseek_balance()
            real = None if now is None else start_balance - now
            if real is None or real + args.real_reserve * len(batch) > args.real_budget:
                print(
                    f"STOP before {batch[0][0]}: real-budget guard (charged so far: {real})"
                )
                break
            print(
                f"  balance ${now:.2f} (really charged so far ${real:.2f})", flush=True
            )
        pending = pending[len(batch) :]
        if args.dry_run:
            for slug, todo in batch:
                for pattern in todo:
                    print(f"would run {pattern}/{slug}")
            continue
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            results = list(pool.map(lambda st: run_slug(*st), batch))
        if not all(results):
            sys.exit(2)  # never loop on an error that might be costing money

    if args.dry_run:
        return
    # Consolidate each cell by merging its per-program results (no model
    # calls). Programs that only ever failed for infrastructure reasons are
    # left out rather than scored as model failures.
    for pattern in patterns:
        cell = args.out / model_dir / pattern
        programs, skipped, meta = [], [], None
        for rf in sorted((cell / "per-program").glob("*/results.json")):
            if infra_error(rf):
                skipped.append(rf.parent.name)
                continue
            data = json.loads(rf.read_text())
            data = data.get("results", data)
            meta = meta or data.get("meta")
            programs += data["programs"]
        if not programs:
            continue
        merged = {
            "meta": dict(
                meta or {},
                merged_from="per-program",
                programs=len(programs),
                infra_skipped=skipped,
            ),
            "programs": programs,
        }
        (cell / "results.json").write_text(json.dumps(merged, indent=1) + "\n")
        note = f" (infra failures left out: {', '.join(skipped)})" if skipped else ""
        print(f"consolidated {args.model}/{pattern}: {len(programs)} programs{note}")
    print(f"final: ${spent(ledger):.2f} of ${args.cap:.2f}")


if __name__ == "__main__":
    main()
