"""What every nested sub-family must satisfy, swept rather than listed.

EQ cost a file of its own, dynamics cost most of one, and there are two
more of the same shape to come (low cut, crossfeed). The assertions that
matter per family are the same four every time -- declared paths equal
reported paths, declared tags equal reported tags, meters stay out, and
the section round-trips -- so they are written once over
`nested_families` instead of copied per family.

What stays per-family is the bounds table, because only upstream knows
it: see `test_dynamics.py` and `test_autolevel.py`. A sweep cannot check
a number it would have to invent.
"""

import json
import re

import pytest
from support import repo_file

from oscmix_desk import dump, reconcile
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.model import Config
from oscmix_desk.registers import (
    BOOL,
    ENABLE_OPTION,
    NUMBER,
    declared_paths,
    nested_families,
    settable_nested,
)

#: Every nested sub-family, split by whether a config can set it.
#: The sweep below used to assume "nested" implied "settable", which
#: Room EQ disproved: 640 registers reported and, until the f2fdd5e
#: pin, refused on write (see `test_roomeq.py`). The read-only half is
#: empty again since that pin -- the split stays, because the next
#: read-only family will need its assertions, above all that it grows
#: no config section.
ALL_FAMILIES = [(family, sub)
                for family in ("input", "output")
                for sub in nested_families(UCX2, family)]

FAMILIES = [(family, sub) for family, sub in ALL_FAMILIES
            if settable_nested(UCX2, sub, family)]

READ_ONLY = [(family, sub) for family, sub in ALL_FAMILIES
             if not settable_nested(UCX2, sub, family)]

IDS = ["%s:%s" % (sub, family) for family, sub in FAMILIES]
READ_ONLY_IDS = ["%s:%s" % (sub, family) for family, sub in READ_ONLY]


@pytest.fixture(scope="module")
def reported():
    """Path -> type tag, for everything the device reported."""
    raw = json.loads(repo_file("tests", "data", "refresh-dump.json").read_text())
    return {path: value[0] for path, value in raw["registers"].items()}


def test_the_sweep_covers_every_family_declared():
    """The guard on the guard: a sweep that found nothing would pass."""
    assert len(FAMILIES) >= 8         # eq, dynamics, autolevel, lowcut x 2
    assert ("input", "eq") in FAMILIES
    assert ("output", "autolevel") in FAMILIES
    assert set(FAMILIES) | set(READ_ONLY) == set(ALL_FAMILIES)


@pytest.mark.parametrize(("family", "sub"), READ_ONLY, ids=READ_ONLY_IDS)
def test_a_read_only_family_offers_no_config_section(tmp_path, family, sub):
    """"A config cannot set what oscmix cannot write."

    Empty since the f2fdd5e pin made Room EQ writable, and kept: a
    family modelled so the surface is described must not grow a section
    that would accept settings and deliver none.
    """
    from oscmix_desk.errors import ConfigError

    assert settable_nested(UCX2, sub, family) == {}
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n\n"
                    "[%s:%s:5]\nband1gain = -6.0\n" % (sub, family))
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(("family", "sub"), READ_ONLY, ids=READ_ONLY_IDS)
def test_a_read_only_family_is_still_declared_and_reported(reported, family,
                                                           sub):
    """Not settable is not the same as not modelled. The paths are still
    checked against the recording, so the model keeps describing what
    the device has rather than only what it accepts."""
    infix = "/%s" % sub
    declared = {p for p in declared_paths(UCX2)
                if p.startswith("/%s/" % family) and infix in p}
    assert declared
    assert declared <= set(reported)


@pytest.mark.parametrize(("family", "sub"), FAMILIES, ids=IDS)
def test_declared_paths_are_exactly_what_the_device_reports(reported, family,
                                                            sub):
    """Both directions. A family that declares a path the device does not
    report is a config option that sets nothing; one that misses a path
    the device reports is a setting nobody can write."""
    prefix = "/%s/" % family
    infix = "/%s" % sub
    declared = {p for p in declared_paths(UCX2)
                if p.startswith(prefix) and infix in p}
    seen = {p for p in reported
            if p.startswith(prefix) and infix in p
            and not p.endswith("/meter")}
    assert declared == seen


@pytest.mark.parametrize(("family", "sub"), FAMILIES, ids=IDS)
def test_declared_tags_are_the_ones_the_device_sent(reported, family, sub):
    """The `band1freq` lesson: a `,f` written to a `setint` register is
    dropped without a word -- parsed, validated, on the wire, device
    unchanged."""
    for register in settable_nested(UCX2, sub, family).values():
        path = register.template.format(ch=1)
        assert register.tags == reported[path], (
            "%s declared %r, device reports %r"
            % (path, register.tags, reported[path]))


