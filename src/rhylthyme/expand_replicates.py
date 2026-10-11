#!/usr/bin/env python3
"""
Replicate expansion for Rhylthyme programs.

Expands `replicates` objects on tracks and steps into flat tracks/steps
BEFORE validation and visualization. Also converts legacy `batch_size`/`stagger`
fields to the `replicates` format.
"""

import copy
import re
from typing import Any, Dict, List


def _parse_delay(delay) -> float:
    """Parse a delay value (number or time string like '1m30s') into seconds."""
    if delay is None:
        return 0.0
    if isinstance(delay, (int, float)):
        return float(delay)
    if isinstance(delay, str):
        s = str(delay).strip()
        if s.isdigit():
            return float(s)
        try:
            return float(s)
        except ValueError:
            pass
        total = 0.0
        hour_match = re.search(r"(\d+)h", s)
        if hour_match:
            total += int(hour_match.group(1)) * 3600
        minute_match = re.search(r"(\d+)m", s)
        if minute_match:
            total += int(minute_match.group(1)) * 60
        second_match = re.search(r"(\d+)s", s)
        if second_match:
            total += int(second_match.group(1))
        return total
    return 0.0


def _suffix_step_refs(
    steps: List[Dict], suffix: str, original_step_ids: set
) -> List[Dict]:
    """
    Suffix all stepIds and intra-track afterStep references within a list of steps.

    Only references to steps within `original_step_ids` are rewritten—cross-track
    references are left untouched.
    """
    new_steps = []
    for step in steps:
        step = copy.deepcopy(step)
        step["stepId"] = step["stepId"] + suffix

        # Update startTrigger references
        trigger = step.get("startTrigger", {})
        _suffix_trigger_refs(trigger, suffix, original_step_ids)

        new_steps.append(step)
    return new_steps


def _suffix_trigger_refs(trigger: Dict, suffix: str, original_step_ids: set):
    """Recursively suffix stepId references in a trigger (handles compound triggers)."""
    if "triggers" in trigger:
        for sub in trigger["triggers"]:
            _suffix_trigger_refs(sub, suffix, original_step_ids)
    elif trigger.get("type") in ("afterStep", "afterStepWithBuffer", "onAbort"):
        ref = trigger.get("stepId", "")
        if ref in original_step_ids:
            trigger["stepId"] = ref + suffix


def _convert_legacy_batch(program: Dict[str, Any]) -> Dict[str, Any]:
    """
    Phase 1: Convert legacy batch_size/stagger/stagger_seconds on tracks
    into the `replicates` object format.
    """
    for track in program.get("tracks", []):
        if "replicates" in track:
            continue  # Already has replicates, skip legacy conversion

        batch_size = track.get("batch_size", 1)
        if batch_size <= 1:
            continue

        stagger_value = track.get("stagger", track.get("stagger_seconds", 0))
        stagger_seconds = _parse_delay(stagger_value)

        if stagger_seconds > 0:
            track["replicates"] = {
                "count": batch_size,
                "mode": "stagger",
                "delay": stagger_seconds,
            }
        else:
            track["replicates"] = {
                "count": batch_size,
                "mode": "parallel",
            }

        # Clean up legacy fields
        for key in ("batch_size", "stagger", "stagger_seconds"):
            track.pop(key, None)

    return program


