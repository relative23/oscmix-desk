"""`[desk]` and `mirror =` refusals, by the complete text the person who wrote the file reads.

test_watch.py, test_peak_safe.py and test_mirror.py pin which declarations are refused. These
pin what each refusal says: the section and option, the value given, the outputs involved and
what to do instead.
"""

import pytest

from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.mirrors import expand_mirrors, parse_mirror
from oscmix_desk.model import Route
from oscmix_desk.profiles import load_profile

HEAD = "[device]\nname = Fireface UCX II\n\n"


def refusal(call, *args):
    with pytest.raises(ConfigError) as refused:
        call(*args)
    return str(refused.value)


def conf(tmp_path, body):
    path = tmp_path / "routing.conf"
    path.write_text(HEAD + body)
    return path


@pytest.mark.parametrize("option", ["enforce = apply", "peak-safe = true"])
def test_a_profile_that_sets_how_the_session_keeps_the_desk_is_told_where_it_belongs(
        tmp_path, option):
    main = conf(tmp_path, "")
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "loud.conf").write_text("[desk]\n%s\n" % option)
    assert refusal(load_profile, "loud", main) == (
        "[desk] %s: how the running session keeps the desk is set in routing.conf, and every "
        "profile inherits it; take it out of the profile" % option.split(" = ")[0])


def test_an_unknown_enforce_names_the_choices(tmp_path):
    assert refusal(load_config, conf(tmp_path, "[desk]\nenforce = always\n")) == (
        "[desk] enforce: expected one of apply, watch, got 'always'")


def test_a_peak_safe_that_is_no_boolean_names_the_section_and_option(tmp_path):
    assert refusal(load_config, conf(tmp_path, "[desk]\npeak-safe = maybe\n")) == (
        "[desk] peak-safe: 'maybe' is not a boolean")


@pytest.mark.parametrize(("raw", "channel", "message"), [
    ("left", 3, ("[output:3] mirror: expected an output or an output pair such as 1/2, got "
                 "'left'")),
    ("2/3", 5, "[output:5] mirror: '2/3' is not an output pair"),
    ("1/2/3", 5, "[output:5] mirror: one output or one pair, not '1/2/3'"),
    ("21/22", 3, "[output:3] mirror: Fireface UCX II has no output 21/22"),
    ("3", 3, "[output:3] mirror: an output cannot mirror itself"),
])
def test_a_wrong_mirror_is_named(raw, channel, message):
    assert refusal(parse_mirror, raw, "output:%d" % channel, channel, UCX2) == message


def test_a_mirror_on_an_unmodelled_device_names_the_target():
    assert refusal(parse_mirror, "1/2", "output:3", 3, None) == (
        "[output:3] mirror: this device has no output 3/4")


def test_two_mirrors_onto_one_output_are_named():
    mirrors = [((3, 4), (1, 2)), ((3,), (5,))]
    assert refusal(expand_mirrors, [], mirrors) == "output 3 is the target of two mirrors"


def test_a_mirror_of_a_mirror_names_both_pairs():
    mirrors = [((3, 4), (1, 2)), ((5, 6), (3, 4))]
    assert refusal(expand_mirrors, [], mirrors) == (
        "output 5/6 mirrors 3/4, which is itself a mirror; name the original")


def test_a_pair_route_over_a_mono_mirror_source_is_named():
    route = Route("main", playback=(1, 2), output=(1, 2))
    assert refusal(expand_mirrors, [route], [((5,), (1,))]) == (
        "route 'main' writes 1/2, which only partly covers the mirrored output 1; mirror the "
        "same width the routes use")


def test_a_mono_route_over_a_pair_mirror_source_is_named():
    route = Route("left", playback=(1,), output=(1,))
    assert refusal(expand_mirrors, [route], [((3, 4), (1, 2))]) == (
        "route 'left' writes 1, which only partly covers the mirrored outputs 1/2; mirror the "
        "same width the routes use")


def test_a_route_onto_a_mirror_target_is_named_with_its_source():
    route = Route("extra", playback=(3,), output=(4,))
    assert refusal(expand_mirrors, [route], [((3, 4), (1, 2))]) == (
        "route 'extra' writes output 4, which mirrors 1/2; a mirror's submix comes only from "
        "its source")
