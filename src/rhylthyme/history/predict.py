"""
History-based duration prediction: the identical-then-similar lookup.

Badosa et al. (2019) §3 predict a job's run time by looking first for
*identical* executions, then for *similar* ones, fitting a regression on the
predictor variables whose Pearson correlation with the outcome clears a
threshold. :func:`predict_durations` is that lookup over Rhylthyme run
records, per authored step:

1. **identical** — runs of the same ``programVersion``, the same
   ``environmentId`` and the same answers to the program's declared variance
   factors. Their median is the prediction and P10/P90 the interval. The
   caller's own runs are preferred whenever there are enough of them (PRD open
   question 3), which is what ``source`` reports.
2. **model** — otherwise every usable measurement of the same program, fitted
   by ordinary least squares on the numeric factors and one-hot enums that
   survive the correlation filter, evaluated at the caller's factor values.
   Residual P10/P90 give the interval. With too few measurements, or when no
   factor survives, the same basis falls back to the median (``method:
   "median"``, ``factors: []``).
3. **none** — no measurement at all, or the inferentiality report says the
   executor decides this step's length, so there is no number to give.

.. rubric:: Two copies, one module

This file exists twice, byte for byte: ``rhylthyme_server/rhylthyme/predict.py``
is the source of truth and ``rhylthyme/history/predict.py`` is its
copy, checked by ``tools/check_mirrors.sh``. rhylthyme-server must not import
rhylthyme (the same rule that produced the copied prompt templates),
and Phase 7 needs the lookup inside the runner, so the module is standalone:
it imports nothing from either package and carries its own copy of the
usable-run filter and the percentile definition. ``tests/test_predict.py``
asserts that copy agrees with :mod:`rhylthyme.history.usable` on
every case of the shared usable-run fixture, so the duplication cannot drift
silently.

The JavaScript twin is ``predictDurations`` in ``mcp-api/history.js``; the
parity fixture both sides read is
``rhylthyme-cli-runner/tests/fixtures/history/predict-cases.json``.

    from rhylthyme_server.rhylthyme.predict import predict_durations

    predictions = predict_durations(
        program, records,
        user_tags={"turkeyKg": 7}, user_id="me",
        program_version="sha256:...",
    )
    predictions["turkey-roast"]
    # {'seconds': 1230.4, 'low': ..., 'high': ..., 'basis': 'model', 'n': 20,
    #  'source': 'all', 'method': 'ols',
    #  'factors': [{'key': 'turkeyKg', 'coef': 90.0}]}
"""

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

DEFAULT_MIN_IDENTICAL = 3
DEFAULT_MIN_MODEL = 8
DEFAULT_CORR_THRESHOLD = 0.3

BASIS_IDENTICAL = "identical"
BASIS_MODEL = "model"
BASIS_NONE = "none"
METHOD_OLS = "ols"
METHOD_MEDIAN = "median"
EXECUTOR_CONTROLLED = "executor-controlled"

# Context keys that every run has whether or not the author declared any
# variance factors, so a program with no `metadata.varianceFactors` can still
# be conditioned on how many people and how many portions.
IMPLICIT_FACTORS = ("serves", "actors")

# Reason codes of the inlined usable-run filter. They are the strings
# rhylthyme.history.usable and mcp-api/history.js use, character for
# character, because callers compare them.
OUTCOME_NOT_COMPLETED = "outcome-not-completed"
CLOCK_NOT_WALL = "clock-not-wall"
SPEED_NOT_1 = "speed-not-1"


# ------------------------------------------------- inlined usable-run filter
#
# A copy, not an import: see "Two copies, one module" above. Kept to the
# minimum prediction needs — whether a run is a measurement, and the observed
# duration of the steps within it that are.