def _expand_track_replicates(program: Dict[str, Any]) -> Dict[str, Any]:
    """
    Phase 2: Expand track-level replicates into multiple flat tracks.
    """
    new_tracks = []

    for track in program.get("tracks", []):
        replicates = track.get("replicates")
        if not replicates or replicates.get("count", 1) <= 1:
            # No expansion needed—strip replicates key and pass through
            track_copy = copy.deepcopy(track)
            track_copy.pop("replicates", None)
            new_tracks.append(track_copy)
            continue

        count = replicates["count"]
        mode = replicates.get("mode", "parallel")
        delay = _parse_delay(replicates.get("delay", 0))

        original_track_id = track["trackId"]
        original_track_name = track["name"]
        original_steps = track.get("steps", [])
        original_step_ids = {s["stepId"] for s in original_steps}

        for i in range(count):
            suffix = f"-r{i + 1}"
            replicate_track = copy.deepcopy(track)
            replicate_track.pop("replicates", None)
            replicate_track["trackId"] = f"{original_track_id}{suffix}"
            replicate_track["name"] = f"{original_track_name} ({i + 1} of {count})"

            # Suffix step IDs and internal references
            replicate_track["steps"] = _suffix_step_refs(
                original_steps, suffix, original_step_ids
            )

            if mode == "stagger" and i > 0 and delay > 0:
                # Add programStartOffset to first step of each staggered replicate
                first_step = replicate_track["steps"][0]
                trigger = first_step.get("startTrigger", {})
                trigger_type = trigger.get("type", "programStart")

                if trigger_type == "programStart":
                    first_step["startTrigger"] = {
                        "type": "programStartOffset",
                        "offsetSeconds": delay * i,
                    }
                elif trigger_type == "programStartOffset":
                    current_offset = _parse_delay(trigger.get("offsetSeconds", 0))
                    first_step["startTrigger"] = {
                        "type": "programStartOffset",
                        "offsetSeconds": current_offset + delay * i,
                    }

            elif mode == "serial" and i > 0:
                # Chain first step of replicate i to last step of replicate i-1
                prev_suffix = f"-r{i}"
                prev_last_step_id = f"{original_steps[-1]['stepId']}{prev_suffix}"
                first_step = replicate_track["steps"][0]
                first_step["startTrigger"] = {
                    "type": "afterStep",
                    "stepId": prev_last_step_id,
                }

            # For parallel mode (or first replicate), keep original triggers

            new_tracks.append(replicate_track)

    program["tracks"] = new_tracks
    return program


_STEP_REF_TYPES = ("afterStep", "afterStepWithBuffer")
_INSTANCE_VALUES = ("each", "all", "any")


def _trigger_atoms(trigger) -> List[Dict]:
    """Return the single triggers that make up a trigger (a compound's sub-triggers, or itself)."""
    if not isinstance(trigger, dict):
        return []
    if isinstance(trigger.get("triggers"), list):
        return [t for t in trigger["triggers"] if isinstance(t, dict)]
    return [trigger]


def _has_instances(trigger) -> bool:
    """True if any single trigger inside `trigger` carries an `instances` key."""
    return any("instances" in t for t in _trigger_atoms(trigger))


def _strip_instances(trigger):
    """Remove every `instances` key from a trigger (in place)."""
    for atom in _trigger_atoms(trigger):
        atom.pop("instances", None)


def _rewrite_barrier_trigger(trigger: Dict, groups: Dict) -> Dict:
    """
    Rewrite `instances: "all" | "any"` references to a replicated (or "each"-derived)
    step into an explicit fan-in over that step's instances.

    - single trigger  -> {"logic": <all|any>, "triggers": [<atom per instance>]}
    - inside a compound with the same logic -> flattened into the compound
    - inside a compound with different logic -> ValueError (nested compounds
      are not expressible in the 0.2.0 constructs the expander emits)

    Offset/buffer/event on the original trigger are preserved on every fan-in
    entry. `instances` keys are removed. References to steps that are not
    instance groups have their `instances` key dropped and are left alone
    (the validator reports those as E_INSTANCES_ON_SINGLE).
    """
    is_compound = isinstance(trigger.get("triggers"), list)
    logic = trigger.get("logic")
    atoms = trigger["triggers"] if is_compound else [trigger]
    new_atoms = []
    for atom in atoms:
        if not isinstance(atom, dict):
            new_atoms.append(atom)
            continue
        inst = atom.pop("instances", None)
        group = None
        if inst in ("all", "any") and atom.get("type") in _STEP_REF_TYPES:
            group = groups.get(atom.get("stepId"))
        if group is None:
            new_atoms.append(atom)
            continue
        fan = []
        for instance_id in group["instance_ids"]:
            entry = dict(atom)
            entry["stepId"] = instance_id
            fan.append(entry)
        if not is_compound:
            return {"logic": inst, "triggers": fan}
        if inst == logic:
            new_atoms.extend(fan)
        else:
            raise ValueError(
                f"instances: '{inst}' on '{atom.get('stepId')}' inside a compound "
                f"'{logic}' trigger is not supported (would require a nested compound)"
            )
    if is_compound:
        trigger["triggers"] = new_atoms
        return trigger
    return new_atoms[0]


def _merge_in_flight(own: Dict, synthetic: List[Dict]) -> Dict:
    """
    Add the synthetic in-flight sub-trigger(s) to a step's own trigger.

    The result must be satisfied by BOTH the step's own trigger and every
    gate, so the gates join an ``all`` compound. An own trigger that is
    already an ``all`` compound absorbs them; an ``any`` compound is nested
    (its "first of these" meaning must not be flattened away).
    """
    if isinstance(own.get("triggers"), list) and own.get("logic", "all") == "all":
        own["triggers"] = list(own["triggers"]) + synthetic
        return own
    return {"logic": "all", "triggers": [own] + synthetic}


