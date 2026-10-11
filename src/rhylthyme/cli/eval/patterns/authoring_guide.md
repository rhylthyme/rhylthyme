# Rhylthyme program authoring guide

A program describes a real-time, multi-track process. The live runner
(rhylthyme.com) turns it into timers, cues and a shared clock.

## Shape

```json
{
  "schemaVersion": "0.3.0-alpha",
  "programId": "kebab-case-id",
  "name": "Human title",
  "description": "optional",
  "environmentType": "kitchen | laboratory | event | gym | manufacturing | general",
  "actors": 1,
  "tracks": [
    { "trackId": "t1", "name": "Station / dish / instrument / performer",
      "steps": [
        { "stepId": "s1", "name": "Do the thing", "task": "oven",
          "duration": { "type": "fixed", "seconds": 600 },
          "startTrigger": { "type": "programStart" } },
        { "stepId": "s2", "name": "Next thing", "task": "prep",
          "duration": { "type": "variable", "minSeconds": 60, "maxSeconds": 300, "defaultSeconds": 120 },
          "startTrigger": { "type": "afterStep", "stepId": "s1" } }
      ] }
  ],
  "resourceConstraints": [ { "task": "oven", "maxConcurrent": 1 }, { "task": "prep", "maxConcurrent": 2 } ],
  "metadata": { "ingredients": [ { "name": "eggs", "measure": "2" } ], "serves": "4", "sourceUrl": "", "attribution": "" }
}
```

## Rules the validator enforces

1. `stepId` is unique across the WHOLE program (not just the track).
2. Steps in one track run sequentially and must not overlap. The first
   step usually uses `programStart`; every later step chains with
   `{"type":"afterStep","stepId":"<previous step>"}`. Parallel work goes
   in separate tracks.
3. Every `task` used by a step needs a `resourceConstraints` entry
   with the same name (unless the program references an `environment`
   or declares `actors`). This includes steps inside an
   `instances: "each"` chain: every instance claims the same task, so
   one constraint entry covers them, but it must exist.
4. Durations: `fixed` needs `seconds`; `variable` needs `minSeconds`
   + `maxSeconds` (+ `defaultSeconds` for planning); `indefinite` runs
   until the user ends it (give `defaultSeconds` so previews look right).
   Numbers are seconds; strings like `"5m"`, `"1h30m"`, `"90s"` also work.
5. `afterStep` references must point at existing steps and must not form
   a cycle.
6. `instances` may only reference a replicated step (or a step already
   replicated by an `"each"` chain); an `"each"` step must not declare
   its own `replicates`; `maxInFlight` must be <= `count`. See
   "Repeating work" below for the codes.

## Triggers

| type                 | fields                                   | meaning                                            |
|----------------------|------------------------------------------|----------------------------------------------------|
| programStart         | —                                        | at t = 0                                           |
| programStartOffset   | offsetSeconds                            | at t = offset                                      |
| afterStep            | stepId, offsetSeconds?, event?, choiceId?, instances? | when stepId ends (event="start": when it starts) + offset |
| afterStepWithBuffer  | stepId, bufferSeconds, instances?        | when stepId ends + buffer                          |
| manual               | triggerName?                             | user taps to start                                 |
| onAbort              | stepId                                   | only if stepId is aborted                          |
| compound             | logic: "all" \| "any", triggers: [...]   | wait for all / the first of several triggers       |

Negative `offsetSeconds` ("start 20 min before the roast finishes")
requires the referenced step to be `indefinite`. `instances`
(`"each" | "all" | "any"`, default `"all"`) only applies when the
referenced step is replicated — see "Repeating work" below.

## Finishing together

To make every track end at the same moment, compute each track's length
and delay the shorter ones with `programStartOffset`, or chain them
`afterStep` off a step in the long track. `analyze_schedule` reports
per-track slack; pass `finishAt` to get wall-clock start times.

## Repeating work: per-instance chains, barriers and in-flight limits

### `replicates`: do this step n times

`replicates` on a step says "do this n times" without copying JSON:

```json
"replicates": { "count": 3, "mode": "serial", "delay": "5m", "maxInFlight": 2 }
```

- `count` — how many instances. Expansion names them `<stepId>-r1` …
  `<stepId>-r<n>` and stamps `instanceOf` / `instanceIndex` on each.
- `mode` — `serial` (one after another, in the same track), `parallel`
  (all at once, each instance in its own sub-track), `stagger` (each
  start `delay` after the previous one).
- `delay` — the gap for `stagger` (`"5m"`, `"90s"`, or seconds).
- `maxInFlight` — the work-in-progress cap; see below.

### `instances`: per instance, every instance, or the first

A trigger that references a replicated step says how it joins.
`instances` goes on `afterStep` / `afterStepWithBuffer` (including
inside `compound`):

| instances        | meaning                                                                                 |
|------------------|-----------------------------------------------------------------------------------------|
| `"all"` (default) | one step that waits for EVERY instance — the barrier; this is what pre-0.3.0 programs already do, so leaving `instances` off never changes an existing schedule |
| `"each"`         | the step is itself replicated, once per instance: instance i starts when instance i of the referenced step ends (`offsetSeconds` / `bufferSeconds` / `event: "start"` all still apply) |
| `"any"`          | one step that starts when the FIRST instance ends                                       |

