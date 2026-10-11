"""The bundled Claude skill (skills/rhylthyme) must stay true.

Every complete program in the skill has to pass the same validation the skill
tells its reader to run, and every command it names has to exist.
"""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from rhylthyme.cli.main import cli

SKILL = Path(__file__).resolve().parent.parent / "skills" / "rhylthyme"
DOCS = sorted([SKILL / "SKILL.md", *(SKILL / "references").glob("*.md")])
FENCE = re.compile(r"```json\n(.*?)\n```", re.S)


def complete_programs():
    for doc in DOCS:
        for i, block in enumerate(FENCE.findall(doc.read_text())):
            try:
                data = json.loads(block)
            except ValueError:
                continue  # a fragment with elisions, not a program
            if isinstance(data, dict) and "tracks" in data and "programId" in data:
                yield pytest.param(data, id=f"{doc.name}:{data['programId']}")


def test_frontmatter():
    text = (SKILL / "SKILL.md").read_text()
    assert text.startswith("---\n")
    front = text.split("---\n")[1]
    assert re.search(r"^name: rhylthyme$", front, re.M)
    description = re.search(r"^description: (.+)$", front, re.M).group(1)
    assert 200 < len(description) <= 1024
    assert re.search(r"^license: ", front, re.M)
    assert "skill-author:" in front


def test_referenced_files_exist():
    text = (SKILL / "SKILL.md").read_text()
    for ref in set(re.findall(r"`(references/[\w./-]+)`", text)):
        assert (SKILL / ref).is_file(), ref


def test_the_skill_has_programs():
    ids = [p.values[0]["programId"] for p in complete_programs()]
    assert len(ids) >= 4 and len(set(ids)) == len(ids)


@pytest.mark.parametrize("program", complete_programs())
def test_every_complete_program_is_valid(program, tmp_path):
    path = tmp_path / "program.json"
    path.write_text(json.dumps(program))
    result = CliRunner().invoke(cli, ["validate", str(path), "--strict"])
    assert result.exit_code == 0, result.output


def test_every_command_the_skill_names_exists():
    named = set()
    for doc in DOCS:
        for line in doc.read_text().splitlines():
            m = re.match(r"\s*(?:#+ `)?rhylthyme ([a-z][a-z-]+)", line)
            if m:
                named.add(m.group(1))
    assert {"validate", "analyze", "publish", "run", "calibrate", "generate"} <= named
    missing = named - set(cli.commands)
    assert not missing, missing


def test_the_readme_recipe_program_is_valid_and_is_what_the_readme_shows():
    root = SKILL.parent.parent
    recipe = root / "examples" / "recipe" / "birthday-party.json"
    result = CliRunner().invoke(cli, ["validate", str(recipe), "--strict"])
    assert result.exit_code == 0, result.output
    readme = (root / "README.md").read_text()
    assert "examples/recipe/birthday-party.json" in readme
    for step in json.loads(recipe.read_text())["tracks"][0]["steps"]:
        assert step["name"] in readme, f"README itinerary is missing {step['name']!r}"
