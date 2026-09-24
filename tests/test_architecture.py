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
from support import repo_file

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
    # `errors` since 0.6.9: two identical interfaces without
    # `[device] serial` are a configuration the user has to fix, and the
    # leaf that finds the interfaces is the one that can tell (ADR 0024).
    # errors is itself a leaf, so no direction in the graph changes.
    "discovery": {"errors", "log"},
    # log is a leaf: one named logger, configured by the CLI entry point
    # before load_config runs. The section parsers have it for the
    # unknown-section warning of ADR 0006 -- a warning has to reach the
    # journal, and returning it up the call chain would be a second error
    # channel beside ConfigError for no benefit. The loader itself logs
    # nothing.
    # registers and devices are near-leaves like constants: the shape of
    # a register row, and the rows. devices sits on registers, since a
    # table is made of rows, and everything that asks which device a
    # config names reads devices.
    "config": {"constants", "devices", "errors", "model", "registers",
               "sections"},
    # link_messages/mix_messages moved down into reconcile: they are
    # pure message shapes, and keeping them here made reconcile sit
    # above routing while routing wanted to call it -- a cycle.
    # `errors` since 0.6.11, here and in verify: both have to tell a
    # receive port that cannot be bound from one that is held (ADR 0025),
    # and the leaf is where that exception lives -- imported from there,
    # because `__init__` is the only module that re-exports.
    "routing": {"backend", "constants", "errors", "log", "model", "numeric",
                "reconcile", "streams"},
    "streams": {"constants", "log", "model"},
    "verify": {"backend", "constants", "devices", "errors", "log", "model",
               "numeric", "osc", "reconcile", "registers", "routing"},
    "pipewire": {"errors", "model"},
    "process": {"constants", "discovery", "log"},
    # `locking` since 0.7.0: the unit takes the device lock itself around
    # its apply and its verifier, and the lock is a module of its own now
    # rather than a part of profiles. What the desk in effect is, session
    # asks reload.
    # `devices` since 0.7.0: a start says when the interface reports
    # another firmware than the one its register table was recorded on.
    "session": {"constants", "devices", "discovery", "errors", "locking",
                "log", "model", "notices", "notify", "process", "reconcile",
                "reload", "routing", "verify"},
    # What a running session does with a desk it reads again, split out
    # of session in 0.7.0: below session, which starts it and hands it
    # the SIGHUP. `profiles` because a reload has to apply the same desk
    # a start does, and "the active profile, else routing.conf" is
    # answered in profiles.effective_config; profiles sits above verify
    # and imports nothing from here, so no cycle. `locking` for the lock
    # around every reconcile.
    "reload": {"constants", "discovery", "errors", "locking", "log", "model",
               "notices", "notify", "paths", "profiles", "verify"},
    # `process` since 0.6.3: an applied profile switch reloads the unit
    # so its own verifier cannot revert it; process already sits below
    # session and imports nothing above discovery.
    "cli": {"config", "constants", "errors", "log", "model", "notices",
            "outcome", "paths", "pipewire", "preview", "process", "profiles", "reads",
            "session", "status"},
    # The three actions that read the device, split out of cli in 0.7.0.
    # `discovery` since 0.6.2: the snapshot header names the device's
    # serial and firmware, which are the leaf's to answer. `process` for
    # whose backend holds the port, `dump` for the device's state as a
    # config.
    "reads": {"backend", "constants", "devices", "discovery", "dump", "errors",
              "log", "model", "osc", "process", "reconcile"},
    # Sits above verify because a switch has to report whether the
    # device confirmed it. Below cli because the outcome is a value, not
    # an exit code -- the mapping to one is the CLI's business.
    # `discovery` since 0.6.7: the device lock is keyed by the interface,
    # and the serial that names it is the leaf's to answer (ADR 0022).
    # `process` since 0.6.9: a switch accepts the OSC port only from an
    # oscmix of this user that bridges the resolved interface, which is
    # the question the start's stale cleanup already asks through
    # process.socket_owner (ADR 0024). process imports nothing above
    # discovery, so no cycle.
    "profiles": {"backend", "config", "constants", "devices", "discovery",
                 "errors", "locking", "log", "marker", "model", "notices",
                 "outcome", "paths", "process", "routing", "verify"},
    # The three things a switch is made of besides its order, split out of
    # profiles in 0.7.0. Each is a near-leaf: the lock knows its wait and
    # the journal, the marker knows what a profile name is, and an outcome
    # is a value that imports nothing.
    "locking": {"constants", "log"},
    "marker": {"errors", "log", "paths"},
    "outcome": set(),
    "launcher": {"config", "constants", "desktop", "diagnostics", "discovery",
                 "errors", "model", "paths"},
    "diagnostics": {"constants", "discovery", "errors", "model", "paths", "process"},
    "desktop": {"diagnostics", "discovery", "model"},
    "status": {"constants", "desktop", "diagnostics", "discovery", "errors",
               "marker", "model", "paths", "process", "profiles", "streams"},
    "preview": {"model", "reconcile"},
    # What config.py was until 0.7.0, by what each part is. `model` is the
    # desk as data and what nearly everything reads -- the reconciler, the
    # router, the verifier and the sink generator stopped depending on
    # the parser the day it moved out. `paths` is where a desk is looked
    # for, `sections` the parsers the register table drives, `notices`
    # what there is to say about a desk; `config` is the loader on top.
    "model": {"constants", "registers"},
    "paths": {"errors"},
    "sections": {"devices", "errors", "log", "model", "numeric", "registers"},
    "numeric": {"constants", "registers"},
    "notices": {"devices", "log", "model"},
    # A leaf: the shape of a row and of a device, and the questions asked
    # of a table somebody hands it.
    "registers": set(),
    # constants only for the fader range: the register table declares the
    # device's bounds, and writing -65.0/6.0 here as well would be the
    # same fact in two files -- which is how a validator and a register
    # table come to disagree.
    "devices": {"constants", "registers"},
    # The one place that opens a socket to the device. Its Traits name
    # the upstream behaviour the timing constants work around.
    # `errors` since 0.6.11: a receive port that cannot be bound for a
    # reason other than a holder is an exception every caller has to be
    # able to name, and errors is a leaf.
    "backend": {"errors", "osc"},
    # Pure: config + the message shapes + the register table. No
    # socket, no clock -- which is what lets it be tested against
    # recordings instead of hardware.
    # `osc` since 0.7.0, here and in whatever handles a register's value:
    # the leaf names what a value on this wire is, so that it is not an
    # `object` each reader casts past the type checker.
    "reconcile": {"constants", "devices", "model", "numeric", "osc", "registers"},
    # The other direction, split out of reconcile in 0.7.0: what the device
    # reports, as a config and as its text. As pure as the reconciler,
    # whose message shapes and policy it reads.
    "dump": {"constants", "model", "numeric", "osc", "reconcile", "registers"},
    # The supported surface and nothing else since 0.7.0: what it imports
    # is what it re-exports, and the leaves it used to pull in for the
    # sake of their internals are reached through their own modules.
    "__init__": {"config", "constants", "errors", "launcher", "marker",
                 "model", "outcome", "paths", "pipewire", "profiles",
                 "routing", "session", "verify"},

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
                else:                            # from . import x, y
                    relative.update(alias.name for alias in node.names)
            elif node.module:
                absolute.add(node.module.split(".")[0])
    return absolute, relative


