# rhylthyme

[![PyPI](https://img.shields.io/pypi/v/rhylthyme)](https://pypi.org/project/rhylthyme/)
[![CI](https://github.com/rhylthyme/rhylthyme/actions/workflows/ci.yml/badge.svg)](https://github.com/rhylthyme/rhylthyme/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

The Python library and the `rhylthyme` command for real-time schedules: validate, plan and run programs of parallel tracks against a clock, and publish them as live timelines.

## A timeline in five commands: a birthday party

Parents collect at 4:00. Working backwards from that: when does the cake go in
the oven, when do the balloons go up, and do the pizzas and the cake ever want
the oven at the same time?

```bash
pip install rhylthyme

# 1. a program: the party, the kitchen and the setup crew, 13 steps
curl -sO https://raw.githubusercontent.com/rhylthyme/rhylthyme/main/examples/recipe/birthday-party.json

# 2. is it well formed?
rhylthyme validate birthday-party.json --strict

# 3. any clashes, and what time does everything start if we finish at 4?
rhylthyme analyze birthday-party.json --finish-at 16:00
```

```
**Makespan:** 3h 10m across 3 tracks, 13 steps.
**Critical path:** Guests arrive → Party games → Pizza → Candles and cake → Piñata, until it breaks → Party bags and goodbyes
**Binding constraints:** step dependencies only — no resource or in-flight limit gates the critical path.
**Peak concurrency:** 2 steps at 10:00.
**Resource conflicts:** none.

**Tracks that finish early:** Kitchen (1h 10m early), Setup crew (50m early)

Start Sat 12:50, finish Sat 16:00 (local time)
  Sat 12:50  Bake the cake
  Sat 13:00  Balloons and banner
  Sat 13:25  Cake cools
  Sat 13:30  Hang the piñata
  Sat 13:50  Guests arrive
  Sat 14:05  Frost and decorate
  Sat 14:20  Party games
  Sat 14:35  Pizzas in the oven
  Sat 14:50  Fill party bags
  Sat 14:50  Pizza
  Sat 15:15  Candles and cake
  Sat 15:30  Piñata, until it breaks
  Sat 15:45  Party bags and goodbyes
```

```bash
# 4. a live timeline for the fridge door (or a phone): timers, cues, and you end the piñata when it breaks
rhylthyme publish birthday-party.json --open --image party-preview.png

# 5. a picture for the group chat, on the wall clock (needs Node.js)
rhylthyme render birthday-party.json -o birthday-party.png \
  --style web --palette vivid --label-overflow outside --start-at "$(date +%F)T12:50:00"
```

![Eighth birthday party: the party, the kitchen and the setup crew on a wall-clock axis](https://raw.githubusercontent.com/rhylthyme/rhylthyme/main/examples/recipe/birthday-party.png)

The pizzas go in 15 minutes after the games *start*, so they come out as the
games end; the party bags get filled while everyone is eating. Try
`--finish-at 17:30`, or turn the cake into a two-hour fruitcake (`"seconds": "2h"`
on `bake`) and `analyze` reports the clash: `oven needs 2 but max is 1 from
1:45:00 to 2:00:00 (bake, heat-pizza)`. `rhylthyme run birthday-party.json --time-scale 60` rehearses
the whole afternoon in the terminal at a minute per second.

## Install

```bash
pip install rhylthyme              # "rhylthyme[all]" adds `rhylthyme import` and `rhylthyme render`
```

```python
import rhylthyme, rhylthyme_spec
result = rhylthyme.validate_program_file_structured("birthday-party.json", rhylthyme_spec.get_program_schema_path("0.3.0-alpha"))
print(result["is_valid"], [f["code"] for f in result["findings"]])
```

## Links

| | |
|---|---|
| Documentation | [docs.rhylthyme.com](https://docs.rhylthyme.com) ([command reference](https://docs.rhylthyme.com/development/command-reference/)) |
| Program format | [rhylthyme-spec](https://github.com/rhylthyme/rhylthyme-spec) |
| Example programs | [rhylthyme-examples](https://github.com/rhylthyme/rhylthyme-examples) |
| MCP server | [rhylthyme-mcp](https://github.com/rhylthyme/rhylthyme-mcp) ([how it differs from this package](https://docs.rhylthyme.com/getting-started/rhylthyme-and-rhylthyme-mcp/)) |
| R package | [rhylthyme-r](https://github.com/rhylthyme/rhylthyme-r) |
| Importers | [rhylthyme-importers](https://github.com/rhylthyme/rhylthyme-importers) |

## Contributing

See the [contributing guide](https://docs.rhylthyme.com/development/contributing/); `make ci-test` runs the checks.

## License

Apache-2.0; see [LICENSE](LICENSE).
