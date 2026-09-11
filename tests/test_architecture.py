"""Structural guarantees, enforced instead of asserted in a comment.

The package was extracted from a single 1386-line script. Splitting it is
only worth something if the properties that made the split desirable stay
true, so they are checked here rather than trusted:

* the runtime imports nothing outside the standard library, which is what
  lets it run from a checkout on a bare system
* modules form a layered graph with no cycles
* the entry point in bin/ stays a shim
* the public surface is what ``__all__`` says it is
"""

import ast
import os
import re
import sys
from pathlib import Path

import pytest
from conftest import repo_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = PROJECT_ROOT / "src" / "oscmix_desk"

# These assertions describe the *source* tree. A mutation run deliberately
# rewrites it -- mutmut inserts its own import into every mutated module --
# so asserting source properties there would only measure mutmut.
pytestmark = pytest.mark.skipif(
    bool(os.environ.get("MUTANT_UNDER_TEST")),
    reason="architecture describes the source tree, not a mutated copy",
)

# Which modules a module may import. A module absent from a value list is
# forbidden, so adding a dependency is a deliberate edit here, not an
# accident in an import block.
ALLOWED_IMPORTS = {
    "constants": set(),
    "errors": set(),
    "log": set(),
    "osc": set(),
    "notify": {"log"},
    "discovery": {"log"},
    # log is a leaf: one named logger, configured by the CLI entry point
    # before load_config runs. config gained it for the unknown-section
    # warning of ADR 0006 -- a warning has to reach the journal, and
    # returning it up the call chain would be a second error channel
    # beside ConfigError for no benefit.
    # registers is a leaf like constants: pure data about devices,
    # importing nothing from the package.
    "config": {"constants", "errors", "log", "registers"},
    # link_messages/mix_messages moved down into reconcile: they are
    # pure message shapes, and keeping them here made reconcile sit
    # above routing while routing wanted to call it -- a cycle.
    "routing": {"backend", "config", "constants", "log", "osc", "reconcile"},
    "verify": {"backend", "config", "constants", "log", "osc", "reconcile",
               "registers", "routing"},
    "pipewire": {"config", "errors", "log"},
    "process": {"constants", "discovery", "log"},
    # `profiles` since 0.6.3: a reload has to apply the same desk a
    # start does, and "the active profile, else routing.conf" is
    # answered in profiles.effective_config. profiles sits above verify
    # and imports nothing from session, so no cycle.
    "session": {"config", "constants", "discovery", "errors", "log",
                "notify", "process", "profiles", "reconcile", "routing",
                "verify"},
    # `discovery` since 0.6.2: the snapshot header names the device's
    # serial and firmware, which are the leaf's to answer. A leaf with no
    # imports of its own, already below session; cli reading it changes
    # no direction in the graph.
    "cli": {"backend", "config", "constants", "discovery", "errors", "log",
            "pipewire", "profiles", "reconcile", "registers", "session"},
    # Sits above verify because a switch has to report whether the
    # device confirmed it. Below cli because the outcome is a value, not
    # an exit code -- the mapping to one is the CLI's business.
    "profiles": {"backend", "config", "constants", "errors", "log",
                 "registers", "routing", "verify"},
    "launcher": {"constants", "discovery"},
    # constants only, and only for the fader range: the register table
    # declares the device's bounds, and writing -65.0/6.0 here as well
    # would be the same fact in two files -- which is how a validator and
    # a register table come to disagree. constants imports nothing
    # itself, so the graph stays acyclic.
    "registers": {"constants"},
    # The one place that opens a socket to the device. Its Traits name
    # the upstream behaviour the timing constants work around.
    "backend": {"osc"},
    # Pure: config + the message shapes + the register table. No
    # socket, no clock -- which is what lets it be tested against
    # recordings instead of hardware.
    "reconcile": {"config", "constants", "registers"},
    "__init__": {"config", "constants", "discovery", "errors", "launcher",
                 "log", "notify", "osc", "pipewire", "process", "profiles",
                 "reconcile", "registers", "routing", "session", "verify"},

}


def module_paths():
    return sorted(PACKAGE.glob("*.py"))


def parse(path):
    return ast.parse(path.read_text(), filename=str(path))