def is_usable_run(record: Any) -> Tuple[bool, Optional[str]]:
    """``(usable, reason)`` for one run record; the reason is the first failure."""
    if not isinstance(record, dict):
        return False, OUTCOME_NOT_COMPLETED
    if record.get("outcome") != "completed":
        return False, OUTCOME_NOT_COMPLETED
    runtime = record.get("runtime")
    if not isinstance(runtime, dict):
        runtime = {}
    if runtime.get("clockMode") != "wall":
        return False, CLOCK_NOT_WALL
    speed = runtime.get("speed", 1)
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        return False, SPEED_NOT_1
    if speed != 1.0:
        return False, SPEED_NOT_1
    return True, None


def is_measured_step(step: Any) -> bool:
    """
    Is ``step``'s observed duration a measurement of the executor?

    True when the step started and ended, was never paused, is not ``fixed``
    (a fixed step's length only confirms its timer) and was ended by a person.
    """
    if not isinstance(step, dict):
        return False
    planned = step.get("planned") or {}
    actual = step.get("actual") or {}
    if actual.get("start") is None or actual.get("end") is None:
        return False
    if (step.get("pausedSeconds") or 0) > 0:
        return False
    if planned.get("durationType") == "fixed":
        return False
    return step.get("endedBy") == "executor"


def step_duration(step: Any) -> Optional[float]:
    """Observed duration of ``step`` in seconds, or ``None``."""
    actual = (step or {}).get("actual") or {}
    start, end = actual.get("start"), actual.get("end")
    if start is None or end is None:
        return None
    return float(end) - float(start)


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """
    Linear-interpolated percentile (``q`` in 0..1): position ``q * (n - 1)`` in
    the sorted sample. NumPy's default, ``report.percentile``'s definition and
    ``durationStats``' in ``mcp-api/history.js``, all the same.
    """
    ordered = sorted(float(v) for v in values if v is not None)
    n = len(ordered)
    if n == 0:
        return None
    if n == 1:
        return ordered[0]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


# --------------------------------------------------------------- small utils


def _round3(value: Any) -> Optional[float]:
    """Round to 3 decimals, half away from zero, identically in both languages."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if number < 0:
        return -math.floor(-number * 1000 + 0.5) / 1000
    return math.floor(number * 1000 + 0.5) / 1000


def _num(value: Any) -> Optional[float]:
    """``value`` as a finite number, or None. Booleans are categories."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (dict, list, tuple, set)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def declared_factor_keys(program: Any) -> List[str]:
    """The keys of ``metadata.varianceFactors``, in declaration order."""
    metadata = program.get("metadata") if isinstance(program, dict) else None
    raw = metadata.get("varianceFactors") if isinstance(metadata, dict) else None
    keys: List[str] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if isinstance(key, str) and key and key not in keys:
                keys.append(key)
    return keys


def program_step_ids(program: Any) -> List[str]:
    """Authored step ids of ``program``, in program order, deduplicated."""
    ids: List[str] = []
    tracks = program.get("tracks") if isinstance(program, dict) else None
    for track in tracks or []:
        if not isinstance(track, dict):
            continue
        for step in track.get("steps") or []:
            if not isinstance(step, dict):
                continue
            sid = step.get("instanceOf") or step.get("stepId")
            if isinstance(sid, str) and sid and sid not in ids:
                ids.append(sid)
    return ids


def _context(record: Any) -> Dict[str, Any]:
    ctx = record.get("context") if isinstance(record, dict) else None
    return ctx if isinstance(ctx, dict) else {}


def _user_tags(record: Any) -> Dict[str, Any]:
    tags = _context(record).get("userTags")
    return tags if isinstance(tags, dict) else {}


def record_user_id(record: Any) -> Optional[str]:
    """
    Who ran ``record``, or None.

    The runs schema is closed, so the owner lives in the open ``context``
    object; a ``userId`` / ``user_id`` alongside the record (the shape the web
    API returns) is accepted too.
    """
    if not isinstance(record, dict):
        return None
    ctx = _context(record)
    for candidate in (
        ctx.get("userId"),
        ctx.get("user_id"),
        record.get("userId"),
        record.get("user_id"),
    ):
        if candidate is not None and candidate != "":
            return str(candidate)
    return None


