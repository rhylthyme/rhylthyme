"""
Declared variance factors: asking the executor what explains the variance.

A program may declare ``metadata.varianceFactors`` — the author's guess at what
makes its steps take longer or shorter (turkey weight, oven type, sample count).
The runtime asks for them once, at run start, and stores the answers in the run
record's ``context.userTags``, where calibration and prediction can condition on
them.

The prompt is a pre-flight: it runs on the plain terminal *before*
``curses.wrapper`` takes the screen, never inside the TUI. Every answer is
optional — pressing Enter skips a factor and records nothing for it. For
headless use the answers can be supplied ahead of time:

* ``--factor key=value`` (repeatable) on ``rhylthyme run``,
* ``RHYLTHYME_FACTORS="turkeyKg=6.4,oven=gas"`` in the environment,
* ``--no-factor-prompt`` to skip the prompt entirely (pre-supplied answers are
  still recorded).

Values are validated against the declared ``type``: ``number`` and ``integer``
must parse, ``enum`` must be one of ``values`` (case-insensitively, recorded in
the declared spelling), ``string`` is taken as typed. A bad answer typed at the
prompt is re-asked; a bad answer supplied by flag or environment is reported and
dropped, because a headless run must not block.
"""

import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

FACTORS_ENV = "RHYLTHYME_FACTORS"
VALID_TYPES = ("number", "integer", "enum", "string")


