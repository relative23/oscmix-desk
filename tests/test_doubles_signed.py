"""A test double of this project's code takes the arguments of what it replaces.

`lambda *a: value` takes anything, so a caller that drops, adds or misnames an argument still
passes against it, and the mutation run of 0.10.1 left such changes alive behind them.
`signed` in tests/support.py binds every call to the real signature first. Doubles of the
standard library (`os`, `time`, `socket`, ...) are outside this rule.
"""

import re

from support import repo_file

#: monkeypatch.setattr(<a module of this project>, "<name>", lambda *...)
ANY_ARGUMENTS = re.compile(r'setattr\(([A-Za-z_]\w*), *"[A-Za-z_]\w*", *lambda \*')


def test_no_double_of_project_code_takes_any_arguments():
    found = []
    for path in sorted(repo_file("tests").glob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            match = ANY_ARGUMENTS.search(line)
            if match:
                found.append("%s:%d" % (path.name, number))
    assert found == [], "use support.signed(real, value) or body=: %s" % found
