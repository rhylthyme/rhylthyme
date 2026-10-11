---
name: rhylthyme
description: Schedule multi-step procedures that a person carries out against a clock, with parallel tracks, shared equipment and steps that end when the person says so. Use when turning a lab protocol, recipe, event run sheet or workout into a timed plan; when several procedures must share one instrument (one centrifuge, one thermocycler, one oven); when work has to finish at a set time; when asking "when do I start each thing"; or when a protocol should be run at the bench with timers and then its real durations recorded. Validates the plan, finds resource conflicts, publishes a live timeline the person follows on a phone, and runs it in the terminal. For robot-executed protocols use opentrons-integration or pylabrobot; for finding published protocols use protocolsio-integration.
license: Apache-2.0
metadata:
    skill-author: Rhylthyme (Jeremy Leipzig)
---

# Rhylthyme

## Overview

Rhylthyme is a small JSON language and a command-line tool for schedules that a
*person* executes: a bench protocol with overlapping incubations, four dishes
that must land together, a run of show, a training session. A **program** is a
set of parallel **tracks**, each a sequence of timed **steps**; steps start on
**triggers** (at the start, after another step, a fixed time before another
step ends, when the person taps a button) and occupy **resources** with a
capacity (`centrifuge: 1`, `burner: 4`). The tool validates the program,
reports resource conflicts and the chain that sets the finish time, publishes
it as a live timeline with timers and audio cues, and runs it in the terminal.

The part that distinguishes it from a checklist with a timer is the resource
model: two protocols that both need the only thermocycler are scheduled around
each other, and stay that way while the work is running.

## When to Use This Skill

- **Timing a protocol at the bench**: a Western blot, a PCR plus gel, a cell
  passage, an ELISA, with incubations that overlap hands-on work
- **Running several procedures at once** that compete for one instrument, hood,
  rotor, oven or pair of hands
- **Working back from a deadline**: "imaging at 16:00, when do I start the
  transfer?" or "dinner at 7, when does the roast go in?"
- **Turning prose into a plan**: a methods section, a recipe, a run sheet or a
  coach's message into a validated multi-track schedule
- **Steps with unknown length**: "incubate until confluent", "roast until
  74 °C", which end when the person says so and hold everything downstream
- **Repeating work**: twelve samples through one rotor, three trays through one
  oven, with a limit on how many are in progress at once
- **Learning real durations**: recording planned against actual times over
  repeated runs and proposing better estimates
- **Sharing a schedule**: a link a colleague opens on a phone, with a clock

Do not use it for robot or liquid-handler execution, for optimizing a schedule
to a proven minimum (it checks a schedule, it does not search for the best one), or for
calendar booking.

## Installation

```bash
pip install rhylthyme                        # Python 3.12+; the CLI, importers and renderer
rhylthyme --version
```

The command itself is the `rhylthyme` package; from source:
`pip install "git+https://github.com/rhylthyme/rhylthyme"`.

**Installed as the `rhylthyme` Claude plugin?** Then the hosted MCP tools come
with it, and `validate_program`, `analyze_schedule` and `visualize_schedule`
do what `rhylthyme validate`, `analyze` and `publish` do, with nothing to
install. Use the tools when they are available and the command line when you
need a file on disk, the terminal runner, or recorded runs.

No account is needed to validate, analyze, run or publish. `analyze`,
`publish` and `generate` call the hosted server at mcp.rhylthyme.com and need
network access; `validate`, `run`, `runs` and `calibrate` are local. Only `rhylthyme
generate` (server-side authoring from text) needs `rhylthyme login`, which
opens a browser; on a headless machine set `RHYLTHYME_TOKEN` instead.

## Core Capabilities

### 1. Author a program from a description or a source text

Write the program JSON yourself; that is the normal path and needs no account.
Work in this order, because extracting the steps before relating them is what
makes the result correct (on 24 expert-written programs it raised dependency
F1 from about 0.2 to about 0.6 on every model tested):

1. **Read the source back.** List what it contains: dishes or sub-procedures,
   equipment, people, any deadline. Note what is missing.
2. **Fix the model.** Decide the tracks (one per station, instrument, dish or
   person: whatever can only do one thing at a time) and the resources with
   their capacities.
3. **Extract every step** with its duration, quoting the phrase it came from.
   Mark a step you inferred rather than read as inferred.
4. **Only then add triggers**: chain each track with `afterStep`, add
   cross-track dependencies, and delay short tracks so everything lands
   together.

Then validate (capability 2) and fix every finding before doing anything else.
An imported program deserves a second opinion: `rhylthyme import ... --review`
(or the `review_program` MCP tool) has a model read it against the source and
report wrong durations, dropped steps and bad ordering; apply what it says,
then validate again.

