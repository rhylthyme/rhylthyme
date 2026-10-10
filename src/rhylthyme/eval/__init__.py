"""Prompt-evaluation harness for agent-authored programs (``rhylthyme eval-prompts``).

Public surface used by the CLI:

* :func:`load_gold_set` / :class:`GoldProgram` -- read ``gold/<slug>/``.
* :func:`score_program` -- one gold/predicted pair -> :class:`ComponentScores`.
* :func:`score_set` -- a whole gold set -> :class:`SetResult`.
* :func:`write_results` -- ``results.json`` + ``results.md``.

Live mode lives in :mod:`~.harness` (``run_harness``, ``HarnessConfig``),
:mod:`~.llm` (``AnthropicClient``, ``FakeClient``) and :mod:`~.patterns`
(``Pattern``, ``Turn``). They are deliberately not imported here so the
scorer, and every test that uses it, never needs the Anthropic SDK.
"""

from .gold import (
    GoldProgram,
    load_gold_program,
    load_gold_set,
    load_predicted_program,
    load_predicted_set,
    locate_quote,
    locate_quote_fuzzy,
)
from .matcher import StepMatching, StepRef, flatten_steps, match_steps, normalize_name
from .metrics import (
    ComponentScores,
    SetResult,
    critical_path,
    makespan,
    score_program,
    score_set,
    summarize,
)
from .report import render_markdown, render_table, write_results

__all__ = [
    "ComponentScores",
    "GoldProgram",
    "SetResult",
    "StepMatching",
    "StepRef",
    "critical_path",
    "flatten_steps",
    "load_gold_program",
    "load_gold_set",
    "load_predicted_program",
    "load_predicted_set",
    "locate_quote",
    "locate_quote_fuzzy",
    "makespan",
    "match_steps",
    "normalize_name",
    "render_markdown",
    "render_table",
    "score_program",
    "score_set",
    "summarize",
    "write_results",
]
