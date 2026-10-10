"""`rhylthyme import`, `search` and `importers`, with a fake importer registry
so nothing is fetched."""

import json
import sys
import types

import pytest
from click.testing import CliRunner

from rhylthyme.cli.main import cli

PROGRAM = {
    "schemaVersion": "0.3.0-alpha",
    "programId": "fake-dinner",
    "name": "Fake dinner",
    "environmentType": "kitchen",
    "tracks": [
        {
            "trackId": "a",
            "name": "A",
            "steps": [
                {
                    "stepId": "s1",
                    "name": "Cook",
                    "task": "stove",
                    "duration": {"type": "fixed", "seconds": 600},
                    "startTrigger": {"type": "programStart"},
                }
            ],
        }
    ],
    "resourceConstraints": [{"task": "stove", "maxConcurrent": 1}],
}
BAD = dict(
    PROGRAM,
    tracks=[
        {
            "trackId": "a",
            "name": "A",
            "steps": [
                {
                    "stepId": "s1",
                    "name": "Cook",
                    "task": "stove",
                    "duration": {"type": "fixed", "seconds": 600},
                    "startTrigger": {"type": "afterStep", "stepId": "nope"},
                }
            ],
        }
    ],
)


class Result:
    def __init__(self, program=None, error=None):
        self.success = program is not None
        self.program = program
        self.error = error


class FakeImporter:
    name = "fake"
    description = "Fake source"
    supported_domains = ["fake.example"]
    allow_local_files = False

    def __init__(self):
        self.calls = []

    def can_import(self, s):
        return "fake.example" in s

    def search(self, q):
        return [
            {
                "name": f"Hit for {q}",
                "url": "https://fake.example/1",
                "description": "d",
            }
        ]

    def import_from_url(self, url):
        self.calls.append(("url", url, self.allow_local_files))
        return Result(BAD if "bad" in url else PROGRAM)

    def import_from_source(self, text):
        self.calls.append(("text", text, None))
        return Result(PROGRAM)


@pytest.fixture
def fake(monkeypatch):
    importer = FakeImporter()

    class Registry:
        @staticmethod
        def get(name):
            return importer if name == "fake" else None

        @staticmethod
        def find_for_url(url):
            return importer if importer.can_import(url) else None

        @staticmethod
        def list_importers():
            return [
                {
                    "name": "fake",
                    "description": "Fake source",
                    "supported_domains": ["fake.example"],
                }
            ]

    module = types.ModuleType("rhylthyme_importers")
    module.ImporterRegistry = Registry
    monkeypatch.setitem(sys.modules, "rhylthyme_importers", module)
    return importer


def test_import_by_url_writes_a_named_file(fake, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli, ["import", "https://fake.example/dinner"])
    assert r.exit_code == 0, r.output
    assert (
        json.loads((tmp_path / "fake-dinner.json").read_text())["name"] == "Fake dinner"
    )
    assert "Importing with fake" in r.output


def test_explicit_importer_output_path_and_stdout(fake, tmp_path):
    out = tmp_path / "d.json"
    r = CliRunner().invoke(cli, ["import", "12345", "-i", "fake", "-o", str(out)])
    assert (
        r.exit_code == 0 and json.loads(out.read_text())["programId"] == "fake-dinner"
    )
    r = CliRunner().invoke(cli, ["import", "12345", "-i", "fake", "--stdout"])
    assert r.exit_code == 0 and '"programId": "fake-dinner"' in r.output


def test_a_local_file_is_allowed_from_the_command_line(fake, tmp_path):
    f = tmp_path / "protocol.py"
    f.write_text("def run(protocol): pass\n")
    r = CliRunner().invoke(cli, ["import", str(f), "-i", "fake", "--stdout"])
    assert r.exit_code == 0, r.output
    assert fake.calls[-1] == (
        "url",
        str(f),
        True,
    ), "the CLI opts in to local files; servers never do"


def test_stdin_needs_an_importer_and_goes_to_import_from_source(fake):
    r = CliRunner().invoke(cli, ["import", "-"], input="text")
    assert r.exit_code != 0 and "-i" in r.output
    r = CliRunner().invoke(
        cli, ["import", "-", "-i", "fake", "--stdout"], input="def run(p): pass"
    )
    assert r.exit_code == 0 and fake.calls[-1][0] == "text"


def test_an_invalid_import_is_reported_and_not_published(fake, tmp_path):
    r = CliRunner().invoke(
        cli,
        [
            "import",
            "https://fake.example/bad",
            "-o",
            str(tmp_path / "x.json"),
            "--publish",
        ],
    )
    assert r.exit_code != 0
    assert "does not validate" in r.output and "nope" in r.output
    assert not (tmp_path / "x.json").exists()


