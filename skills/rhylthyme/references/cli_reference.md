# Command reference

`rhylthyme <command> --help` prints the same information. Programs may be JSON
or YAML. If the `rhylthyme` script is not on the PATH, every command also works
as `python -m rhylthyme <command>`.

## No account needed

### `rhylthyme validate PROGRAM_FILES...`

Schema validation plus semantic checks (unique step ids, dangling references,
cycles, overlapping steps within a track, negative offsets, replicate rules).

| Option | Meaning |
|---|---|
| `-j, --json` | machine-readable result; use this in a fix loop |
| `-s, --strict` | every task used by a step or buffer must be in `resourceConstraints` |
| `-e, --environment PATH` | validate against an environment file's constraints |
| `-v, --verbose` | detailed output |
| `--schema PATH` | use a schema file other than the built-in one |

Exit status is non-zero when any file is invalid.

### `rhylthyme publish PROGRAM`

`PROGRAM` is a file, an http(s) URL, or `-` for standard input (the same for
`analyze`). Publishes a live, shareable timeline through the hosted MCP server. The server
validates again and refuses an invalid program.

| Option | Meaning |
|---|---|
| `-e, --env` | `generic`, `kitchen`, `lab`, `events`, `gym`; default comes from the program's `environmentType` |
| `-q, --quiet` | print only the URL |
| `--json` | print `url`, `shareId`, `imageUrl`, `makespanSeconds`, `warnings` |
| `--image PATH` | also save a PNG of the timeline |
| `--open` | open the timeline in a browser |

Without `-q` or `--json` it prints a summary: total time, resources, an ASCII
Gantt chart, a chronological itinerary and a schedule check.

### `rhylthyme analyze PROGRAM`

Resolves the program onto a clock on the hosted MCP server and reports total
length, critical path, what gates each link of it, resource conflicts,
in-flight windows and tracks that finish early. Nothing is published.

| Option | Meaning |
|---|---|
| `--finish-at TEXT` | when everything must be finished: `19:00`, `7:30pm` (the next such local time) or an ISO 8601 datetime; prints each step's local clock time |
| `--start-at TEXT` | when the program starts, same formats; ignored with `--finish-at` |
| `--strict` | exit non-zero when there are resource conflicts |
| `--json` | the full analysis: `makespanSeconds`, `steps` (start and end per step), `criticalPath`, `bindingConstraints`, `resourceConflicts`, `inFlight`, `wallClock`, `validation` |

### `rhylthyme import URL_OR_ID_OR_FILE`

Imports a recipe (recipe sites, TheMealDB, Spoonacular, CookLang), a
protocol (protocols.io, Opentrons, Benchling) or a slide deck as a program,
validates it and writes `<programId>.json`. Needs the `rhylthyme-importers`
package.

| Option | Meaning |
|---|---|
| `-i IMPORTER` | which importer; default is chosen from the URL (`rhylthyme importers` lists them) |
| `-o PATH`, `--stdout` | where the program goes |
| `--publish`, `--open` | also publish a live timeline |
| `--no-validate` | keep an import that does not validate |
| `--review` | a model reads the import against its source and lists what looks wrong (durations, dropped steps, order, total); needs `rhylthyme login`; changes nothing |

`rhylthyme search QUERY -i themealdb|spoonacular|protocolsio` finds things
to import.

### `rhylthyme render PROGRAM_FILE -o OUT.svg|png|pdf [options]`

A static figure of the schedule (needs Node.js). `--style web|publication`,
`--palette`, `--color-by task`, `--start-at ISO`, `--legend right`;
`rhylthyme render --help` lists everything.

### `rhylthyme run PROGRAM_FILE`

Interactive terminal runner. Needs a real terminal.

| Option | Meaning |
|---|---|
| `--auto-start` | start without waiting for a keypress |
| `--time-scale FLOAT` | run faster than real time, for rehearsal (60 = a minute per second) |
| `-e, --environment TEXT` | environment file or id, overriding the program's |
| `--validate / --no-validate` | validate first (default on) |
| `--runs-dir PATH` | where run records go (default `$RHYLTHYME_RUNS_DIR` or `~/.rhylthyme/runs`) |
| `--no-record` | do not write a run record |
| `--factor KEY=VALUE` | answer a declared variance factor ahead of time (repeatable); also `RHYLTHYME_FACTORS='k=v,k2=v2'` |
| `--no-factor-prompt` | do not ask for variance factors |
| `--history PATH`, `--no-history` | run records to predict durations from, or none |
| `--predict-context KEY=VALUE` | context to predict for |