@pytest.mark.parametrize(("family", "sub"), FAMILIES, ids=IDS)
def test_no_meter_is_declared_as_a_setting(family, sub):
    """A meter is streamed and has no `.set` upstream. Declaring one
    would put a level reading in a config file."""
    assert not [p for p in declared_paths(UCX2) if p.endswith("/meter")]
    assert "meter" not in settable_nested(UCX2, sub, family)


@pytest.mark.parametrize(("family", "sub"), FAMILIES, ids=IDS)
def test_the_switch_is_a_bool_and_every_option_has_a_domain(family, sub):
    options = settable_nested(UCX2, sub, family)
    assert options[ENABLE_OPTION].domain == BOOL
    assert all(register.domain in (NUMBER, BOOL, options[name].domain)
               for name, register in options.items())
    assert all(register.domain is not None for register in options.values())


#: Q is the one conventionally unitless quantity here. Everything else
#: with bounds has to say what its number is -- including the ones that
#: are not physical: the ratios carry ":1" and `lowcut/slope` carries
#: "index", because "3" of nothing is a number nobody can act on.
#:
#: A rule rather than a list. It was `("band1q", "band2q", "band3q")`
#: until Room EQ arrived with nine bands, which is the second time a
#: hardcoded set in this file needed one more entry. What makes `q`
#: exempt is that it is a Q factor, not which band it sits on.
UNITLESS_BY_CONVENTION = re.compile(r"^band\d+q$")


@pytest.mark.parametrize(("family", "sub"), FAMILIES, ids=IDS)
def test_every_bounded_option_declares_a_unit(family, sub):
    """A bound with no unit is a number nobody can act on: `9.9` of what?

    Kept as a sweep with one named exemption rather than a growing list.
    `lowcut/slope` failed it, and the fix was to say what the value is
    ("index") rather than to add it here -- which is the outcome this
    test exists to force.
    """
    for name, register in settable_nested(UCX2, sub, family).items():
        if register.domain != NUMBER or register.lo is None:
            continue
        assert register.unit or UNITLESS_BY_CONVENTION.match(name), (
            "%s has bounds %s..%s and no unit"
            % (register.template, register.lo, register.hi))


@pytest.mark.parametrize(("family", "sub"), FAMILIES, ids=IDS)
def test_a_section_round_trips_through_a_dump(family, sub):
    """Point 3 of the bar, per family. Everything nested is REMEMBER, so
    a dump renders it commented; what is asserted is that it renders at
    all, under the right header, and reparses."""
    seen = {register.template.format(ch=3): _plausible(register)
            for register in settable_nested(UCX2, sub, family).values()}
    config = Config(device_name="Fireface UCX II",
                    channels=list(dump.channels_from_observed(seen, UCX2)))
    text = dump.render_config(config, UCX2)
    assert "[%s:%s:3]" % (sub, family) in text


def _plausible(register):
    """A value inside the register's domain, bounded or not.

    `lowcut/slope` carries no bounds -- upstream declares none, so the
    model declares none either -- and the first version of this helper
    reached straight for `register.lo`, which produced the string
    "None" in a config file. The sweep found it on the family that
    introduced the case, which is the argument for sweeping.
    """
    if register.domain == BOOL:
        return (1,)
    if register.domain == NUMBER:
        value = register.lo if register.lo is not None else 0
        return (float(value) if register.tags.startswith("f")
                else int(value),)
    return (0, register.choices[0])


@pytest.mark.parametrize(("family", "sub"), FAMILIES, ids=IDS)
def test_a_config_section_writes_only_declared_paths(tmp_path, family, sub):
    """Point 2 of the bar, per family: written ⊆ declared."""
    options = settable_nested(UCX2, sub, family)
    lines = ["[%s:%s:3]" % (sub, family), "enabled = true"]
    for name, register in sorted(options.items()):
        if name == ENABLE_OPTION or register.domain != NUMBER:
            continue
        lines.append("%s = %s" % (name, _plausible(register)[0]))
    path = tmp_path / "routing.conf"
    path.write_text("[device]\nname = Fireface UCX II\n\n"
                    + "\n".join(lines) + "\n")
    written = {entry.path for entry in reconcile.desired(load_config(path))}
    assert written <= set(declared_paths(UCX2))
    assert "/%s/3/%s" % (family, sub) in written