A complete, valid program:

```json
{
  "schemaVersion": "0.3.0-alpha",
  "programId": "western-blot-day-one",
  "name": "Western blot, day one",
  "environmentType": "laboratory",
  "tracks": [
    { "trackId": "gel", "name": "Gel and transfer", "steps": [
      { "stepId": "load-run", "name": "Load and run SDS-PAGE", "task": "gel-box",
        "duration": { "type": "fixed", "seconds": "90m" },
        "startTrigger": { "type": "programStart" } },
      { "stepId": "transfer", "name": "Transfer to PVDF", "task": "transfer-cell",
        "duration": { "type": "fixed", "seconds": "60m" },
        "startTrigger": { "type": "afterStep", "stepId": "load-run" } },
      { "stepId": "block", "name": "Block in 5% milk", "task": "rocker",
        "duration": { "type": "variable", "minSeconds": "45m", "maxSeconds": "90m", "defaultSeconds": "60m" },
        "startTrigger": { "type": "afterStep", "stepId": "transfer" } },
      { "stepId": "primary", "name": "Primary antibody, 4 °C", "task": "cold-room-rocker",
        "duration": { "type": "indefinite", "defaultSeconds": "16h" },
        "startTrigger": { "type": "afterStep", "stepId": "block" } }
    ] },
    { "trackId": "prep", "name": "Bench prep", "steps": [
      { "stepId": "buffers", "name": "Make transfer buffer, cut membrane", "task": "bench",
        "duration": { "type": "fixed", "seconds": "20m" },
        "startTrigger": { "type": "programStartOffset", "offsetSeconds": "60m" } },
      { "stepId": "dilute-ab", "name": "Dilute primary antibody", "task": "bench",
        "duration": { "type": "fixed", "seconds": "10m" },
        "startTrigger": { "type": "afterStep", "stepId": "transfer", "offsetSeconds": "40m" } }
    ] }
  ],
  "resourceConstraints": [
    { "task": "gel-box", "maxConcurrent": 1 }, { "task": "transfer-cell", "maxConcurrent": 1 },
    { "task": "rocker", "maxConcurrent": 2 }, { "task": "cold-room-rocker", "maxConcurrent": 1 },
    { "task": "bench", "maxConcurrent": 1 }
  ]
}
```

**Reference:** read `references/program_format.md` for every trigger and
duration type, replicates and in-flight limits, choices, environments and the
rules the validator enforces. Read `references/worked_examples.md` for a
kitchen, an event and a twelve-sample example, and for deadline arithmetic.

### 2. Validate, and fix what it reports

```bash
rhylthyme validate program.json            # human-readable
rhylthyme validate program.json --json     # machine-readable, for a fix loop
rhylthyme validate program.json --strict   # every task must have a constraint
rhylthyme validate *.json -e lab.json      # against an environment file
```

Always validate before publishing or running. The three findings that account
for nearly all rejected drafts, and their fixes:

| Finding | Cause | Fix |
|---|---|---|
| Steps in one track overlap | two steps in a track both start on their own trigger | chain the second with `afterStep` on the first, or move it to another track |
| Negative `offsetSeconds` requires an indefinite step | "start 45 min before X ends" where X is fixed or variable | make X `indefinite` with a `defaultSeconds`, or use a positive offset from an earlier step |
| Task has no resource constraint | a step's `task` is not declared | add `{ "task": ..., "maxConcurrent": n }` |

A program can be valid and still wrong about time, or ask for one instrument
twice at once. After validating, run `rhylthyme analyze` (capability 5) and
check the total length and the finish-together arithmetic against the source.

### 3. Publish a live timeline

```bash
rhylthyme publish program.json        # summary, ASCII Gantt, itinerary and the URL
rhylthyme publish program.json -q     # just the URL
rhylthyme publish program.json --json # url, imageUrl, makespanSeconds, warnings
rhylthyme publish program.json -q --image timeline.png   # the URL, and a picture to show
```

No account needed. The server validates again and refuses an invalid program.
The URL opens an interactive timeline with play and pause, timers, audio cues
at step boundaries, an itinerary sorted by clock time and a dependency graph.
The site is chosen from `environmentType` (`laboratory` goes to
lab.rhylthyme.com); override with `-e kitchen|lab|events|gym|generic`. Give the
person the URL rather than describing the schedule in prose.

### 4. Run it in the terminal

```bash
rhylthyme run program.json                  # interactive; waits for a manual start
rhylthyme run program.json --auto-start
rhylthyme run program.json --time-scale 60  # rehearse: one minute per second
```