`"each"` is transitive and inherits the count — never declare
`replicates` on an `"each"` step. `"all"` and `"any"` collapse the chain
back into a single step, which downstream steps then reference without
`instances`. A `compound` may pair two `"each"` upstreams only if they
have the same `count`.

### `maxInFlight`: hold upstream, don't strand downstream

`maxConcurrent` (on `resourceConstraints`) caps how many steps occupy
one task at one instant. `maxInFlight` (on `replicates`) caps how many
instances are *between* the replicated step and its barrier — a chain
across time. Instance i is in flight from its start until instance i has
finished every `"each"` descendant; instance i + `maxInFlight` may not
start before that.

Reach for `maxInFlight` whenever the limit is a holding area rather than
a machine: a cooling rack that holds two trays, a rotor that holds six
tubes, a taxiway that holds four aircraft, a bench with room for four
open plates. `"rack", maxConcurrent: 2` alone would let the third tray
bake anyway and then make it queue for a rack slot — a hot tray with
nowhere to go. `maxInFlight: 2` holds the *bake* instead.

With `mode: "parallel"` a `maxInFlight` below `count` turns the fan-out
into a rolling window; with `mode: "stagger"` the delay becomes a
minimum gap.

### Worked example: three trays, one oven, a rack that holds two

<!-- rhylthyme:example cookies-three-trays -->
```json
{
  "schemaVersion": "0.3.0-alpha",
  "programId": "cookies-three-trays",
  "name": "Three trays, one oven",
  "environmentType": "kitchen",
  "tracks": [
    { "trackId": "cookies", "name": "Cookies", "steps": [
      { "stepId": "mix", "name": "Mix dough", "task": "prep",
        "duration": { "type": "fixed", "seconds": 900 },
        "startTrigger": { "type": "programStart" } },
      { "stepId": "bake", "name": "Bake tray", "task": "oven",
        "duration": { "type": "fixed", "seconds": 720 },
        "replicates": { "count": 3, "mode": "serial", "maxInFlight": 2 },
        "startTrigger": { "type": "afterStep", "stepId": "mix" } },
      { "stepId": "cool", "name": "Cool on rack", "task": "rack",
        "duration": { "type": "fixed", "seconds": 900 },
        "startTrigger": { "type": "afterStep", "stepId": "bake", "instances": "each" } },
      { "stepId": "box", "name": "Box cookies", "task": "prep",
        "duration": { "type": "fixed", "seconds": 300 },
        "startTrigger": { "type": "afterStep", "stepId": "cool", "instances": "all" } }
    ] }
  ],
  "resourceConstraints": [
    { "task": "prep", "maxConcurrent": 1 },
    { "task": "oven", "maxConcurrent": 1 },
    { "task": "rack", "maxConcurrent": 2 }
  ]
}
```

Minutes from start: mix 0–15; bake 15–27, 27–39, **42–54**; cool 27–42,
39–54, 54–69; box 69–74. The third bake could start at 39 (the oven is
free) but waits until 42, when the first tray leaves the rack.
`analyze_schedule` reports the in-flight windows and names `rack`, not
`oven`, as the binding constraint. The same shape covers "12 samples,
the rotor holds 6" and "three landings, the taxiway holds two".

### Findings you may see

| code                    | what it means                                                                   |
|-------------------------|---------------------------------------------------------------------------------|
| `E_INSTANCES_ON_SINGLE` | `instances` on a step that is not replicated — remove it, or add `replicates` to the referenced step |
| `E_EACH_WITH_REPLICATES`| a step has both an `"each"` trigger and its own `replicates` — drop the `replicates`, the count is inherited |
| `E_EACH_COUNT_MISMATCH` | a `compound` `"each"` pairs two replicated steps with different `count`s     |
| `E_INFLIGHT_GT_COUNT`   | `maxInFlight` is greater than `count`                                          |
| `E_INFLIGHT_NO_CHAIN`   | `maxInFlight` on a `serial` replicate with no `"each"` descendants: nothing is ever held back |
| `W_UNBARRIERED_CHAIN`   | warning: an `"each"` chain has no `"all"` barrier, yet later steps do not wait for it |
| `I_IMPLICIT_BARRIER`    | info: a reference to a replicated step with no `instances`; the default `"all"` barrier applies. Add `"all"` to confirm it, or `"each"` if the work is per instance |

## Choice branching (schemaVersion "0.2.0-alpha" and later)

A step with `"choice": {"prompt": "...", "options": [{"choiceId":"a","label":"A"},{"choiceId":"b","label":"B"}]}`
becomes a decision point; downstream steps with `"startTrigger": {"type":"afterStep","stepId":"<choice step>","choiceId":"a"}`
only run for that option.

## Workflow

build → `validate_program` (fix every error) → `analyze_schedule`
(optional; makespan, critical path, conflicts, wall clock) →
`visualize_schedule` (publishes; returns the live URL, Gantt and itinerary).