def _apply_in_flight_gates(
    tracks: List[Dict],
    groups: Dict,
    each_parents: Dict[str, List[str]],
    in_flight: List,
):
    """
    Phase 3b: `replicates.maxInFlight` (schema 0.3.0-alpha, PRD §4.2/§5 step 4).

    For a replicated step X with ``maxInFlight: k`` and ``count: n``, instance
    i (1-based) is *in flight* from its own start until instance i has ended
    in every ``instances: "each"`` descendant of X. Instance i + k may not
    start before instance i leaves flight, so for every i > k the expander
    merges into X-r<i>'s trigger one ``afterStep L-r<i-k>`` per leaf chain,
    where L is the last ``"each"`` descendant of that chain. X with no
    ``"each"`` descendants gates on itself, which turns a parallel fan-out
    into a rolling window of k.

    Each synthetic sub-trigger is tagged ``_synthetic: "inFlight"`` with
    ``inFlightOf`` and ``inFlightLimit`` so the renderer and the analyzer can
    tell it from an authored dependency. The tag is inert for the timing
    resolvers, which read only ``type``/``stepId``/offsets, and it keeps the
    trigger from being re-expanded.
    """
    if not in_flight:
        return
    by_id = {}
    for track in tracks:
        for step in track.get("steps", []):
            by_id[step.get("stepId")] = step

    for root, limit in in_flight:
        group = groups.get(root)
        if not group:
            continue
        count = group["count"]
        # k >= count is E_INFLIGHT_GT_COUNT (or a no-op); the validators
        # report it and expansion stays a no-op rather than raising.
        if limit < 1 or limit >= count:
            continue
        # "each" descendants of X, in expansion order; the leaves are those
        # with no "each" child of their own.
        descendants = [g for g in groups if g != root and groups[g].get("root") == root]
        has_each_child = set()
        for child in descendants:
            for parent in each_parents.get(child, []):
                has_each_child.add(parent)
        leaves = [d for d in descendants if d not in has_each_child] or [root]

        for index in range(limit, count):  # instance index+1 > k
            step = by_id.get(group["instance_ids"][index])
            if step is None:
                continue
            synthetic = []
            for leaf in leaves:
                leaf_ids = groups[leaf]["instance_ids"]
                if index - limit >= len(leaf_ids):
                    continue
                synthetic.append(
                    {
                        "type": "afterStep",
                        "stepId": leaf_ids[index - limit],
                        "_synthetic": "inFlight",
                        "inFlightOf": root,
                        "inFlightLimit": limit,
                    }
                )
            if synthetic:
                step["startTrigger"] = _merge_in_flight(
                    step.get("startTrigger") or {}, synthetic
                )


