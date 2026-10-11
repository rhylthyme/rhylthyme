# rhylthyme-cli-runner (renamed to rhylthyme)

This package has been renamed to [rhylthyme](https://pypi.org/project/rhylthyme/).
Install that instead:

```bash
pip install rhylthyme            # the library and the rhylthyme command
pip install 'rhylthyme[all]'     # plus `rhylthyme import` and `rhylthyme render`
```

`rhylthyme-cli-runner` now only depends on `rhylthyme`. It keeps code that
still says `import rhylthyme_cli_runner` working, with a `DeprecationWarning`,
by mapping each old module to its new home:

| Old | New |
|---|---|
| `rhylthyme_cli_runner.<module>` | `rhylthyme.<module>` |
| `rhylthyme_cli_runner.cli` | `rhylthyme.cli.main` |
| `rhylthyme_cli_runner.remote`, `bridge`, `bridge_serve`, `import_cli`, `render_cli` | `rhylthyme.cli.…` |
| `rhylthyme_cli_runner.history.cli`, `*_cli`, `render` | `rhylthyme.cli.history.…` |
| `rhylthyme_cli_runner.eval.cli`, `harness`, `llm`, `patterns` | `rhylthyme.cli.eval.…` |
| `rhylthyme_cli_runner.program_runner.run_program`, `draw_ui`, … | `rhylthyme.cli.tui` |
| `rhylthyme_cli_runner.instruments.start_bridge` | `rhylthyme.cli.bridge.start_bridge` |

The shim will be removed in a later release.