def imports_of(path):
    """(stdlib_or_absolute, relative) module names imported by ``path``."""
    absolute, relative = set(), set()
    for node in ast.walk(parse(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                absolute.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:                       # from .x import y
                if node.module:
                    relative.add(node.module.split(".")[0])
            elif node.module:
                absolute.add(node.module.split(".")[0])
    return absolute, relative


def test_every_module_is_listed_in_the_layering():
    # A new module must be placed in the graph deliberately; defaulting to
    # "anything goes" would make this test decorative.
    listed = set(ALLOWED_IMPORTS)
    actual = {path.stem for path in module_paths()}
    assert actual == listed, (
        "modules not placed in ALLOWED_IMPORTS: %s; listed but missing: %s"
        % (sorted(actual - listed), sorted(listed - actual))
    )


@pytest.mark.parametrize("path", module_paths(), ids=lambda p: p.stem)
def test_runtime_imports_only_the_standard_library(path):
    # The one dependency claim the README makes. It is what allows the
    # package to run before any package manager is involved.
    absolute, _ = imports_of(path)
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    if not stdlib:                               # pragma: no cover (py<3.10)
        pytest.skip("sys.stdlib_module_names needs Python 3.10+")
    foreign = {name for name in absolute
               if name not in stdlib and not name.startswith("_")}
    assert foreign == set(), "%s imports non-stdlib: %s" % (path.name,
                                                            sorted(foreign))


@pytest.mark.parametrize("path", module_paths(), ids=lambda p: p.stem)
def test_module_only_imports_its_declared_layer(path):
    _, relative = imports_of(path)
    allowed = ALLOWED_IMPORTS[path.stem]
    assert relative <= allowed, (
        "%s imports %s, which its layer does not allow (allowed: %s)"
        % (path.name, sorted(relative - allowed), sorted(allowed) or "nothing")
    )


def test_the_import_graph_is_acyclic():
    # Guaranteed by the layering above, but stated separately: a cycle is
    # the failure this whole arrangement exists to prevent.
    graph = {path.stem: imports_of(path)[1] for path in module_paths()}
    visiting, done = set(), set()

    def visit(name, trail):
        if name in done:
            return
        assert name not in visiting, "import cycle: %s" % " -> ".join(
            trail + [name])
        visiting.add(name)
        for dependency in sorted(graph.get(name, ())):
            visit(dependency, trail + [name])
        visiting.discard(name)
        done.add(name)

    for module in sorted(graph):
        visit(module, [])


@pytest.mark.parametrize("name", ["oscmix-session", "oscmix-launch"])
def test_the_entry_points_stay_shims(name):
    # Logic in bin/ is logic that unit tests cannot reach, because the
    # files have no .py suffix and are only ever run as subprocesses.
    # The launcher was the last exception to this and to everything else
    # item 1 established; it is a shim now too.
    tree = parse(PROJECT_ROOT / "bin" / name)
    defined = [node.name for node in tree.body
               if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    assert defined == ["_package_root"], (
        "bin/oscmix-session should only locate the package, but defines %s"
        % defined)


def test_public_surface_matches_dunder_all(session_mod):
    exported = {name for name in vars(session_mod)
                if not name.startswith("_")
                and getattr(vars(session_mod)[name], "__module__", "")
                .startswith("oscmix_desk")}
    declared = set(session_mod.__all__)
    assert exported <= declared, (
        "re-exported but not declared in __all__: %s" % sorted(exported - declared))
    missing = {name for name in declared if not hasattr(session_mod, name)}
    assert missing == set(), "declared in __all__ but absent: %s" % sorted(missing)


@pytest.mark.parametrize("path", module_paths(), ids=lambda p: p.stem)
def test_functions_stay_readable(path):
    # run_session was 106 lines before the split and did six things. The
    # ceiling is a smell detector, not a style rule.
    too_long = []
    for node in ast.walk(parse(path)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = (node.end_lineno or node.lineno) - node.lineno
            if length > 70:
                too_long.append("%s (%d lines)" % (node.name, length))
    assert too_long == [], "%s has oversized functions: %s" % (path.name,
                                                               too_long)


@pytest.mark.parametrize("path", module_paths(), ids=lambda p: p.stem)
def test_every_module_documents_itself(path):
    assert ast.get_docstring(parse(path)), "%s has no module docstring" % path.name


def named_in_tests():
    """Every identifier any test file mentions."""
    mentioned = set()
    for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.Attribute):
                mentioned.add(node.attr)
            elif isinstance(node, ast.Name):
                mentioned.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    mentioned.add(alias.asname or alias.name.split(".")[0])
    return mentioned


def test_every_public_name_is_exercised_by_some_test(session_mod):
    """A declared public surface nobody tests is a promise nobody checks.

    Weaker than "has a dedicated test" on purpose: this catches a name
    that was exported and then forgotten, without pretending that being
    mentioned equals being covered. Coverage and the mutation score are
    the measures of *how well*; this one is about *at all*.
    """
    untested = sorted(name for name in session_mod.__all__
                      if name not in named_in_tests())
    assert untested == [], (
        "declared in __all__ but named by no test: %s" % untested)


# --------------------------------------------------------------------------
# The mutation exemption, ADR 0015.
# --------------------------------------------------------------------------

def _exempt_lines():
    """(first, last) line of the `no mutate` region in registers.py."""
    source = (PACKAGE / "registers.py").read_text().splitlines()
    starts = [i for i, line in enumerate(source, 1)
              if line.strip() == "# pragma: no mutate start"]
    ends = [i for i, line in enumerate(source, 1)
            if line.strip() == "# pragma: no mutate end"]
    assert len(starts) == 1, "expected one no-mutate start, found %d" % len(starts)
    assert len(ends) == 1, "expected one no-mutate end, found %d" % len(ends)
    assert starts[0] < ends[0]
    return starts[0], ends[0]


def _defined_at(name):
    """The line a top-level name is bound on in registers.py."""
    for node in ast.walk(parse(PACKAGE / "registers.py")):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node.lineno
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return node.lineno
        if (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name):
            return node.lineno
    raise AssertionError("registers.py defines no %s" % name)


@pytest.mark.parametrize("name", ["_seq", "_EQ_BANDS", "_ROOMEQ_BANDS", "_HIGH_SHELF",
                                  "_band_registers", "_roomeq_registers",
                                  "_DYNAMICS_OPTIONS", "_AUTOLEVEL_OPTIONS",
                                  "_LOWCUT_OPTIONS",
                                  "_sub_registers",
                                  "UCX2", "FF802", "DEVICES"])
def test_the_register_table_is_inside_the_mutation_exemption(name):
    """ADR 0015: the data is checked by the recordings, not by mutmut."""
    first, last = _exempt_lines()
    assert first < _defined_at(name) < last


@pytest.mark.parametrize("name", ["device_for_name", "settable_options",
                                  "settable_nested", "nested_families",
                                  "register_at", "cold_plug_complete",
                                  "declared_paths", "register_policy",
                                  "verify_class", "settable_globals"])
def test_everything_that_queries_the_table_stays_under_mutation(name):
    """The half of ADR 0015 that keeps it honest.

    Exempting data is defensible because the recordings check it harder.
    Exempting the functions that read that data would not be -- a wrong
    answer there is behaviour, and nothing else is measuring it.
    """
    _, last = _exempt_lines()
    assert _defined_at(name) > last


# --------------------------------------------------------------------------
# The architecture page, checked against the code.
# --------------------------------------------------------------------------

def documented_modules():
    """Module names the architecture page's module table lists."""
    text = repo_file("docs", "ARCHITECTURE.md").read_text()
    table = text[text.index("## The modules"):]
    table = table[:table.index("\n## ")]
    return set(re.findall(r"^\| `([a-z_]+)` \|", table, re.MULTILINE))


def test_the_architecture_page_names_every_runtime_module():
    """A page that drifts is worse than none, because it is believed.

    `docs/ARCHITECTURE.md` was 186 lines describing the 0.2.0 system and
    mentioning none of `reconcile`, `desired`, `plan`, `snapshot` or
    `profiles`. It did not drift slowly: it was never checked, so there
    was nothing to notice.
    """
    actual = {path.stem for path in module_paths()}
    missing = sorted(actual - documented_modules())
    assert missing == [], (
        "runtime modules absent from docs/ARCHITECTURE.md: %s" % missing)


def test_the_architecture_page_invents_no_modules():
    """The other direction, which is how a page survives a deletion."""
    actual = {path.stem for path in module_paths()}
    invented = sorted(documented_modules() - actual)
    assert invented == [], (
        "docs/ARCHITECTURE.md names modules that do not exist: %s" % invented)


def test_the_page_carries_no_history():
    """Chronicle belongs in the roadmap, decisions in the ADRs.

    Not style policing: the page rotted because it mixed "how it works"
    with "how we got here", and the second kind of sentence is the kind
    nobody updates. Version numbers in prose are the tell.
    """
    text = repo_file("docs", "ARCHITECTURE.md").read_text()
    body = text[text.index("## The system it sits in"):text.index("## Design decisions")]
    dated = re.findall(r"\b0\.\d\.\d\b", body)
    assert dated == [], (
        "version numbers in the architecture body belong in the roadmap: %s"
        % dated)
