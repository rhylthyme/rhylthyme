"""``rhylthyme render``: the timeline renderer from rhylthyme-timeline,
reached from the one command people already have. Every option of the
``rhylthyme-render`` command is accepted unchanged."""

from __future__ import annotations

import click

INSTALL_HINT = (
    "rhylthyme render needs the timeline extra: pip install 'rhylthyme[timeline]'. "
    "It runs on Node.js 18 or newer."
)


@click.command(
    "render",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": [],
    },
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def render_command(ctx, args):
    """Draw a program as an SVG, PNG or PDF figure (needs Node.js).

    \b
      rhylthyme render dinner.json -o dinner.png --style web --palette vivid
      rhylthyme render blot.json -o figure.pdf --style publication --legend right
      rhylthyme render --help          # every option
    """
    try:
        from rhylthyme_timeline import main as render_main
    except ImportError:
        raise click.ClickException(INSTALL_HINT)
    ctx.exit(render_main(list(args)))


def register(cli_group: click.Group) -> None:
    cli_group.add_command(render_command)
