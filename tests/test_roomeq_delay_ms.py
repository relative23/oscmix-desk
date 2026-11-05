"""Room EQ delay in milliseconds, the unit measured through output loopback (2026-11-05).

`[roomeq:output:N] delay = 2.5 ms` writes the backend value 0.25. A bare number keeps its old
meaning, the backend's OSC units, so no existing config changes what it sets. The factor is the
measurement's, held here against the committed fixture rather than restated from memory.
"""

import json

import pytest
from backend_doubles import RecordingBackend
from support import repo_file, routing_conf

from oscmix_desk import dump, numeric, reconcile, routing
from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError
from oscmix_desk.model import Config
from oscmix_desk.registers import register_at

PATH = "/output/3/roomeq/delay"


def written(tmp_path, raw):
    config = load_config(routing_conf(tmp_path, "[roomeq:output:3]\ndelay = %s\n" % raw))
    backend = RecordingBackend()
    routing.apply_routing(config, backend)
    return [args for path, _tags, args in backend.sent if path == PATH]


@pytest.mark.parametrize(("raw", "value"), [
    ("2.5 ms", 0.25), ("2.5ms", 0.25), ("0 ms", 0.0), ("0.01 ms", 0.001), ("4.25 ms", 0.425),
    # Without a unit, the backend's OSC units, as before.
    ("0.25", 0.25), ("0.425", 0.425),
])
def test_milliseconds_and_bare_osc_units_both_load(tmp_path, raw, value):
    assert written(tmp_path, raw) == [(pytest.approx(value),)]


@pytest.mark.parametrize(("raw", "message"), [
    ("4.26 ms", "outside 0..4.25 ms"), ("-1 ms", "outside 0..4.25 ms"),
    ("fast ms", "not a number in OSC units or ms"), ("2.5 s", "not a number"),
])
def test_a_delay_the_backend_cannot_take_is_refused(tmp_path, raw, message):
    with pytest.raises(ConfigError, match=message):
        written(tmp_path, raw)


def test_only_the_measured_register_has_a_physical_unit():
    assert numeric.physical(register_at(UCX2, PATH)) == ("ms", 10.0)
    assert numeric.physical(register_at(UCX2, "/echo/delay")) is None
    assert numeric.physical(None) is None


def test_the_factor_is_the_one_measured_through_loopback():
    probe = repo_file("tests", "data", "output-loopback-probes-ucx2.json")
    fixture = json.loads(probe.read_text())
    delay = fixture["delay"]
    unit, factor = numeric.PHYSICAL["/output/{ch}/roomeq/delay"]
    assert unit == "ms"
    assert delay["ms_per_osc_unit"] == pytest.approx(factor, rel=0.002)
    assert delay["residual_ms"] < 0.05
    assert delay["carried"] is True
    assert max(point["osc"] for point in delay["points"]) == register_at(UCX2, PATH).hi


@pytest.mark.parametrize("reported", [0.0, 0.25, 0.123, 0.425])
def test_an_export_in_milliseconds_loads_back_to_the_same_register(tmp_path, reported):
    seen = {PATH: (reported,)}
    config = Config(channels=dump.channels_from_observed(seen, UCX2),
                    globals=dump.globals_from_observed(seen, UCX2))
    text = dump.render_config(config, UCX2)
    assert " ms" in text
    adopted = "\n".join(line[2:] if line.startswith("# ") and " = " in line else line
                        for line in text.splitlines())
    path = tmp_path / "export.conf"
    path.write_text(adopted)
    entry, = reconcile.desired(load_config(path))
    assert entry.path == PATH
    assert reconcile.matches("f", entry.args, (reported,), register=register_at(UCX2, PATH))