### `rhylthyme plan INPUT_FILE OUTPUT_FILE`

An older stagger heuristic. It predates the current program format (`stepId`,
`task`) and leaves programs written in it unchanged, so do not rely on it to
resolve conflicts: use `analyze` and edit the triggers.

### `rhylthyme environments` and `rhylthyme environment-info TYPE`

List the environment catalogs the tool can find (`-f table|json|yaml`) and
describe one environment type. Catalogs live in the
[rhylthyme-examples](https://github.com/rhylthyme/rhylthyme-examples)
repository; point at a checkout with `rhylthyme --environments-dir DIR ...`.
`rhylthyme validate-environments` checks catalog files.

### `rhylthyme runs list|show|report|evaluate`

| Command | Purpose |
|---|---|
| `runs list [PROGRAM]` | recorded runs, newest first (`--json`) |
| `runs show RUN` | planned against actual per step; `--svg PATH` draws the overlay (needs Node) |
| `runs report [PROGRAM]` | per-step statistics and a predictability verdict; `--all`, `--since`, `--min-runs`, `--cv-threshold`, `--format table\|md\|json` |
| `runs evaluate [PROGRAM]` | holds out the latest runs and compares history-based predictions with the planned durations; `--holdout` |

Only runs completed on a wall clock at speed 1 count as usable, and only steps
the person ended and never paused are measured.

### `rhylthyme calibrate PROGRAM`

Proposes durations from recorded runs. Reads and prints by default.

| Option | Meaning |
|---|---|
| `--min-runs INTEGER` | measurements a step needs (default 5) |
| `--low-pct`, `--high-pct` | percentiles proposed as the range (default 10 and 90) |
| `--indefinite-range` | also propose a range for indefinite steps |
| `--accept IDS\|all` | which proposals to apply |
| `--write PATH`, `--in-place` | where to write the calibrated program (needs `--accept`) |
| `--format table\|md\|json`, `--out PATH` | output |

### `rhylthyme mcp-test`

Smoke-tests a Rhylthyme MCP server (the hosted one by default, or `--url`).
Not needed for scheduling work.

## Needs an account

### `rhylthyme login`, `logout`, `whoami`

`login` opens the rhylthyme.com sign-in page and receives the session on a
one-shot listener bound to 127.0.0.1; the session is stored in
`~/.config/rhylthyme/credentials.json` (mode 0600) and renews itself.
`--token TOKEN` stores a pasted access token instead; `--no-browser` prints the
URL.

### `rhylthyme generate [REQUEST...]`

Authors a program on the server from a request or a source text, then
publishes it.

| Option | Meaning |
|---|---|
| `-f, --file PATH` | read the request or source text from a file, or `-` for stdin |
| `-e, --env` | `generic` (default), `kitchen`, `lab`, `events`, `gym` |
| `--by TEXT` | when everything must be finished, e.g. `16:00` |
| `--with TEXT` | equipment and people limits, e.g. `one centrifuge, two people` |
| `-o, --output PATH` | save the program JSON |
| `--no-publish` | build the program only |
| `--run` | run it in the terminal afterwards |
| `--open`, `--json`, `-q` | open the URL; JSON output; URL only |

Costs model calls, is capped per day, and takes 20 to 60 seconds.

## Environment variables

| Variable | Effect |
|---|---|
| `RHYLTHYME_TOKEN` | access token for `generate`, instead of `login` (lasts about an hour) |
| `RHYLTHYME_MCP_URL` | MCP server for `analyze`, `publish`, `generate`, `mcp-test` (default `https://mcp.rhylthyme.com/mcp`) |
| `RHYLTHYME_SITE_URL` | site used by `login` (default `https://www.rhylthyme.com`) |
| `RHYLTHYME_RUNS_DIR` | where run records are kept |
| `RHYLTHYME_FACTORS` | variance-factor answers for `run` |