def _env_id(value: Any) -> Optional[str]:
    """environmentId normalised so that "" and missing both mean None."""
    return None if value is None or value == "" else str(value)


def _flat_factors(
    tags: Optional[Dict[str, Any]], implicit: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    The flat ``{key: value}`` a record's (or the caller's) factors amount to:
    the implicit context factors first, overridden by the declared userTags.
    """
    out: Dict[str, Any] = {}
    for key in IMPLICIT_FACTORS:
        value = (implicit or {}).get(key)
        if value is not None and value != "":
            out[key] = value
    for key in sorted((tags or {}).keys()):
        value = (tags or {})[key]
        if value is not None and value != "":
            out[key] = value
    return out


def _record_factors(record: Any) -> Dict[str, Any]:
    ctx = _context(record)
    return _flat_factors(
        _user_tags(record), {"serves": ctx.get("serves"), "actors": ctx.get("actors")}
    )


def _context_factors(
    program: Any, user_tags: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    metadata = program.get("metadata") if isinstance(program, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    implicit = {
        "serves": metadata.get("serves"),
        "actors": program.get("actors") if isinstance(program, dict) else None,
    }
    return _flat_factors(user_tags, implicit)


def _same_factor(a: Any, b: Any) -> bool:
    """Do two factor values mean the same thing? Numbers numerically, else text."""
    an, bn = _num(a), _num(b)
    if an is not None and bn is not None:
        return an == bn
    if a is None or a == "":
        return b is None or b == ""
    if b is None or b == "":
        return False
    return str(a).strip().lower() == str(b).strip().lower()


def _verdict_map(verdicts: Any) -> Dict[str, Any]:
    """``{stepId: verdict}`` from a report, a mapping, or a list of rows."""
    out: Dict[str, Any] = {}
    if not verdicts:
        return out
    rows = None
    if isinstance(verdicts, (list, tuple)):
        rows = list(verdicts)
    elif hasattr(verdicts, "steps"):
        rows = list(getattr(verdicts, "steps") or [])
    elif isinstance(verdicts, dict) and isinstance(verdicts.get("steps"), list):
        rows = verdicts["steps"]
    if rows is not None:
        for row in rows:
            sid = (
                row.get("stepId")
                if isinstance(row, dict)
                else getattr(row, "stepId", None)
            )
            if not sid:
                continue
            verdict = (
                row.get("verdict")
                if isinstance(row, dict)
                else getattr(row, "verdict", None)
            )
            out[str(sid)] = verdict
        return out
    if isinstance(verdicts, dict):
        for key, value in verdicts.items():
            out[str(key)] = value
    return out


# ----------------------------------------------------------- linear algebra


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    """Pearson correlation of two equal-length samples, or None when undefined."""
    n = min(len(xs), len(ys))
    if n < 2:
        return None
    mx = my = 0.0
    for i in range(n):
        mx += xs[i]
        my += ys[i]
    mx /= n
    my /= n
    sxy = sxx = syy = 0.0
    for i in range(n):
        dx = xs[i] - mx
        dy = ys[i] - my
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0 or syy <= 0:
        return None
    r = sxy / math.sqrt(sxx * syy)
    return r if math.isfinite(r) else None


def ols_fit(
    rows: Sequence[Sequence[float]], y: Sequence[float]
) -> Optional[List[float]]:
    """
    Ordinary least squares with an intercept, by the normal equations
    ``(X'X)b = X'y`` solved with Gauss-Jordan elimination and partial pivoting.

    ``rows`` are the design rows WITHOUT the intercept column, which is
    prepended here. Returns ``[intercept, *coefficients]``, or None when the
    system is singular (a collinear or constant column) — the caller then falls
    back to the median rather than reporting a fitted number it cannot trust.
    """
    n = len(rows)
    if n == 0 or n != len(y):
        return None
    p = len(rows[0])
    m = p + 1
    if n < m + 1:
        return None
    # Augmented normal matrix [X'X | X'y].
    a = [[0.0] * (m + 1) for _ in range(m)]
    for k in range(n):
        row = [1.0] + [float(v) for v in rows[k]]
        for i in range(m):
            for j in range(m):
                a[i][j] += row[i] * row[j]
            a[i][m] += row[i] * float(y[k])
    # Scale-free pivot tolerance: the largest magnitude anywhere in X'X.
    scale = 0.0
    for i in range(m):
        for j in range(m):
            scale = max(scale, abs(a[i][j]))
    if scale == 0:
        return None
    tol = 1e-10 * scale
    for col in range(m):
        piv = col
        for r in range(col + 1, m):
            if abs(a[r][col]) > abs(a[piv][col]):
                piv = r
        if abs(a[piv][col]) < tol:
            return None
        if piv != col:
            a[piv], a[col] = a[col], a[piv]
        d = a[col][col]
        for j in range(col, m + 1):
            a[col][j] /= d
        for r in range(m):
            if r == col:
                continue
            f = a[r][col]
            if f == 0:
                continue
            for j in range(col, m + 1):
                a[r][j] -= f * a[col][j]
    beta = []
    for i in range(m):
        if not math.isfinite(a[i][m]):
            return None
        beta.append(a[i][m])
    return beta


# ------------------------------------------------------------- the columns


def _candidate_columns(samples: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    The candidate predictor columns for one step's measurements.

    A numeric factor becomes one column; a categorical factor becomes one
    column per observed value (one-hot). A factor missing from any of the
    measurements is dropped rather than imputed, because an imputed zero would
    be a measurement the history does not contain.
    """
    numeric: Dict[str, int] = {}
    categorical: Dict[str, int] = {}
    for sample in samples:
        for key, value in sample["factors"].items():
            if _num(value) is not None:
                numeric[key] = numeric.get(key, 0) + 1
            else:
                categorical[key] = categorical.get(key, 0) + 1
    n = len(samples)
    columns: List[Dict[str, Any]] = []
    for key in sorted(numeric):
        if numeric[key] != n or key in categorical:
            continue
        columns.append(
            {
                "key": key,
                "kind": "numeric",
                "get": (lambda k: lambda f: _num(f.get(k)))(key),
            }
        )
    for key in sorted(categorical):
        if categorical[key] != n or key in numeric:
            continue
        values: List[str] = []
        for sample in samples:
            text = str(sample["factors"][key])
            if text not in values:
                values.append(text)
        for value in sorted(values):
            columns.append(
                {
                    "key": key + "=" + value,
                    "kind": "enum",
                    "get": (
                        lambda k, v: lambda f: (
                            1.0 if f.get(k) is not None and str(f.get(k)) == v else 0.0
                        )
                    )(key, value),
                }
            )
    return columns


def _median_prediction(
    values: Sequence[float],
    basis: str,
    n: int,
    source: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out = {
        "seconds": _round3(percentile(values, 0.5)),
        "low": _round3(percentile(values, 0.10)),
        "high": _round3(percentile(values, 0.90)),
        "basis": basis,
        "n": n,
        "source": source,
        "method": METHOD_MEDIAN,
    }
    out.update(extra or {})
    return out


def _model_prediction(
    samples: Sequence[Dict[str, Any]],
    at: Dict[str, Any],
    min_model: int,
    corr_threshold: float,
) -> Optional[Dict[str, Any]]:
    """Fit and evaluate one step's model, or None when it cannot be fitted."""
    n = len(samples)
    if n < min_model:
        return None
    y = [float(s["value"]) for s in samples]
    kept: List[Dict[str, Any]] = []
    for col in _candidate_columns(samples):
        xs = [col["get"](s["factors"]) for s in samples]
        if any(x is None or not math.isfinite(x) for x in xs):
            continue
        r = pearson(xs, y)
        if r is None or abs(r) < corr_threshold:
            continue
        here = col["get"](at)
        if here is None or not math.isfinite(here):
            continue
        kept.append({"col": col, "xs": xs, "here": here})
    if not kept:
        return None
    rows = [[k["xs"][i] for k in kept] for i in range(n)]
    beta = ols_fit(rows, y)
    if beta is None:
        return None
    predicted = beta[0]
    for j, k in enumerate(kept):
        predicted += beta[j + 1] * k["here"]
    if not math.isfinite(predicted):
        return None
    residuals = []
    for i in range(n):
        fit = beta[0]
        for j, k in enumerate(kept):
            fit += beta[j + 1] * k["xs"][i]
        residuals.append(y[i] - fit)
    lo_residual = percentile(residuals, 0.10)
    hi_residual = percentile(residuals, 0.90)
    assert lo_residual is not None and hi_residual is not None  # residuals is non-empty
    lo = predicted + lo_residual
    hi = predicted + hi_residual
    return {
        "seconds": _round3(max(0.0, predicted)),
        "low": _round3(max(0.0, min(lo, predicted))),
        "high": _round3(max(hi, predicted)),
        "factors": [
            {"key": k["col"]["key"], "coef": _round3(beta[j + 1])}
            for j, k in enumerate(kept)
        ],
    }


# ---------------------------------------------------------------- the lookup


def predict_durations(
    program: Any,
    records: Optional[Sequence[Dict[str, Any]]],
    *,
    environment_id: Any = None,
    user_tags: Optional[Dict[str, Any]] = None,
    user_id: Any = None,
    program_version: Any = None,
    min_identical: int = DEFAULT_MIN_IDENTICAL,
    min_model: int = DEFAULT_MIN_MODEL,
    corr_threshold: float = DEFAULT_CORR_THRESHOLD,
    verdicts: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Predict each step's duration from run history.

    Args:
        program: the program being planned, read for ``programId``, its step
            ids, ``metadata.varianceFactors`` and the implicit serves/actors.
        records: run records, any ``programId`` (foreign ones are ignored) and
            any quality (only usable runs are read — see :func:`is_usable_run`).
        environment_id, user_tags, user_id, program_version: the context being
            planned for. ``program_version`` left None means the version is not
            constrained, which makes "identical" a looser match than Badosa's.
        min_identical: measurements needed before an identical-context median
            is reported, and before the caller's own runs are used in
            preference to everyone's.
        min_model: measurements needed before a model is fitted at all.
        corr_threshold: ``|Pearson r|`` a factor must clear to enter the model.
        verdicts: inferentiality verdicts (a ``{stepId: verdict}`` mapping, a
            list of rows, or a whole :class:`~.report.Report`): an
            ``executor-controlled`` step is never predicted.

    Returns:
        ``{stepId: {seconds, low, high, basis, n, source, method, factors?,
        reason?}}`` keyed by authored stepId. Steps with no history at all are
        absent; ``n`` counts measurements, not runs, so a replicated step
        contributes one per instance.
    """
    verdict_by_step = _verdict_map(verdicts)
    step_ids = program_step_ids(program)
    known = set(step_ids)
    program_id = program.get("programId") if isinstance(program, dict) else None

    at = _context_factors(program, user_tags)
    want_env = _env_id(environment_id)
    want_version = None if program_version in (None, "") else str(program_version)
    match_keys = declared_factor_keys(program)
    if not match_keys:
        match_keys = sorted((user_tags or {}).keys())
    want_user = None if user_id in (None, "") else str(user_id)
    want_tags = user_tags or {}

    def identical_context(record: Dict[str, Any]) -> bool:
        if (
            want_version is not None
            and str(record.get("programVersion") or "") != want_version
        ):
            return False
        if _env_id(record.get("environmentId")) != want_env:
            return False
        tags = _user_tags(record)
        for key in match_keys:
            if not _same_factor(tags.get(key), want_tags.get(key)):
                return False
        return True

    # Every measurement of every step, with the context it was measured in.
    by_step: Dict[str, List[Dict[str, Any]]] = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        if program_id and record.get("programId") and record["programId"] != program_id:
            continue
        if not is_usable_run(record)[0]:
            continue
        factors = _record_factors(record)
        identical = identical_context(record)
        owner = record_user_id(record)
        for step in record.get("steps") or []:
            if not is_measured_step(step):
                continue
            sid = step.get("stepId")
            if not sid or (known and sid not in known):
                continue
            value = step_duration(step)
            if value is None or not math.isfinite(value):
                continue
            by_step.setdefault(sid, []).append(
                {
                    "value": value,
                    "factors": factors,
                    "identical": identical,
                    "owner": owner,
                }
            )

    out: Dict[str, Dict[str, Any]] = {}
    order = step_ids if step_ids else list(by_step.keys())
    for sid in order:
        samples = by_step.get(sid, [])
        if verdict_by_step.get(sid) == EXECUTOR_CONTROLLED:
            out[sid] = {
                "seconds": None,
                "low": None,
                "high": None,
                "basis": BASIS_NONE,
                "n": len(samples),
                "source": "all",
                "reason": EXECUTOR_CONTROLLED,
            }
            continue
        if not samples:
            continue

        # 1. Identical context, the caller's own runs first.
        same_context = [s for s in samples if s["identical"]]
        chosen: Optional[List[Dict[str, Any]]] = None
        source: Optional[str] = None
        if want_user is not None:
            mine = [s for s in same_context if s["owner"] == want_user]
            if len(mine) >= min_identical:
                chosen, source = mine, "user"
        if chosen is None and len(same_context) >= min_identical:
            chosen, source = same_context, "all"
        if chosen is not None:
            assert source is not None  # set together with `chosen`
            out[sid] = _median_prediction(
                [s["value"] for s in chosen], BASIS_IDENTICAL, len(chosen), source
            )
            continue

        # 2. Similar context: a model over every measurement of this program.
        model = _model_prediction(samples, at, min_model, corr_threshold)
        if model is not None:
            out[sid] = {
                "seconds": model["seconds"],
                "low": model["low"],
                "high": model["high"],
                "basis": BASIS_MODEL,
                "n": len(samples),
                "source": "all",
                "method": METHOD_OLS,
                "factors": model["factors"],
            }
            continue
        out[sid] = _median_prediction(
            [s["value"] for s in samples],
            BASIS_MODEL,
            len(samples),
            "all",
            {"factors": []},
        )
    return out


def predicted_seconds(
    predictions: Optional[Dict[str, Dict[str, Any]]]
) -> Dict[str, float]:
    """``{stepId: seconds}`` for every step the lookup actually put a number on."""
    out: Dict[str, float] = {}
    for sid, prediction in (predictions or {}).items():
        if not isinstance(prediction, dict):
            continue
        value = _num(prediction.get("seconds"))
        if value is not None:
            out[sid] = value
    return out


__all__ = [
    "predict_durations",
    "predicted_seconds",
    "is_usable_run",
    "is_measured_step",
    "step_duration",
    "percentile",
    "pearson",
    "ols_fit",
    "declared_factor_keys",
    "program_step_ids",
    "record_user_id",
    "DEFAULT_MIN_IDENTICAL",
    "DEFAULT_MIN_MODEL",
    "DEFAULT_CORR_THRESHOLD",
    "BASIS_IDENTICAL",
    "BASIS_MODEL",
    "BASIS_NONE",
    "METHOD_OLS",
    "METHOD_MEDIAN",
    "EXECUTOR_CONTROLLED",
]
