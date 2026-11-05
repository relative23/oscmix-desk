"""`[output:N] loopback = on|off`: written when declared, never reported, never confirmed.

The device takes `/output/N/loopback` (its capture channel N then carries output N) and reports
nothing back, not in a refresh dump either. So the row is WRITE_ONLY with a BOOL domain and
REMEMBER: a start or switch writes the declared value, a reload leaves it alone, and the
read-back calls it unverifiable. Only the upstream pin 89ed835 sends the output's own index;
the earlier pin sent 0xffffffff for every output (tests/backend_writes.py measures the words).
"""

import pytest
from backend_doubles import RecordingBackend
from support import repo_file, routing_conf

from oscmix_desk import reconcile, routing, verify
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.observation import Observation
from oscmix_desk.reconcile import policy_for
from oscmix_desk.registers import BOOL, REMEMBER, WRITE_ONLY, register_at
from oscmix_desk.verify import register_ever_reported


@pytest.mark.parametrize("channel", [1, 2, 20])
def test_the_row_is_a_remembered_write_only_flag(channel):
    path = "/output/%d/loopback" % channel
    row = register_at(UCX2, path)
    assert (row.tags, row.verify, row.domain, row.policy) == ("i", WRITE_ONLY, BOOL, REMEMBER)
    assert policy_for(path, UCX2, {}) is REMEMBER
    assert register_ever_reported(path, UCX2) is False


@pytest.mark.parametrize(("raw", "flag"), [("on", 1), ("true", 1), ("off", 0), ("no", 0)])
def test_the_declared_value_is_written_as_the_backend_flag(tmp_path, raw, flag):
    config = load_config(routing_conf(tmp_path, "[output:3]\nloopback = %s\n" % raw))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    assert backend.sent == [("/output/3/loopback", "i", (flag,))]


@pytest.mark.parametrize("raw", ["2", "-1", "maybe", "1.0"])
def test_anything_but_a_boolean_is_refused_before_io(tmp_path, raw):
    with pytest.raises(ConfigError, match="loopback"):
        load_config(routing_conf(tmp_path, "[output:3]\nloopback = %s\n" % raw))


def test_inputs_and_playback_have_no_loopback(tmp_path):
    for section in ("input:3", "playback:3"):
        with pytest.raises(ConfigError, match="loopback"):
            load_config(routing_conf(tmp_path, "[%s]\nloopback = on\n" % section))


@pytest.mark.parametrize("flag", [0, 1])
def test_an_echo_of_the_written_value_confirms_nothing(flag):
    path = "/output/3/loopback"
    observation = Observation({path: ("i", (flag,))}, UCX2)
    observation.absorb((path, "i", (flag,)))
    assert observation.confirmed == observation.mismatched == set()
    assert observation.unobserved == {path}
    for seen in ({}, {path: (flag,)}, {path: (1 - flag,)}):
        plan = reconcile.plan(
            (reconcile.Entry(path, "i", (flag,), reconcile.Phase.CHANNEL),), seen, UCX2)
        assert plan.confirmed == ()
        assert plan.unverifiable == (path,)
        assert [write.args for write in plan.writes] == [(flag,)]


def test_the_read_back_names_it_unverifiable(tmp_path):
    config = load_config(routing_conf(tmp_path, "[output:1]\nloopback = on\n"))
    expected = verify.expected_registers(config)
    assert expected == {"/output/1/loopback": ("i", (1,))}

    class Backend:
        def request_dump(self):
            pass

        def messages(self, _timeout):
            return iter([("/output/1/loopback", "i", (1,))])

    result = verify.verify_routing(expected, Backend(), timeout=0.002, device_model=UCX2)
    assert result.confirmed == result.mismatched == []
    assert result.unobserved == ["/output/1/loopback"]
    assert verify._unconfirmed(result, UCX2) == []  # absence is expected, not a failure


def test_the_shipped_pin_is_the_one_whose_loopback_word_was_measured():
    install = repo_file("install.sh").read_text()
    assert 'OSCMIX_REF="${OSCMIX_REF:-89ed835ecf8118a21d1d451be7d71944fa5ec384}"' in install


def synthetic_fixture(manifest_bytes):
    import hashlib
    import json

    manifest = json.loads(manifest_bytes)
    shape = dict(missing=[], changed=[], new=[])
    cases = []
    for output in range(1, 21):
        cases.append(dict(output=output, source=2 if output == 1 else 1, passed=True,
                          before=dict(verdict="not-carried"), on=dict(verdict="carried"),
                          off=dict(verdict="not-carried"), given_back=0))
    return dict(schema=1, kind="output-loopback-probes-ucx2", hardware_used=True, passed=True,
                restored=True, cases=cases,
                running_backend=dict(series_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                                     upstream=manifest["upstream"], matches_local_build=True),
                first_dump=dict(paths=2232), final_dump=dict(paths=2232, **shape),
                delay=dict(carried=True, points=[{}, {}, {}, {}]))


def test_the_evidence_validator_accepts_a_complete_probe_and_names_every_flag():
    from loopback_evidence import validate

    manifest = repo_file("patches", "backend-series.json").read_bytes()
    paths = validate(synthetic_fixture(manifest), manifest)
    assert paths == {"/output/%d/loopback" % n for n in range(1, 21)}


@pytest.mark.parametrize("damage", [
    lambda f: f.update(passed=False),
    lambda f: f.update(restored=False),
    lambda f: f["final_dump"]["changed"].append("/output/3/volume"),
    lambda f: f["cases"].pop(),
    lambda f: f["cases"][4]["on"].update(verdict="undetermined"),
    lambda f: f["cases"][4]["off"].update(verdict="carried"),
    lambda f: f["cases"][4].update(given_back=1),
    lambda f: f["cases"][0].update(source=1),
    lambda f: f["running_backend"].update(series_sha256="0" * 64),
    lambda f: f["running_backend"].update(upstream="f2fdd5ec78338848754aad32cc07f3440de63395"),
    lambda f: f["delay"].update(carried=False),
    lambda f: f.update(error="OSError: lost"),
])
def test_the_evidence_validator_refuses_a_probe_that_shows_less(damage):
    from loopback_evidence import validate

    manifest = repo_file("patches", "backend-series.json").read_bytes()
    fixture = synthetic_fixture(manifest)
    damage(fixture)
    with pytest.raises(ValueError, match=r"\w"):  # each refusal names what is missing
        validate(fixture, manifest)