`run` is an interactive terminal UI: it needs a real terminal, so ask the user
to run it rather than running it yourself in a non-interactive shell. Manual
gates wait for a keypress, indefinite and variable steps are ended by the
person, and delays propagate down dependency chains. On exit it writes a record
of planned against actual times under `~/.rhylthyme/runs/`.

### 5. Analyze: total length, conflicts, clock times

```bash
rhylthyme analyze program.json                    # length, critical path, conflicts
rhylthyme analyze program.json --finish-at 19:00  # when to start each step
rhylthyme analyze program.json --strict           # non-zero exit on a resource conflict
rhylthyme analyze program.json --json             # everything, per step
```

`validate` checks structure; `analyze` checks the schedule. It reports the
total length, the chain of steps that sets it, what gates each link of that
chain (a dependency, a full instrument, an in-flight limit), every interval
where more steps claim a resource than its capacity, and which tracks finish
early. With `--finish-at` or `--start-at` it gives each step's local clock
time. No account needed; nothing is published.

A reported conflict means two steps want the same instrument at once. Fix it
in the program: start one track later with `programStartOffset`, or chain the
second use `afterStep` the first. Then analyze again.

### 6. Learn real durations from recorded runs

```bash
rhylthyme runs list <programId>
rhylthyme runs show <runId>
rhylthyme runs report <programId>            # per-step statistics, predictability
rhylthyme calibrate program.json             # proposes durations, with evidence
rhylthyme calibrate program.json --accept all --write calibrated.json
```

`calibrate` proposes the observed median as `defaultSeconds` and P10 to P90 as
the range for steps with at least five measurements, never narrows the
author's range, never changes a fixed step, and writes nothing unless
`--accept` names the steps.

### 7. Author on the server from pasted text (needs sign-in)

```bash
rhylthyme login
rhylthyme generate -e lab -f methods.txt -o blot.json
rhylthyme generate "roast chicken and two sides for 6" -e kitchen --by 19:00 --with "one oven"
```

Runs the same four steps as capability 1 on the hosted server with a repair
loop, then publishes. It costs model calls, is capped per day, takes 20 to 60
seconds, and needs an account. Prefer capability 1 unless the user asks for it.

**Reference:** read `references/cli_reference.md` for every command, flag and
environment variable.

## Best Practices

1. **Tracks are things that can only do one thing at a time.** One per
   instrument, station, dish or person. Steps in a track never overlap.
2. **Every `task` gets a capacity.** That is what lets two protocols share a
   thermocycler correctly.
3. **Use `indefinite` for "until it's done"** (confluence, an internal
   temperature, a colour change) and always give `defaultSeconds` so the plan
   has a length.
4. **Use `variable` for a range** the person may cut short, and `fixed` only
   for timings that really are fixed (a centrifuge program, a PCR cycle).
5. **Finish together by delaying the short tracks** with `programStartOffset`,
   not by padding steps.
6. **Use `replicates` for repeated work**, never copy steps; add `maxInFlight`
   when something downstream (a rack, a bench) holds only so many.
7. **Validate, then analyze.** The validator catches structure; a wrong total
   length is the commonest remaining error, and `analyze` shows it.
8. **Deliver the URL.** A published timeline is what the person follows at the
   bench; prose is not.
9. **Do not put anything confidential in a published program.** A published
   timeline is reachable by anyone who has its link.

## Troubleshooting

- **`rhylthyme: command not found`**: the package installs a `rhylthyme` script
  into the environment's `bin`; activate that environment, or run
  `python -m rhylthyme ...`.
- **`No module named 'pkg_resources'`** on validate: an old `rhylthyme-spec` is
  installed; `pip install -U "rhylthyme-spec>=0.2.0a0"`.
- **`publish` says the program is invalid but `validate` passed**: the server
  runs a second validator; fix what it reports, or compare with `--strict`.
- **`generate`: "Not signed in"**: run `rhylthyme login`, or set
  `RHYLTHYME_TOKEN`; tokens pasted that way last about an hour.
- **`run` exits immediately or draws nothing**: it needs an interactive
  terminal, not a pipe or a non-interactive shell.
- **A step never starts in `run`**: it is waiting on a manual gate or on an
  indefinite step the person has not ended.

## Resources

- Command-line tool: https://github.com/rhylthyme/rhylthyme
- Schema: https://github.com/rhylthyme/rhylthyme-spec
- Example programs and environments: https://github.com/rhylthyme/rhylthyme-examples
- MCP server (the same tools for MCP hosts): https://github.com/rhylthyme/rhylthyme-mcp
- Documentation: https://docs.rhylthyme.com
- Bundled references: `references/program_format.md`,
  `references/cli_reference.md`, `references/worked_examples.md`