def test_the_scanner_sees_every_form_of_relative_import(tmp_path):
    """`from . import x` has no module name, and was dropped: the one form
    that could pass both directions of the layer check unseen. None
    exists in the package; the map is only "exact" if one would count."""
    probe = tmp_path / "probe.py"
    probe.write_text("import os\nfrom . import osc, log\n"
                     "from .config import Config\n"
                     "def f():\n    from .errors import ConfigError\n")
    assert imports_of(probe) == ({"os"}, {"osc", "log", "config", "errors"})


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


@pytest.mark.parametrize("path", module_paths(), ids=lambda p: p.stem)
def test_the_declared_layer_is_what_the_module_imports(path):
    """The other direction. An edge nothing uses is a permission nobody
    reviewed: `routing -> osc`, `verify -> osc` and `pipewire -> log` sat
    in the map long after the imports were gone, and the package
    docstring described a graph from 0.2.0 (found by review, 0.6.11)."""
    _, relative = imports_of(path)
    allowed = ALLOWED_IMPORTS[path.stem]
    assert allowed <= relative, (
        "%s no longer imports %s; take it out of ALLOWED_IMPORTS"
        % (path.name, sorted(allowed - relative)))


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
    """(first, last) line of the `no mutate` region in devices.py."""
    source = (PACKAGE / "devices.py").read_text().splitlines()
    starts = [i for i, line in enumerate(source, 1)
              if line.strip() == "# pragma: no mutate start"]
    ends = [i for i, line in enumerate(source, 1)
            if line.strip() == "# pragma: no mutate end"]
    assert len(starts) == 1, "expected one no-mutate start, found %d" % len(starts)
    assert len(ends) == 1, "expected one no-mutate end, found %d" % len(ends)
    assert starts[0] < ends[0]
    return starts[0], ends[0]