def test_publish_after_import(fake, tmp_path, monkeypatch):
    from rhylthyme.cli.remote import cli as remote

    seen = {}

    def fake_publish(
        program_file, environment, open_url, json_output, quiet, image_path
    ):
        seen["program"] = json.loads(open(program_file).read())
        import click

        click.echo("https://kitchen.rhylthyme.com?share=abc")

    monkeypatch.setattr(remote.publish, "callback", fake_publish)
    r = CliRunner().invoke(
        cli,
        [
            "import",
            "https://fake.example/dinner",
            "-o",
            str(tmp_path / "d.json"),
            "--publish",
        ],
    )
    assert r.exit_code == 0, r.output
    assert seen["program"]["programId"] == "fake-dinner"
    assert "share=abc" in r.output


def test_unknown_importer_and_unrecognised_source(fake):
    r = CliRunner().invoke(cli, ["import", "x", "-i", "nope"])
    assert r.exit_code != 0 and "Unknown importer" in r.output and "fake" in r.output
    r = CliRunner().invoke(cli, ["import", "https://example.org/x"])
    assert r.exit_code != 0 and "No importer recognises" in r.output


def test_search_and_importers(fake):
    r = CliRunner().invoke(cli, ["search", "chili", "-i", "fake"])
    assert (
        r.exit_code == 0
        and "Hit for chili" in r.output
        and "https://fake.example/1" in r.output
    )
    r = CliRunner().invoke(cli, ["importers"])
    assert r.exit_code == 0 and "fake" in r.output and "fake.example" in r.output


def test_without_the_importers_package_the_message_says_how_to_get_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "rhylthyme_importers", None)
    r = CliRunner().invoke(cli, ["import", "https://fake.example/dinner"])
    assert r.exit_code != 0 and "rhylthyme[importers]" in r.output


def test_a_text_importer_gets_a_urls_body(fake, monkeypatch):
    from rhylthyme.cli.remote import cli as remote

    monkeypatch.setattr(
        remote, "_fetch_bytes", lambda url, limit=0: b"def run(protocol): pass\n"
    )
    fake.supported_domains = []  # a file/text importer, like Opentrons
    r = CliRunner().invoke(
        cli, ["import", "https://fake.example/protocol.py", "-i", "fake", "--stdout"]
    )
    assert r.exit_code == 0, r.output
    assert fake.calls[-1] == ("text", "def run(protocol): pass\n", None)


def test_a_missing_local_file_says_so(fake, tmp_path):
    r = CliRunner().invoke(
        cli, ["import", str(tmp_path / "Neapolitan Pizza.cook"), "-i", "fake"]
    )
    assert r.exit_code != 0 and "No such file" in r.output
    assert fake.calls == []


def test_review_needs_a_login_before_any_work(fake, monkeypatch):
    monkeypatch.delenv("RHYLTHYME_TOKEN", raising=False)
    from rhylthyme.cli.remote import auth

    monkeypatch.setattr(auth, "load_credentials", lambda path=None: None)
    r = CliRunner().invoke(
        cli, ["import", "https://fake.example/dinner", "--review", "--stdout"]
    )
    assert r.exit_code != 0 and "rhylthyme login" in r.output
    assert fake.calls == [], "nothing was imported"


def test_review_sends_the_program_and_source_and_prints_findings(
    fake, monkeypatch, tmp_path
):
    monkeypatch.setenv("RHYLTHYME_TOKEN", "tok-1")
    from rhylthyme.cli.remote import mcp_client as MC

    seen = {}

    class FakeMcp:
        def __init__(self, url=None, **kw):
            seen["url"] = url

        def call_tool(self, name, args, timeout=None, **kw):
            seen["tool"], seen["args"] = name, args
            return MC.ToolResult(
                content=[{"type": "text", "text": "x"}],
                structured={
                    "summary": "One problem.",
                    "usable": False,
                    "findings": [
                        {
                            "severity": "error",
                            "stepId": "s1",
                            "message": "Too short.",
                            "suggestion": "90m",
                        },
                        {"severity": "note", "message": "Fine."},
                    ],
                },
            )

    monkeypatch.setattr(MC, "McpClient", FakeMcp)
    src = tmp_path / "dinner.cook"
    src.write_text("Simmer @sauce for ~{90%minutes}.\n")
    fake.supported_domains = []
    r = CliRunner().invoke(
        cli,
        ["import", str(src), "-i", "fake", "--review", "-o", str(tmp_path / "d.json")],
    )
    assert r.exit_code == 0, r.output
    assert seen["tool"] == "review_program"
    assert seen["args"]["token"] == "tok-1"
    assert seen["args"]["program"]["programId"] == "fake-dinner"
    assert seen["args"]["source_text"].startswith("Simmer @sauce")
    assert (
        "One problem." in r.output
        and "✗ [s1] Too short." in r.output
        and "→ 90m" in r.output
    )
