"""`[desk] peak-safe`: an explicit switch quiets the outputs it changes, then gives them back.

The interim states of a switch -- a route at its new level before the old one is silenced, a
pair heard unlinked for a moment -- can be louder than either end. The outputs a switch changes
go to the backend's explicit zero first and get their volume back last: the declared one, else
the one the device reported. Starts, repairs and reloads are not quieted.
"""

import errno

import pytest
from backend_doubles import RecordingBackend

from oscmix_desk.config import load_config
from oscmix_desk.devices import UCX2
from oscmix_desk.errors import ConfigError, WriteFailed
from oscmix_desk.peaksafe import changed_outputs, reported_volumes, send_quietly
from oscmix_desk.profiles import load_profile
from oscmix_desk.reconcile import ApplyIntent, application_plan

DESK = """[device]
name = Fireface UCX II

[desk]
peak-safe = true

[route:main]
playback = 1/2
output = 1/2
level = -6.0
volume = -10.0

[route:phones]
playback = 7/8
output = 7/8
"""
SEEN = {"/output/1/volume": ("f", (-20.0,)), "/output/2/volume": ("f", (-20.0,)),
        "/output/7/volume": ("f", (-30.0,)), "/output/8/volume": ("f", (-30.0,))}


@pytest.fixture
def desk(tmp_path):
    path = tmp_path / "routing.conf"
    path.write_text(DESK)
    return load_config(path)


def switch(desk, intent=ApplyIntent.EXPLICIT, seen=SEEN, fail=False):
    backend = RecordingBackend()
    reads = []

    def read(paths):
        reads.append(tuple(paths))
        return {}

    def send(plan):
        backend.send(write.message() for write in plan.writes)
        if fail:
            raise WriteFailed(OSError(errno.EIO, "lost"), [], [])

    plan = application_plan(desk, intent)
    send_quietly(desk, plan, backend, intent, seen, read, send)
    return backend.sent, reads


def test_a_switch_quiets_what_it_changes_and_gives_the_volume_back(desk):
    sent, reads = switch(desk)
    assert reads == [], "the observation already had the volumes"
    paths = [path for path, _tags, _args in sent]
    assert sent[:4] == [("/output/%d/volume" % ch, "f", (-65.0,)) for ch in (1, 2, 7, 8)]
    assert sent[-4:] == [("/output/1/volume", "f", (-10.0,)), ("/output/2/volume", "f", (-10.0,)),
                         ("/output/7/volume", "f", (-30.0,)), ("/output/8/volume", "f", (-30.0,))]
    assert paths.count("/output/1/volume") == 2, "the declared volume goes out once, last"
    assert "/mix/1/playback/1" in paths[4:-4]


@pytest.mark.parametrize("intent", [ApplyIntent.INITIAL, ApplyIntent.REPAIR,
                                    ApplyIntent.RECONCILE])
def test_only_an_explicit_apply_is_quieted(desk, intent):
    sent, _reads = switch(desk, intent)
    assert ("/output/1/volume", "f", (-65.0,)) not in sent


def test_an_output_without_a_reported_volume_is_left_alone(desk):
    sent, reads = switch(desk, seen={"/output/1/volume": ("f", (-20.0,))})
    assert reads == [("/output/2/volume", "/output/7/volume", "/output/8/volume")]
    assert ("/output/7/volume", "f", (-65.0,)) not in sent
    assert sent[0] == ("/output/1/volume", "f", (-65.0,))


def test_a_failed_switch_still_gives_the_volume_back(desk):
    with pytest.raises(WriteFailed):
        switch(desk, fail=True)


def test_volumes_come_back_after_a_failure_too(desk):
    backend = RecordingBackend()

    def send(_plan):
        raise WriteFailed(OSError(errno.EIO, "lost"), [], [])

    with pytest.raises(WriteFailed):
        send_quietly(desk, application_plan(desk, ApplyIntent.EXPLICIT), backend,
                     ApplyIntent.EXPLICIT, SEEN, lambda _paths: {}, send)
    assert backend.sent[-1] == ("/output/8/volume", "f", (-30.0,))


def test_which_outputs_a_plan_changes():
    assert changed_outputs(["/mix/3/input/1", "/output/9/eq", "/output/stereo"], UCX2) == (
        3, 4, 9, 10), "a pair answers on its odd channel"
    assert len(changed_outputs(["/input/1/gain"], UCX2)) == 20, "monitoring reaches anywhere"
    assert len(changed_outputs(["/playback/3/mute"], UCX2)) == 20


def test_a_silent_or_malformed_volume():
    seen = {"/output/1/volume": ("f", (float("-inf"),)), "/output/2/volume": ("f", ("x",))}
    assert reported_volumes((1, 2, 3), seen) == {1: -65.0}


def test_peak_safe_is_routing_confs_and_profiles_inherit_it(tmp_path):
    main = tmp_path / "routing.conf"
    main.write_text(DESK)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles/quiet.conf").write_text("[route:m]\nplayback = 1/2\noutput = 1/2\n")
    assert load_profile("quiet", main).peak_safe is True
    (tmp_path / "profiles/own.conf").write_text("[desk]\npeak-safe = false\n")
    with pytest.raises(ConfigError, match="peak-safe: how the running session"):
        load_profile("own", main)
    main.write_text(DESK.replace("peak-safe = true", "peak-safe = maybe"))
    with pytest.raises(ConfigError, match="is not a boolean"):
        load_config(main)