def _defined_at(name, module="devices"):
    """The line a top-level name is bound on in that module."""
    for node in ast.walk(parse(PACKAGE / ("%s.py" % module))):
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
    raise AssertionError("%s.py defines no %s" % (module, name))


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
    answer there is behaviour, and nothing else is measuring it. One of
    them lives beside the table, below the region; the rest in
    `registers`, which has no such region at all.
    """
    if name == "device_for_name":
        _, last = _exempt_lines()
        assert _defined_at(name) > last
    else:
        assert _defined_at(name, "registers") > 0
        assert "pragma: no mutate" not in (PACKAGE / "registers.py").read_text()


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


def test_a_patch_that_nothing_reads_fails_the_test(monkeypatch):
    """The guard in conftest, held to doing what it says.

    `oscmix_desk.locking` reads the wait it imports, so that patch goes
    through; the package root reads nothing, so a patch on it would steer
    no code at all.
    """
    import oscmix_desk
    from oscmix_desk import locking

    monkeypatch.setattr(locking, "SWITCH_LOCK_WAIT", 0.1)
    with pytest.raises(pytest.fail.Exception,
                       match=r"patching oscmix_desk\.load_config changes "
                             "nothing"):
        monkeypatch.setattr(oscmix_desk, "load_config", None)
    assert oscmix_desk.load_config is not None, "and nothing was replaced"


def test_the_supported_surface_is_this_and_grows_by_decision(session_mod):
    """78 names until 0.7.0, most of them internals, each one something a
    caller could come to depend on (third outside review). What is left is
    what somebody scripting their desk needs: read a config, apply and
    verify it, switch profiles, the errors and outcomes those produce, the
    two entry points. A name more is a decision, made here."""
    assert set(session_mod.__all__) == {
        # a desk, read
        "load_config", "Config", "Route", "ChannelSetting", "GlobalSetting",
        "CommandLine", "Machine", "discover_config_path", "list_profiles",
        "profile_path",
        # applied and verified
        "apply_routing", "verify_routing", "verify_and_repair", "VerifyResult",
        "expected_registers", "generate_pipewire_conf",
        # profiles
        "switch_profile", "restore_main", "load_profile", "effective_config",
        "active_profile", "describe_profiles", "Outcome", "APPLIED_VERIFIED",
        "APPLIED_UNVERIFIED", "REFUSED", "WRITTEN_IN_PART",
        # what goes wrong
        "ConfigError", "DeviceAmbiguous", "DeviceLockUnavailable",
        "ReceivePortError", "WriteFailed",
        # entry points and their contract
        "run_session", "launch_mixer", "EXIT_OK", "EXIT_FAILURE",
        "EXIT_CONFIG", "__version__",
    }
    # Gone from the root, reachable through their modules as they always
    # were (`oscmix_desk.log` and the rest still exist: they are modules).
    for internal in ("encode_osc", "link_messages", "select_seq_client",
                     "find_stale_backends", "sd_notify", "policy_for"):
        assert not hasattr(session_mod, internal), internal
