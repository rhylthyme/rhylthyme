"""
Validator checks for schema 0.3.0-alpha `instances` triggers and
step-level `replicates` (PRD prd-per-instance-triggers-barriers §6.1).

This module is maintained by byte-copy in two places (see
tools/check_mirrors.sh); the rhylthyme package is the source of truth:

    rhylthyme-cli-runner/src/rhylthyme/instance_checks.py
    rhylthyme-server/src/rhylthyme_server/rhylthyme/instance_checks.py

It must stay dependency-free: it runs on the UNEXPANDED program (the checks
are about `instances` / `replicates` declarations, which expansion removes)
and is called by both Python validators before replicate expansion.

Codes emitted here (fix hints verbatim from the PRD):

    E_INSTANCES_ON_SINGLE   `instances` set but `stepId` is not replicated
                            or "each"-derived
    E_EACH_WITH_REPLICATES  step has both an `instances: "each"` trigger and
                            its own `replicates`
    E_EACH_COUNT_MISMATCH   compound "each" over steps with different counts
    E_INFLIGHT_GT_COUNT     `replicates.maxInFlight` greater than `count`
    E_INFLIGHT_NO_CHAIN     `maxInFlight` on a serial replicate with no
                            "each" descendants, where it can have no effect
    W_UNBARRIERED_CHAIN     an "each" chain has no "all" barrier and the
                            program has later steps that reference nothing
                            in it (warning)

`I_IMPLICIT_BARRIER` is JS-only (PRD §9 Q5).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_STEP_REF_TYPES = ("afterStep", "afterStepWithBuffer")


@dataclass
class Finding:
    """
    One validator finding, in the shape the JS validator already emits
    (`{code, message, where, fix}`) plus a `severity`.

    ``str(finding)`` renders the legacy one-line form used by the
    ``List[str]`` validator APIs: ``"[CODE] message (fix: …)"``. Findings
    created with :meth:`legacy` render as their bare ``message`` so the
    strings of pre-existing checks stay byte-identical.
    """

    code: str
    message: str
    where: Optional[str] = None
    fix: Optional[str] = None
    severity: str = "error"  # "error" | "warning" | "info"
    legacy: bool = field(default=False, repr=False, compare=False)

    @classmethod
    def legacy_error(
        cls, code: str, message: str, where: Optional[str] = None
    ) -> "Finding":
        """A pre-existing check's finding; renders as the unchanged message."""
        return cls(
            code=code, message=message, where=where, severity="error", legacy=True
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "where": self.where,
            "fix": self.fix,
            "severity": self.severity,
        }

    def __str__(self) -> str:
        if self.legacy:
            return self.message
        text = f"[{self.code}] {self.message}"
        if self.fix:
            text += f" (fix: {self.fix})"
        return text


def _trigger_atoms(trigger: Any) -> List[Dict[str, Any]]:
    """The single triggers making up a trigger (a compound's sub-triggers, or itself)."""
    if not isinstance(trigger, dict):
        return []
    if isinstance(trigger.get("triggers"), list):
        return [t for t in trigger["triggers"] if isinstance(t, dict)]
    return [trigger]


def _step_refs(trigger: Any) -> List[Dict[str, Any]]:
    """Trigger atoms that reference another step by id."""
    return [
        a
        for a in _trigger_atoms(trigger)
        if a.get("type") in _STEP_REF_TYPES and a.get("stepId")
    ]


def _replicate_count(step: Dict[str, Any]) -> Optional[int]:
    """The step's own `replicates.count`, or None if it is not replicated."""
    rep = step.get("replicates")
    if not isinstance(rep, dict):
        return None
    count = rep.get("count", 1)
    try:
        return int(count)
    except (TypeError, ValueError):
        return None