def _expand_each_step(
    step: Dict,
    track_copy: Dict,
    groups: Dict,
    remap: Dict,
    sub_tracks_by_parent: Dict,
    each_parents: Dict,
):
    """
    Expand a step whose trigger references an instance group with `instances: "each"`.

    The step is replicated once per instance, paired i -> i with each referenced
    group (offset/buffer/event preserved), placed in instance i's sub-track, and
    registered as an instance group itself so further "each" steps chain
    transitively and plain references downstream join over all instances.
    """
    step_id = step["stepId"]
    step_name = step.get("name", step_id)
    trigger = step.get("startTrigger", {})

    each_groups = []
    each_parent_ids = []
    for atom in _trigger_atoms(trigger):
        if atom.get("instances") != "each":
            continue
        group = (
            groups.get(atom.get("stepId"))
            if atom.get("type") in _STEP_REF_TYPES
            else None
        )
        if group is None:
            atom.pop("instances", None)  # E_INSTANCES_ON_SINGLE: validator's job
            continue
        each_groups.append(group)
        each_parent_ids.append(atom.get("stepId"))

    if not each_groups:
        # Only barrier/any references (or none that resolve): single step.
        step["startTrigger"] = _rewrite_barrier_trigger(trigger, groups)
        return

    count = each_groups[0]["count"]
    for group in each_groups[1:]:
        if group["count"] != count:
            raise ValueError(
                f"E_EACH_COUNT_MISMATCH: step '{step_id}' pairs instances of "
                f"'{each_groups[0]['root']}' (count {count}) with "
                f"'{group['root']}' (count {group['count']})"
            )
    place = each_groups[0]
    parent_track = place["parent_track"]
    parent_id = parent_track["trackId"]

    # Remove the placeholder from its original track.
    track_copy["steps"] = [s for s in track_copy["steps"] if s is not step]

    instance_ids = []
    for i in range(count):
        copy_step = copy.deepcopy(step)
        copy_step["stepId"] = f"{step_id}-r{i + 1}"
        copy_step["name"] = f"{step_name} ({i + 1} of {count})"
        copy_step["instanceOf"] = step_id
        copy_step["instanceIndex"] = i + 1
        copy_trigger = copy_step.get("startTrigger", {})
        for atom in _trigger_atoms(copy_trigger):
            if atom.get("instances") == "each" and atom.get("stepId") in groups:
                atom["stepId"] = groups[atom["stepId"]]["instance_ids"][i]
                atom.pop("instances", None)
        copy_step["startTrigger"] = _rewrite_barrier_trigger(copy_trigger, groups)

        sub_track = place["sub_tracks"][i]
        if sub_track is None:
            sub_track = {
                "trackId": f"{parent_id}--{place['root']}-r{i + 1}",
                "name": f"{parent_track.get('name', parent_id)} - {place['root_name']} ({i + 1} of {count})",
                "parentTrackId": parent_id,
                "steps": [],
            }
            place["sub_tracks"][i] = sub_track
            sub_tracks_by_parent.setdefault(parent_id, []).append(sub_track)
        sub_track["steps"].append(copy_step)
        instance_ids.append(copy_step["stepId"])

    groups[step_id] = {
        "root": place["root"],
        "root_name": place["root_name"],
        "count": count,
        "mode": place["mode"],
        "instance_ids": instance_ids,
        "sub_tracks": place["sub_tracks"],
        "parent_track": parent_track,
    }
    each_parents[step_id] = each_parent_ids
    remap[step_id] = {"_join": True, "step_ids": instance_ids}


