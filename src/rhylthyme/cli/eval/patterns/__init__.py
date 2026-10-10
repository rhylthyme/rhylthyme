"""Prompt patterns the live harness can run.

A pattern turns one gold program's source and context into a sequence of
model turns and ends with a program JSON (or an error). Patterns are
registered by name in :data:`PATTERNS`; ``rhylthyme eval-prompts
--patterns a,b`` looks them up there. Phase 2 ships ``baseline``; Phase 3
adds ``four-turn``.

The interface is three members:

``name``
    Registry key, also written to ``results.json``.
``render(gold) -> list[Turn]``
    The turns to send, in order. A :class:`Turn` carries the user message
    for that turn (as text, or as a callable taking the list of prior
    turn replies so a later turn can quote an earlier answer) plus the
    system prompt for the request.
``parse(turn_outputs) -> program | None``
    Given every turn's reply text, in order, return the program JSON.
    The default takes the first parseable program in the last reply.

:meth:`Pattern.run` is the default driver and is what the harness calls:
it walks the turns as one growing conversation (each reply is appended as
an assistant message, so later turns see earlier ones), parses, and then
runs the shared validate-and-repair tail :func:`finish_with_fix_loop` so
fix iterations are comparable across patterns. Override ``run`` only for
control flow the turn list cannot express::

    class FourTurnPattern(Pattern):
        name = "four-turn"

        def render(self, gold):
            return [
                Turn("T1 read-back", READ_BACK.format(source=gold.source_text),
                     system=SYSTEM),
                Turn("T2 model check", MODEL_CHECK, system=SYSTEM),
                Turn("T3 extraction", scenario(gold), system=SYSTEM),
                Turn("T4 relationships",
                     lambda prior: RELATIONSHIPS.format(steps=prior[2]),
                     system=SYSTEM),
            ]

``session.complete`` is the only way a pattern talks to the model: it
routes through the response cache, records the transcript and counts
tokens.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Type,
    Union,
)

from rhylthyme.eval.gold import GoldProgram, Program
from rhylthyme.eval.metrics import validate_python

from .extract import extract_program

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..harness import Session

#: A turn's user message: fixed text, or a function of the prior replies.
TurnContent = Union[str, Callable[[List[str]], str]]


@dataclass
class Turn:
    """One request in a pattern: a user message plus the system prompt.

    ``content`` is either the message text or a callable taking the list
    of replies to the turns before it (so T4 can quote T3's answer).
    ``slots`` records what was filled in, for the transcript.
    """

    name: str
    content: TurnContent
    system: Optional[str] = None
    slots: Dict[str, Any] = field(default_factory=dict)

    def text(self, prior_outputs: Sequence[str] = ()) -> str:
        """The user message for this turn given the replies so far."""
        if callable(self.content):
            return self.content(list(prior_outputs))
        return self.content


@dataclass
class PatternResult:
    """What a pattern hands back to the harness for scoring."""

    program: Optional[Program] = None
    fix_iterations: int = 0
    error: Optional[str] = None
    validation_errors: List[str] = field(default_factory=list)
    # Free slot for pattern-specific numbers (T1/T2 2/1/0 scores in Phase 3).
    extras: Dict[str, Any] = field(default_factory=dict)


class Pattern(ABC):
    """One way of prompting a model for a program."""

    #: Registry key and the value written to ``results.json``.
    name: str = ""

    def render(self, gold: GoldProgram) -> List[Turn]:
        """The turns to send for ``gold``, in order."""
        raise NotImplementedError

    def parse(self, turn_outputs: Sequence[str]) -> Optional[Program]:
        """The program JSON carried by the turn replies (last reply wins)."""
        for reply in reversed(list(turn_outputs)):
            program = extract_program(reply)
            if program is not None:
                return program
        return None

    def run(self, gold: GoldProgram, session: "Session") -> PatternResult:
        """Walk :meth:`render`'s turns, then parse and repair the result."""
        turns = self.render(gold)
        if not turns:
            return PatternResult(error="pattern rendered no turns")
        history: List[Dict[str, Any]] = []
        outputs: List[str] = []
        system: Optional[str] = None
        for turn in turns:
            system = turn.system
            history.append({"role": "user", "content": turn.text(outputs)})
            reply = session.complete(history, system=system).text
            outputs.append(reply)
            if turn is not turns[-1]:
                history.append({"role": "assistant", "content": reply})
        result = finish_with_fix_loop(
            session,
            history,
            outputs[-1],
            system=system,
            parse=lambda reply: self.parse(outputs[:-1] + [reply]),
        )
        result.extras.setdefault("turns", len(turns))
        result.extras.setdefault(
            "turn_slots", {turn.name: turn.slots for turn in turns if turn.slots}
        )
        return result


FIX_REQUEST = (
    "validate_program reported {count} error(s):\n{errors}\n\n"
    "Fix every error and reply with the complete corrected program JSON "
    "in a single ```json fenced block."
)

NO_JSON_FINDING = "no JSON object with a `tracks` array was found in the reply"


def validator_findings(program: Optional[Program]) -> List[str]:
    """Findings for the fix loop: extraction failure or validator errors."""
    if program is None:
        return [NO_JSON_FINDING]
    ok, errors = validate_python(program)
    return [] if ok else list(errors)


def finish_with_fix_loop(
    session: "Session",
    messages: Sequence[Dict[str, Any]],
    reply: str,
    *,
    system: Optional[str] = None,
    max_iterations: Optional[int] = None,
    parse: Optional[Callable[[str], Optional[Program]]] = None,
) -> PatternResult:
    """Extract a program from ``reply``; if it fails validation, ask for fixes.

    ``messages`` is the conversation so far (ending with the user turn
    that produced ``reply``). Each iteration appends the assistant reply
    and a user message carrying the validator findings, then calls the
    model again. Stops when the program validates or after
    ``max_iterations`` (default ``session.config.max_fix_iterations``).
    """
    if max_iterations is None:
        max_iterations = session.config.max_fix_iterations
    if parse is None:
        parse = extract_program
    history: List[Dict[str, Any]] = list(messages)
    program = parse(reply)
    findings = validator_findings(program)
    iterations = 0
    while findings and iterations < max_iterations:
        iterations += 1
        history.append({"role": "assistant", "content": reply})
        history.append(
            {
                "role": "user",
                "content": FIX_REQUEST.format(
                    count=len(findings),
                    errors="\n".join(f"- {finding}" for finding in findings),
                ),
            }
        )
        reply = session.complete(history, system=system).text
        program = parse(reply)
        findings = validator_findings(program)
    result = PatternResult(
        program=program,
        fix_iterations=iterations,
        validation_errors=findings,
    )
    if program is None:
        result.error = NO_JSON_FINDING
    elif findings:
        result.error = "program failed validation after {} fix iteration(s): {}".format(
            iterations, "; ".join(findings[:3])
        )
    return result


from .baseline import BaselinePattern  # noqa: E402 - needs Pattern defined
from .four_turn import FourTurnPattern  # noqa: E402 - needs Pattern defined

PATTERNS: Dict[str, Type[Pattern]] = {
    BaselinePattern.name: BaselinePattern,
    FourTurnPattern.name: FourTurnPattern,
}


def get_pattern(name: str) -> Pattern:
    try:
        return PATTERNS[name]()
    except KeyError:
        raise KeyError(
            f"unknown pattern {name!r}; known: {', '.join(sorted(PATTERNS))}"
        ) from None


__all__ = [
    "PATTERNS",
    "BaselinePattern",
    "FourTurnPattern",
    "Pattern",
    "PatternResult",
    "Turn",
    "extract_program",
    "finish_with_fix_loop",
    "get_pattern",
    "validator_findings",
]
