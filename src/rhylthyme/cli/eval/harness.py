"""Live evaluation harness: prompt a model per gold program, then score.

For every gold program the harness opens a :class:`Session` (client +
response cache + token counters), hands it to the chosen
:class:`~.patterns.Pattern`, scores whatever program comes back with
:func:`~.metrics.score_program`, and records tokens, cost, cache keys,
fix iterations and file paths in ``ComponentScores.extras``. Programs run
in a thread pool (``--concurrency``); results keep gold order.

Cache layout: ``<cache_dir>/<sha256>.json`` where the key covers the
model, the pattern name, the system prompt and the full message list, so
every turn of a multi-turn pattern is cached on its own. ``--from-cache``
never calls the model and raises :class:`CacheMiss` on the first key it
cannot find.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from rhylthyme.eval.gold import GoldProgram
from rhylthyme.eval.metrics import ComponentScores, SetResult, score_program

from .llm import Client, Completion, estimate_cost
from .patterns import Pattern, PatternResult, get_pattern

RESPONSES_DIR = "responses"
PREDICTED_DIR = "predicted"
CACHE_DIR = "cache"


class CacheMiss(RuntimeError):
    """``--from-cache`` asked for a response that was never recorded."""


@dataclass
class HarnessConfig:
    model: str
    pattern: str = "baseline"
    out_dir: Path = Path("./eval-results")
    cache_dir: Optional[Path] = None
    from_cache: bool = False
    limit: Optional[int] = None
    only: Optional[Sequence[str]] = None
    concurrency: int = 1
    max_fix_iterations: int = 2
    max_tokens: int = 16000
    threshold: float = 0.5
    js: bool = True

    def __post_init__(self) -> None:
        self.out_dir = Path(self.out_dir)
        self.cache_dir = (
            Path(self.cache_dir) if self.cache_dir else self.out_dir / CACHE_DIR
        )
        if self.only is not None:
            self.only = [str(slug).strip() for slug in self.only if str(slug).strip()]
        if self.concurrency < 1:
            self.concurrency = 1


def cache_key(
    model: str,
    pattern: str,
    system: Optional[str],
    messages: Sequence[Dict[str, Any]],
) -> str:
    """sha256 over the model, pattern and the rendered prompt (system + messages)."""
    payload = json.dumps(
        {"model": model, "pattern": pattern, "system": system, "messages": messages},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cache_record(
    key: str,
    model: str,
    pattern: str,
    slug: str,
    system: Optional[str],
    messages: Sequence[Dict[str, Any]],
    completion: Completion,
) -> Dict[str, Any]:
    """One cache file's content.

    The system prompt is recorded as a digest rather than in full, and the
    provider's raw response body is dropped: both are reproducible from
    the pattern and the completion, and the cache is committed to the
    repository, so it is kept small on purpose.
    """
    body = completion.to_dict()
    body.pop("raw", None)
    return {
        "key": key,
        "model": model,
        "pattern": pattern,
        "slug": slug,
        "recorded_at": _now(),
        "system_sha256": (
            hashlib.sha256(system.encode("utf-8")).hexdigest() if system else None
        ),
        "messages": list(messages),
        "completion": body,
    }


class Session:
    """Model access for one program run: cache, transcript and token counts."""

    def __init__(
        self,
        client: Client,
        config: HarnessConfig,
        pattern_name: str,
        slug: str,
    ):
        self.client = client
        self.config = config
        self.pattern_name = pattern_name
        self.slug = slug
        self.transcript: List[Dict[str, Any]] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_keys: List[str] = []
        self.cache_hits = 0

    def _cache_path(self, key: str) -> Path:
        cache_dir = self.config.cache_dir
        assert cache_dir is not None  # filled in by HarnessConfig.__post_init__
        return cache_dir / f"{key}.json"

    def complete(
        self,
        messages: Sequence[Dict[str, Any]],
        *,
        system: Optional[str] = None,
    ) -> Completion:
        messages = [dict(message) for message in messages]
        key = cache_key(self.config.model, self.pattern_name, system, messages)
        path = self._cache_path(key)
        cached = None
        if path.exists():
            with open(path, "r", encoding="utf-8") as handle:
                cached = json.load(handle)
        if cached is not None:
            completion = Completion.from_dict(cached["completion"])
            self.cache_hits += 1
        elif self.config.from_cache:
            raise CacheMiss(
                f"{self.slug}: no cached response for key {key[:12]}… under "
                f"{self.config.cache_dir} (model={self.config.model}, "
                f"pattern={self.pattern_name}); run without --from-cache first"
            )
        else:
            completion = self.client.complete(
                messages,
                system=system,
                model=self.config.model,
                max_tokens=self.config.max_tokens,
            )
            assert self.config.cache_dir is not None  # set in __post_init__
            self.config.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    cache_record(
                        key,
                        self.config.model,
                        self.pattern_name,
                        self.slug,
                        system,
                        messages,
                        completion,
                    ),
                    handle,
                    indent=1,
                    ensure_ascii=False,
                )
                handle.write("\n")
        self.cache_keys.append(key)
        self.input_tokens += completion.input_tokens
        self.output_tokens += completion.output_tokens
        self.transcript.append(
            {
                "cache_key": key,
                "system": system,
                "messages": messages,
                "reply": completion.text,
                "input_tokens": completion.input_tokens,
                "output_tokens": completion.output_tokens,
                "stop_reason": completion.stop_reason,
            }
        )
        return completion


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def run_program(
    gold: GoldProgram,
    pattern: Pattern,
    client: Client,
    config: HarnessConfig,
) -> ComponentScores:
    """Prompt, extract, validate and score one gold program."""
    session = Session(client, config, pattern.name, gold.slug)
    result: PatternResult
    try:
        result = pattern.run(gold, session)
    except CacheMiss:
        raise
    except Exception as exc:  # noqa: BLE001 - a model/pattern failure scores zero
        result = PatternResult(error=f"{type(exc).__name__}: {exc}")

    if result.program is not None and not result.error:
        scores = score_program(
            gold, result.program, threshold=config.threshold, js=config.js
        )
    else:
        scores = ComponentScores.zero(
            gold.slug, result.error or "no program produced", gold.program
        )

    response_path = _write_json(
        config.out_dir / RESPONSES_DIR / f"{gold.slug}.json",
        {
            "slug": gold.slug,
            "model": config.model,
            "pattern": pattern.name,
            "error": result.error,
            "fix_iterations": result.fix_iterations,
            "validation_errors": result.validation_errors,
            "calls": session.transcript,
        },
    )
    predicted_path = None
    if result.program is not None:
        predicted_path = _write_json(
            config.out_dir / PREDICTED_DIR / f"{gold.slug}.json", result.program
        )

    scores.extras.update(
        {
            "model": config.model,
            "pattern": pattern.name,
            "calls": len(session.transcript),
            "input_tokens": session.input_tokens,
            "output_tokens": session.output_tokens,
            "cost_usd": estimate_cost(
                config.model, session.input_tokens, session.output_tokens
            ),
            "cache_keys": list(session.cache_keys),
            "fix_iterations": result.fix_iterations,
            "validation_errors": list(result.validation_errors),
            "response_path": str(response_path),
            "predicted_path": str(predicted_path) if predicted_path else None,
        }
    )
    scores.extras.update(result.extras)
    return scores


ProgressFn = Callable[[ComponentScores], None]


def run_harness(
    gold_set: Iterable[GoldProgram],
    config: HarnessConfig,
    client: Client,
    *,
    progress: Optional[ProgressFn] = None,
) -> SetResult:
    """Run the configured pattern over the gold set and return scored results."""
    pattern = get_pattern(config.pattern)
    programs = list(gold_set)
    if config.only:
        wanted = list(config.only)
        available = {gold.slug for gold in programs}
        unknown = [slug for slug in wanted if slug not in available]
        if unknown:
            raise KeyError(
                "unknown gold slug(s) in --only: "
                + ", ".join(unknown)
                + "; available: "
                + ", ".join(sorted(available))
            )
        programs = [gold for gold in programs if gold.slug in set(wanted)]
    if config.limit is not None:
        programs = programs[: config.limit]
    config.out_dir.mkdir(parents=True, exist_ok=True)

    lock = threading.Lock()

    def work(gold: GoldProgram) -> ComponentScores:
        scores = run_program(gold, pattern, client, config)
        if progress is not None:
            with lock:
                progress(scores)
        return scores

    if config.concurrency > 1 and len(programs) > 1:
        with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
            scored = list(pool.map(work, programs))
    else:
        scored = [work(gold) for gold in programs]

    result = SetResult(programs=scored)
    result.missing = [s.slug for s in scored if s.error]
    totals = totals_for(scored)
    result.meta.update(
        {
            "mode": "live",
            "model": config.model,
            "pattern": config.pattern,
            "from_cache": config.from_cache,
            "cache_dir": str(config.cache_dir),
            "limit": config.limit,
            "only": list(config.only) if config.only else None,
            "concurrency": config.concurrency,
            "max_fix_iterations": config.max_fix_iterations,
            "max_tokens": config.max_tokens,
            "threshold": config.threshold,
            "totals": totals,
        }
    )
    return result


def totals_for(scored: Sequence[ComponentScores]) -> Dict[str, Any]:
    """Token, call and cost totals over a set of scored programs."""
    calls = sum(int(s.extras.get("calls", 0) or 0) for s in scored)
    input_tokens = sum(int(s.extras.get("input_tokens", 0) or 0) for s in scored)
    output_tokens = sum(int(s.extras.get("output_tokens", 0) or 0) for s in scored)
    costs = [s.extras.get("cost_usd") for s in scored]
    cost = sum(c for c in costs if c is not None) if any(costs) else None
    return {
        "programs": len(scored),
        "calls": calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost,
        "fix_iterations": sum(
            int(s.extras.get("fix_iterations", 0) or 0) for s in scored
        ),
    }


def git_note(repo: Path | str | None = None) -> str:
    """One line naming the commit the baseline was produced from.

    The monorepo has one git repository per subproject, so this records
    the rhylthyme-cli-runner HEAD (short sha, dirty flag and subject).
    Falls back to a plain note when git is unavailable.
    """
    import subprocess

    root = Path(repo) if repo else Path(__file__).resolve().parents[3]
    try:
        describe = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%h %s"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if describe.returncode != 0:
            return f"no git metadata for {root.name}"
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        dirty = " (working tree dirty)" if status.stdout.strip() else ""
        return f"{root.name} @ {describe.stdout.strip()}{dirty}"
    except Exception as exc:  # noqa: BLE001 - provenance is best-effort
        return f"git metadata unavailable: {type(exc).__name__}"


def baseline_payload(
    result: SetResult, *, note: Optional[str] = None
) -> Dict[str, Any]:
    """The committed ``eval/baseline.json`` shape: model pin + date + results."""
    return {
        "model": result.meta.get("model"),
        "pattern": result.meta.get("pattern"),
        "date": (result.meta.get("generated_at") or _now())[:10],
        "git_note": note if note is not None else git_note(),
        "results": result.to_dict(),
    }


def write_baseline(
    result: SetResult, path: Path | str, *, note: Optional[str] = None
) -> Path:
    return _write_json(Path(path), baseline_payload(result, note=note))