def _expand_step_replicates(program: Dict[str, Any]) -> Dict[str, Any]:
    """
    Phase 3: Expand step-level replicates.

    - serial: N copies chained sequentially in same track; next step references last copy
    - parallel: N copies in N new sub-tracks; next step uses join trigger
    - stagger: Same as parallel + offsetSeconds on each copy's trigger

    Triggers may carry `instances` (schema 0.3.0-alpha) when they reference a
    replicated step:

    - "each": the referencing step is replicated once per instance (paired
      i -> i, transitive, placed in instance i's sub-track; for serial
      replicates the per-instance sub-tracks `<trackId>--<stepId>-r<i>` are
      created on demand)
    - "all": explicit barrier -> compound{all} over the instances (the default
      join for a plain reference)
    - "any": compound{any} over the instances

    `replicates.maxInFlight` then adds the synthetic in-flight gates
    (see :func:`_apply_in_flight_gates`).

    Expanded copies carry `instanceOf` / `instanceIndex`; sub-tracks carry
    `parentTrackId`. No `instances` key survives expansion.
    """
    new_tracks = []
    sub_tracks_by_parent: Dict[str, List[Dict]] = {}
    # Global step ID remapping for cross-track references
    global_step_id_remap: Dict[str, Any] = {}
    # Instance groups: stepId -> {root, root_name, count, mode, instance_ids, sub_tracks, parent_track}
    groups: Dict[str, Dict] = {}
    # Steps whose triggers carry `instances`, expanded after all groups are known
    deferred = []
    # stepId -> upstream group ids referenced with `instances: "each"`
    each_parents: Dict[str, List[str]] = {}
    # (replicated stepId, maxInFlight) for every step declaring the limit
    in_flight: List = []

    for track in program.get("tracks", []):
        track_id = track["trackId"]
        original_steps = track.get("steps", [])

        track_copy = copy.deepcopy(track)
        expanded_steps: List[Dict[str, Any]] = []
        track_copy["steps"] = expanded_steps
        sub_tracks = sub_tracks_by_parent.setdefault(track_id, [])

        for step in original_steps:
            replicates = step.get("replicates")
            if not replicates or replicates.get("count", 1) <= 1:
                step_copy = copy.deepcopy(step)
                step_copy.pop("replicates", None)
                if _has_instances(step_copy.get("startTrigger", {})):
                    # Leave the trigger untouched until every group exists.
                    deferred.append((track_copy, step_copy))
                else:
                    # Remap trigger reference if needed
                    _remap_trigger_refs(step_copy, global_step_id_remap)
                expanded_steps.append(step_copy)
                continue

            count = replicates["count"]
            mode = replicates.get("mode", "parallel")
            delay = _parse_delay(replicates.get("delay", 0))
            original_step_id = step["stepId"]
            original_step_name = step["name"]

            base_trigger = step.get("startTrigger", {})
            if _has_instances(base_trigger):
                for atom in _trigger_atoms(base_trigger):
                    if atom.get("instances") == "each":
                        raise ValueError(
                            f"E_EACH_WITH_REPLICATES: step '{original_step_id}' has both "
                            f'`replicates` and an `instances: "each"` trigger'
                        )
                step = copy.deepcopy(step)
                step["startTrigger"] = _rewrite_barrier_trigger(
                    step["startTrigger"], groups
                )

            group = {
                "root": original_step_id,
                "root_name": original_step_name,
                "count": count,
                "mode": mode,
                "instance_ids": [],
                "sub_tracks": [None] * count,
                "parent_track": track_copy,
            }

            if mode == "serial":
                # N copies chained sequentially in the same track
                for j in range(count):
                    suffix = f"-r{j + 1}"
                    copy_step = copy.deepcopy(step)
                    copy_step.pop("replicates", None)
                    copy_step["stepId"] = f"{original_step_id}{suffix}"
                    copy_step["name"] = f"{original_step_name} ({j + 1} of {count})"
                    copy_step["instanceOf"] = original_step_id
                    copy_step["instanceIndex"] = j + 1

                    if j == 0:
                        # First copy keeps original trigger (but remapped)
                        _remap_trigger_refs(copy_step, global_step_id_remap)
                    else:
                        # Subsequent copies chain to previous
                        prev_id = f"{original_step_id}-r{j}"
                        copy_step["startTrigger"] = {
                            "type": "afterStep",
                            "stepId": prev_id,
                        }

                    expanded_steps.append(copy_step)
                    group["instance_ids"].append(copy_step["stepId"])

                # Next step should reference the last copy
                global_step_id_remap[original_step_id] = f"{original_step_id}-r{count}"

            elif mode in ("parallel", "stagger"):
                # N copies in N new sub-tracks
                replica_last_ids = []

                for j in range(count):
                    suffix = f"-r{j + 1}"
                    copy_step = copy.deepcopy(step)
                    copy_step.pop("replicates", None)
                    copy_step["stepId"] = f"{original_step_id}{suffix}"
                    copy_step["name"] = f"{original_step_name} ({j + 1} of {count})"
                    copy_step["instanceOf"] = original_step_id
                    copy_step["instanceIndex"] = j + 1

                    # Build trigger for this copy
                    _remap_trigger_refs(copy_step, global_step_id_remap)

                    if mode == "stagger" and j > 0 and delay > 0:
                        # Add offset to the trigger
                        trigger = copy_step.get("startTrigger", {})
                        current_offset = _parse_delay(trigger.get("offsetSeconds", 0))
                        trigger["offsetSeconds"] = current_offset + delay * j
                        copy_step["startTrigger"] = trigger

                    sub_track = {
                        "trackId": f"{track_id}--{original_step_id}{suffix}",
                        "name": f"{track.get('name', track_id)} - {original_step_name} ({j + 1} of {count})",
                        "parentTrackId": track_id,
                        "steps": [copy_step],
                    }
                    sub_tracks.append(sub_track)
                    group["sub_tracks"][j] = sub_track
                    replica_last_ids.append(f"{original_step_id}{suffix}")

                group["instance_ids"] = replica_last_ids
                # Next step should use a join trigger waiting for all copies
                global_step_id_remap[original_step_id] = {
                    "_join": True,
                    "step_ids": replica_last_ids,
                }

            groups[original_step_id] = group
            max_in_flight = replicates.get("maxInFlight")
            if isinstance(max_in_flight, int) and not isinstance(max_in_flight, bool):
                in_flight.append((original_step_id, max_in_flight))

        new_tracks.append(track_copy)

    # Expand `instances` triggers now that every replicated step is a group.
    # A step chained "each" off another "each" step waits for that step's
    # own expansion, so iterate to a fixed point.
    pending_each = {
        s["stepId"]
        for _, s in deferred
        if any(
            a.get("instances") == "each"
            for a in _trigger_atoms(s.get("startTrigger", {}))
        )
    }
    while deferred:
        remaining = []
        progressed = False
        for track_copy, step in deferred:
            waits = any(
                a.get("instances") == "each" and a.get("stepId") in pending_each
                for a in _trigger_atoms(step.get("startTrigger", {}))
            )
            if waits:
                remaining.append((track_copy, step))
                continue
            _expand_each_step(
                step,
                track_copy,
                groups,
                global_step_id_remap,
                sub_tracks_by_parent,
                each_parents,
            )
            pending_each.discard(step["stepId"])
            progressed = True
        if not progressed:
            cyclic = ", ".join(sorted(s["stepId"] for _, s in remaining))
            raise ValueError(
                f'cyclic instances: "each" references among steps: {cyclic}'
            )
        deferred = remaining

    # Assemble: each top-level track followed by its sub-tracks, in creation order
    ordered_tracks = []
    for track_copy in new_tracks:
        ordered_tracks.append(track_copy)
        ordered_tracks.extend(sub_tracks_by_parent.get(track_copy["trackId"], []))

    # Filter out empty tracks (happens when all steps are moved to sub-tracks)
    non_empty_tracks = [track for track in ordered_tracks if track.get("steps")]

    # Apply global step ID remapping to all tracks for cross-track references,
    # and make sure no 0.3.0 `instances` key reaches downstream consumers.
    for track in non_empty_tracks:
        for step in track.get("steps", []):
            _remap_trigger_refs(step, global_step_id_remap)
            _strip_instances(step.get("startTrigger", {}))

    # `maxInFlight` gates last: they reference already-remapped instance ids
    # and must not be rewritten or stripped again.
    _apply_in_flight_gates(non_empty_tracks, groups, each_parents, in_flight)

    program["tracks"] = non_empty_tracks
    return program