def _max_in_flight(step: Dict[str, Any]) -> Optional[int]:
    """The step's own `replicates.maxInFlight` as an int, or None."""
    rep = step.get("replicates")
    if not isinstance(rep, dict) or "maxInFlight" not in rep:
        return None
    value = rep["maxInFlight"]
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_instances(program: Dict[str, Any]) -> List[Finding]:
    """
    Run the `instances` / `replicates` checks on an UNEXPANDED program.

    Returns findings in a stable order: per-step errors in program order,
    then per-chain warnings. Programs without `instances` or step-level
    `replicates` produce no findings, so calling this on an already
    expanded program is a harmless no-op.
    """
    findings: List[Finding] = []
    tracks = program.get("tracks", []) if isinstance(program, dict) else []
    if not isinstance(tracks, list):
        return findings

    # Steps by id, in authored order; ids referenced by each step.
    steps: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        for step in track.get("steps", []) or []:
            if isinstance(step, dict) and step.get("stepId"):
                sid = step["stepId"]
                if sid not in steps:
                    order.append(sid)
                steps[sid] = step

    # Nothing to do unless some step is replicated or carries `instances`.
    if not any(
        _replicate_count(s) is not None
        or any("instances" in a for a in _trigger_atoms(s.get("startTrigger")))
        for s in steps.values()
    ):
        return findings

    # Instance groups: replicated steps, then (to a fixed point) steps that
    # inherit a count through `instances: "each"`. Value: (count, upstream id).
    groups: Dict[str, tuple] = {}
    for sid in order:
        count = _replicate_count(steps[sid])
        if count is not None:
            groups[sid] = (count, sid)
    each_of: Dict[str, List[str]] = {}  # stepId -> upstream ids referenced with "each"
    for sid in order:
        each_of[sid] = [
            a["stepId"]
            for a in _step_refs(steps[sid].get("startTrigger"))
            if a.get("instances") == "each"
        ]
    changed = True
    while changed:
        changed = False
        for sid in order:
            if sid in groups or not each_of[sid]:
                continue
            resolved = [u for u in each_of[sid] if u in groups]
            if not resolved:
                continue
            # Inherit from the first resolvable upstream; mismatches are
            # reported below (the first group decides, as in the expander).
            groups[sid] = (groups[resolved[0]][0], resolved[0])
            changed = True

    # The `instances: "each"` descendants of a replicated step, in program
    # order: the per-instance chain that hangs off it. Used by the
    # `maxInFlight` checks and by W_UNBARRIERED_CHAIN below.
    def each_chain(root: str) -> List[str]:
        chain: List[str] = []
        frontier = [root]
        while frontier:
            cur = frontier.pop(0)
            for sid in order:
                if sid in chain or sid == root:
                    continue
                if cur in each_of[sid] and sid in groups:
                    chain.append(sid)
                    frontier.append(sid)
        return chain

    # ---- per-step errors ------------------------------------------------
    for sid in order:
        step = steps[sid]
        where = f"step:{sid}"
        refs = _step_refs(step.get("startTrigger"))
        own_count = _replicate_count(step)

        # E_INSTANCES_ON_SINGLE: `instances` on a reference to a step that is
        # neither replicated nor "each"-derived. Dangling references are left
        # to the existing dangling-reference check.
        for atom in refs:
            if "instances" not in atom:
                continue
            target = atom["stepId"]
            if target not in steps or target in groups:
                continue
            findings.append(
                Finding(
                    code="E_INSTANCES_ON_SINGLE",
                    message=(
                        f"Step \"{sid}\" uses instances: \"{atom['instances']}\" on "
                        f'"{target}", which is not replicated.'
                    ),
                    where=where,
                    fix=f"remove `instances`, or add `replicates` to `{target}`",
                )
            )

        each_refs = [
            a for a in refs if a.get("instances") == "each" and a["stepId"] in groups
        ]

        # E_EACH_WITH_REPLICATES: the count is inherited, never redeclared.
        if own_count is not None and each_refs:
            upstream = each_refs[0]["stepId"]
            n = groups[upstream][0]
            findings.append(
                Finding(
                    code="E_EACH_WITH_REPLICATES",
                    message=(
                        f'Step "{sid}" has both an instances: "each" trigger '
                        f'(on "{upstream}") and its own `replicates`.'
                    ),
                    where=where,
                    fix=f"drop `replicates` on `{sid}`; it inherits `{n}` instances from `{upstream}`",
                )
            )

        # E_EACH_COUNT_MISMATCH: compound "each" over groups of different size.
        if len(each_refs) > 1:
            first = each_refs[0]["stepId"]
            n = groups[first][0]
            for atom in each_refs[1:]:
                other = atom["stepId"]
                m = groups[other][0]
                if m != n:
                    findings.append(
                        Finding(
                            code="E_EACH_COUNT_MISMATCH",
                            message=(
                                f'Step "{sid}" pairs instances of "{first}" '
                                f'(count {n}) with "{other}" (count {m}).'
                            ),
                            where=where,
                            fix=f"both upstream steps must have `count: {n}`",
                        )
                    )

        # ---- `replicates.maxInFlight` ------------------------------------
        limit = _max_in_flight(step)
        if limit is not None:
            n = own_count if own_count is not None else 1
            # E_INFLIGHT_GT_COUNT: more instances in flight than exist.
            if limit > n:
                findings.append(
                    Finding(
                        code="E_INFLIGHT_GT_COUNT",
                        message=(
                            f'Step "{sid}" has maxInFlight {limit} but only '
                            f"{n} instance{'s' if n != 1 else ''}."
                        ),
                        where=where,
                        fix=f"set `maxInFlight` <= `{n}` or omit it",
                    )
                )
            # E_INFLIGHT_NO_CHAIN: serial instances already cannot overlap,
            # so with no "each" descendants the limit can never bite.
            mode = (step.get("replicates") or {}).get("mode", "parallel")
            if mode == "serial" and not each_chain(sid):
                findings.append(
                    Finding(
                        code="E_INFLIGHT_NO_CHAIN",
                        message=(
                            f'Step "{sid}" sets maxInFlight but has mode '
                            f'"serial" and no instances: "each" descendants, '
                            f"so no instance is ever held back."
                        ),
                        where=where,
                        fix=(
                            "`maxInFlight` has no effect here; remove it or "
                            "chain a per-instance step"
                        ),
                    )
                )

    # ---- W_UNBARRIERED_CHAIN --------------------------------------------
    # For every replicated root X with at least one "each" descendant: the
    # chain is X's "each" descendants. It is barriered when some step
    # references a chain member with instances "all" (explicit) or with no
    # `instances` at all (the implicit all-join). "Later steps" are steps
    # outside the chain that are not upstream of X or of any chain member
    # (so the other root of a compound "each" is not "later") and reference
    # nothing in the chain; only then is the missing barrier worth a warning.
    def upstream_closure(*start: str) -> set:
        seen: set = set()
        stack = list(start)
        while stack:
            cur = stack.pop()
            for a in _step_refs(steps.get(cur, {}).get("startTrigger")):
                ref = a["stepId"]
                if ref in steps and ref not in seen:
                    seen.add(ref)
                    stack.append(ref)
        return seen

    roots = [sid for sid in order if _replicate_count(steps[sid]) is not None]
    for root in roots:
        chain = each_chain(root)
        if not chain:
            continue
        chain_set = set(chain)
        barriered = False
        later: List[str] = []
        upstream = upstream_closure(root, *chain)
        for sid in order:
            if sid == root or sid in chain_set:
                continue
            refs = _step_refs(steps[sid].get("startTrigger"))
            touches = [a for a in refs if a["stepId"] in chain_set]
            if any(a.get("instances", "all") == "all" for a in touches):
                barriered = True
                break
            if not touches and sid not in upstream:
                later.append(sid)
        if barriered or not later:
            continue
        findings.append(
            Finding(
                code="W_UNBARRIERED_CHAIN",
                message=(
                    f'The instances: "each" chain from "{root}" '
                    f"({', '.join(chain)}) has no instances: \"all\" barrier, "
                    f"but later steps ({', '.join(later)}) do not wait for it."
                ),
                where=f"step:{root}",
                fix='add a step with `instances:"all"` if later work should wait for every instance',
                severity="warning",
            )
        )

    return findings