def declared_factors(program: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    The program's declared variance factors, normalised and deduplicated.

    Returns ``[]`` for a program that declares none (the common case), so the
    caller can skip the prompt entirely without a second check.
    """
    if not isinstance(program, dict):
        return []
    metadata = program.get("metadata")
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("varianceFactors")
    if not isinstance(raw, list):
        return []
    factors: List[Dict[str, Any]] = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        if not key or not isinstance(key, str) or key in seen:
            continue
        kind = item.get("type")
        if kind not in VALID_TYPES:
            kind = "string"
        factor = {
            "key": key,
            "label": str(item.get("label") or key),
            "type": kind,
        }
        if kind == "enum":
            values = [str(v) for v in (item.get("values") or [])]
            if not values:
                continue
            factor["values"] = values
        if item.get("unit"):
            factor["unit"] = str(item["unit"])
        factors.append(factor)
        seen.add(key)
    return factors


def coerce_factor_value(factor: Dict[str, Any], raw: Any) -> Tuple[bool, Any, str]:
    """
    Validate ``raw`` against ``factor``: ``(ok, value, message)``.

    ``value`` is the value to store (a float for ``number``, an int for
    ``integer``, the declared spelling for ``enum``, the string otherwise).
    ``message`` explains a rejection and is empty on success.
    """
    kind = factor.get("type", "string")
    text = str(raw).strip()
    if text == "":
        return False, None, "empty"
    if kind == "number":
        try:
            value = float(text)
        except ValueError:
            return False, None, f"'{text}' is not a number"
        if value.is_integer():
            value = int(value)
        return True, value, ""
    if kind == "integer":
        try:
            value = int(text, 10)
        except ValueError:
            return False, None, f"'{text}' is not an integer"
        return True, value, ""
    if kind == "enum":
        values = factor.get("values") or []
        for candidate in values:
            if text.lower() == str(candidate).lower():
                return True, candidate, ""
        return False, None, f"'{text}' is not one of {', '.join(values)}"
    return True, text, ""


def parse_factor_args(pairs: Optional[Sequence[str]]) -> Dict[str, str]:
    """Parse ``["key=value", ...]`` (``--factor``) into ``{key: value}``."""
    out: Dict[str, str] = {}
    for pair in pairs or []:
        if pair is None:
            continue
        text = str(pair)
        if "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        if key:
            out[key] = value.strip()
    return out


def factors_from_env(environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """
    Parse ``RHYLTHYME_FACTORS`` — ``key=value`` pairs separated by commas or
    semicolons — into ``{key: value}``.
    """
    env = environ if environ is not None else os.environ
    raw = env.get(FACTORS_ENV)
    if not raw:
        return {}
    parts: List[str] = []
    for chunk in str(raw).split(","):
        parts.extend(chunk.split(";"))
    return parse_factor_args(parts)


def preset_answers(
    factors: Sequence[Dict[str, Any]],
    supplied: Dict[str, Any],
    warn: Optional[Callable[[str], None]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Coerce pre-supplied answers, dropping (and reporting) the invalid ones.

    Answers for keys the program does not declare are dropped too: the record's
    ``userTags`` are meant to be the declared factors, not a free-for-all.
    """
    by_key = {f["key"]: f for f in factors}
    answers: Dict[str, Any] = {}
    problems: List[str] = []
    for key, raw in supplied.items():
        factor = by_key.get(key)
        if factor is None:
            problems.append(f"{key}: not declared by this program, ignored")
            continue
        ok, value, message = coerce_factor_value(factor, raw)
        if ok:
            answers[key] = value
        else:
            problems.append(f"{key}: {message}, ignored")
    if warn is not None:
        for problem in problems:
            warn(problem)
    return answers, problems


def prompt_for_factors(
    factors: Sequence[Dict[str, Any]],
    preset: Optional[Dict[str, Any]] = None,
    input_fn: Optional[Callable[[str], str]] = None,
    echo_fn: Optional[Callable[[str], None]] = None,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """
    Ask for each declared factor that has no pre-supplied answer.

    Returns the full ``{key: value}`` answer set (presets included). Enter skips
    a factor. An unparseable answer is re-asked up to ``max_attempts`` times and
    then skipped. EOF (a closed stdin) skips the rest, so a piped run never
    hangs.
    """
    # Resolved here, not in the signature, so a test (or a wrapper) can replace
    # builtins.input and still be seen.
    if input_fn is None:
        input_fn = input
    if echo_fn is None:
        echo_fn = print
    answers: Dict[str, Any] = dict(preset or {})
    pending = [f for f in factors if f["key"] not in answers]
    if not pending:
        return answers

    echo_fn("")
    echo_fn(
        "This program declares variance factors — facts about this run that may "
        "explain how long it takes."
    )
    echo_fn("Press Enter to skip any of them.")
    for factor in pending:
        label = factor.get("label") or factor["key"]
        if factor["type"] == "enum":
            hint = "/".join(factor.get("values") or [])
        elif factor.get("unit"):
            hint = factor["unit"]
        else:
            hint = factor["type"]
        prompt = f"  {label} [{hint}]: "
        for _ in range(max(1, int(max_attempts))):
            try:
                raw = input_fn(prompt)
            except (EOFError, KeyboardInterrupt, OSError):
                # No terminal to ask on (piped input, captured stdin): skip
                # the rest rather than block a headless run.
                echo_fn("")
                return answers
            if raw is None or str(raw).strip() == "":
                break
            ok, value, message = coerce_factor_value(factor, raw)
            if ok:
                answers[factor["key"]] = value
                break
            echo_fn(f"    {message}")
    echo_fn("")
    return answers


def collect_factors(
    program: Dict[str, Any],
    factor_args: Optional[Sequence[str]] = None,
    prompt: bool = True,
    environ: Optional[Dict[str, str]] = None,
    input_fn: Optional[Callable[[str], str]] = None,
    echo_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    The whole pre-flight: declared factors, presets (env then flags), prompt.

    ``--factor`` overrides ``RHYLTHYME_FACTORS``. Returns ``{}`` when the
    program declares no factors, so the caller need not special-case it.
    """
    if echo_fn is None:
        echo_fn = print
    factors = declared_factors(program)
    if not factors:
        return {}
    supplied = dict(factors_from_env(environ))
    supplied.update(parse_factor_args(factor_args))
    answers, _problems = preset_answers(factors, supplied, warn=echo_fn)
    if not prompt:
        return answers
    return prompt_for_factors(
        factors, preset=answers, input_fn=input_fn, echo_fn=echo_fn
    )


__all__ = [
    "FACTORS_ENV",
    "declared_factors",
    "coerce_factor_value",
    "parse_factor_args",
    "factors_from_env",
    "preset_answers",
    "prompt_for_factors",
    "collect_factors",
]