def _remap_trigger_refs(step: Dict, remap: Dict):
    """Remap startTrigger stepId references using the remap dict."""
    trigger = step.get("startTrigger", {})
    _remap_trigger(trigger, remap)


def _remap_trigger(trigger: Dict, remap: Dict):
    """Recursively remap trigger references."""
    if "triggers" in trigger:
        # Handle compound triggers - flatten any joins to avoid nested compounds
        new_triggers = []
        for sub in trigger["triggers"]:
            ref = sub.get("stepId", "")
            if ref in remap and sub.get("type") in (
                "afterStep",
                "afterStepWithBuffer",
                "onAbort",
            ):
                replacement = remap[ref]
                if isinstance(replacement, dict) and replacement.get("_join"):
                    # Flatten the join into the parent compound trigger
                    for sid in replacement["step_ids"]:
                        new_triggers.append(
                            {
                                "type": "afterStep",
                                "stepId": sid,
                            }
                        )
                else:
                    sub_copy = sub.copy()
                    sub_copy["stepId"] = replacement
                    new_triggers.append(sub_copy)
            else:
                # Recursively handle nested compounds (though we try to avoid them)
                sub_copy = sub.copy()
                _remap_trigger(sub_copy, remap)
                new_triggers.append(sub_copy)
        trigger["triggers"] = new_triggers
        return

    if trigger.get("type") in ("afterStep", "afterStepWithBuffer", "onAbort"):
        ref = trigger.get("stepId", "")
        if ref in remap:
            replacement = remap[ref]
            if isinstance(replacement, dict) and replacement.get("_join"):
                # Replace entire trigger with a compound join trigger
                join_triggers = []
                for sid in replacement["step_ids"]:
                    join_triggers.append(
                        {
                            "type": "afterStep",
                            "stepId": sid,
                        }
                    )
                # Mutate trigger in place to become a compound trigger
                trigger.clear()
                trigger["logic"] = "all"
                trigger["triggers"] = join_triggers
            else:
                trigger["stepId"] = replacement


def expand_replicates(program: Dict[str, Any]) -> Dict[str, Any]:
    """
    Expand all replicates in a program into flat tracks and steps.

    This is a pure function that deep-copies the input and returns a new program
    with all replicates expanded. Should be called BEFORE validation and visualization.

    Phases:
    1. Convert legacy batch_size/stagger to replicates objects
    2. Expand track-level replicates
    3. Expand step-level replicates
    """
    program = copy.deepcopy(program)
    program = _convert_legacy_batch(program)
    program = _expand_track_replicates(program)
    program = _expand_step_replicates(program)
    return program
