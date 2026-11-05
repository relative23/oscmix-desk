"""The command-line grammar changes only on purpose, and the documentation names every option.

`tests/cli_grammar.json` is the grammar as data: options, metavars, types, defaults and help
text (not a measured fixture, so not in tests/data). A change to any of them fails here until
the file is regenerated (`PYTHONPATH=src:tests python tests/cli_grammar.py >
tests/cli_grammar.json`), which puts the change in front of a reviewer instead of letting it
slip out with a refactoring.
"""

import json

from cli_grammar import grammar
from support import repo_file


def test_the_grammar_is_the_recorded_one():
    recorded = json.loads(repo_file("tests", "cli_grammar.json").read_text())
    assert grammar() == recorded


def test_every_option_is_documented():
    documented = (repo_file("README.md").read_text()
                  + repo_file("docs", "FEATURE-SURFACE.md").read_text())
    missing = [option for row in grammar()["options"] for option in row["options"]
               if option.startswith("--") and option not in documented]
    assert missing == []
