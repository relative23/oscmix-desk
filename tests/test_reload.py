"""A desk read again by a running session: kept for the machine it runs
on, or refused as a desk for somewhere else (ADR 0024, ADR 0026).
"""

import argparse

import pytest
from conftest import write_config
from two_boxes import DESK, lock_dir

from oscmix_desk import cli, profiles
from oscmix_desk import notices as notices_mod
from oscmix_desk import reload as reload_mod
from oscmix_desk import session as session_module


def test_a_reconcile_that_cannot_reach_the_backend_stands_down(
        tmp_path, monkeypatch, caplog):
    import argparse

    from oscmix_desk import Config

    def unreachable(*_a, **_k):
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(reload_mod, "reconcile_now", unreachable)
    statuses = []
    monkeypatch.setattr(reload_mod, "sd_notify", statuses.append)
    lock_dir(tmp_path, monkeypatch)
    path = write_config(tmp_path / "routing.conf", DESK)
    with caplog.at_level("ERROR"):
        reload_mod._reconcile(argparse.Namespace(config=path), Config(),
                                  {"stop": False})
    assert ("SIGHUP: cannot reach the backend on UDP 7222 ([Errno 101] "
            "Network is unreachable); reconcile skipped") in caplog.text
    assert statuses[-1].startswith("STATUS=running; reconcile skipped")

@pytest.mark.parametrize("reread", ["_desk_under_the_lock", "_reloaded_desk"])
def test_a_re_read_desk_keeps_every_machine_setting_of_the_process(
        tmp_path, reread):
    """The start's re-read under the lock and the SIGHUP's each spelled the
    five assignments out by hand; `profiles.MACHINE_SETTINGS` is the list, and
    a setting added to it is kept by both without anybody remembering."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 3/4\n")
    running = profiles.load_config(path)
    # What a start replaces, replaced -- the command line's two and the
    # serial it pins. The file says none of it.
    cli._override_device(running, "Fireface UCX II (live)")
    running.osc_port, running.serial = 9000, "24216011"
    running.overrides = running.overrides._replace(osc_port=9000)
    args = (running, path) if reread == "_reloaded_desk" else (path, running)
    fresh = getattr(reload_mod, reread)(*args)
    assert fresh is not running
    assert [route.output for route in fresh.routes] == [(3, 4)], \
        "the desk itself is the file's"
    for _section, _option, attr in profiles.MACHINE_SETTINGS:
        assert getattr(fresh, attr) == getattr(running, attr), attr
    assert fresh.overrides == running.overrides, \
        "where two of the kept settings came from goes with them"

def test_keeping_the_machine_settings_walks_the_whole_table():
    """The two re-reads can only differ from their file in what a start
    replaces; the table is what `keep_machine_settings` walks, all of it."""
    from oscmix_desk import Config

    running = Config(device_name="A", usb_id="1:2", serial="3", osc_port=4,
                     osc_recv_port=5)
    kept = profiles.keep_machine_settings(Config(), running)
    for _section, _option, attr in profiles.MACHINE_SETTINGS:
        assert getattr(kept, attr) == getattr(running, attr), attr

def _reread(name, running, path):
    """`_desk_under_the_lock` answers with the running desk when it refuses,
    `_reloaded_desk` with None; both mean "not applied"."""
    args = (running, path) if name == "_reloaded_desk" else (path, running)
    fresh = getattr(reload_mod, name)(*args)
    return None if fresh is running else fresh

@pytest.mark.parametrize("reread", ["_desk_under_the_lock", "_reloaded_desk"])
@pytest.mark.parametrize(("elsewhere", "named"), [
    ("[device]\nname = Some Box\n", "device name 'Some Box'"),
    ("[device]\nserial = 99887766\n", "serial '99887766' (not '')"),
    ("[osc]\nport = 9500\n", "osc port 9500 (not 7222)"),
], ids=["another model", "another box of the model", "another backend"])
def test_a_re_read_desk_for_somewhere_else_is_not_applied_here(
        tmp_path, caplog, reread, elsewhere, named):
    """Until 0.6.11 it was pinned to this session's interface and written.
    Measured and reviewed: `routing.conf` edited to name a box with 42
    outputs reached a UCX II, which has twenty; a profile stating another
    port and serial was written to its own interface by the switch and
    then to this one by the reload the switch sent -- the same persisted
    profile meant one target for a switch, another for a reload, and the
    first again after a restart. A notice that compared models was silent
    for two boxes of one model."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    running = profiles.load_config(path)
    assert _reread(reread, running, path) is not None, "its own desk"

    # As the file itself ...
    path.write_text(elsewhere + "[route:far]\nplayback = 1/2\noutput = 3/4\n")
    with caplog.at_level("INFO"):
        assert _reread(reread, running, path) is None
    assert "the desk now in effect is for another backend or interface" \
        in caplog.text
    assert named in caplog.text
    assert "systemctl --user restart oscmix.service" in caplog.text
    assert "SIGHUP: reloaded" not in caplog.text, "it was not"
    assert "--no-profile" not in caplog.text

    # ... and as an active profile over an untouched routing.conf.
    path.write_text("[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "far.conf", elsewhere
                 + "[route:far]\nplayback = 1/2\noutput = 3/4\n")
    (tmp_path / "active-profile").write_text("far\n")
    caplog.clear()
    with caplog.at_level("ERROR"):
        assert _reread(reread, running, path) is None
    assert "take [osc] and [device] out of profile 'far'" in caplog.text, \
        "a start is told which profile as much as a reload is"

def test_the_box_a_start_pinned_is_not_another_box_when_the_file_names_it(
        tmp_path, caplog):
    """A start pins the serial of the interface it found. Adding that very
    serial to routing.conf -- what TROUBLESHOOTING tells a user with two
    boxes to do -- was read as a desk for another box and refused until a
    restart (found by review). Naming no serial means "the only one", which
    is also this one; naming another is elsewhere."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    running = profiles.load_config(path)
    running.serial = "24216011"                     # pinned by the start
    route = "[route:main]\nplayback = 1/2\noutput = 1/2\n"
    path.write_text("[device]\nserial = 24216011\n" + route)
    assert _reread("_reloaded_desk", running, path) is not None
    started_named = profiles.load_config(path)      # a start that named it
    started_named.serial = "24216011"
    path.write_text(route)
    assert _reread("_reloaded_desk", started_named, path) is not None
    path.write_text("[device]\nserial = 99887766\n" + route)
    with caplog.at_level("ERROR"):
        assert _reread("_reloaded_desk", running, path) is None
    assert "serial '99887766' (not '24216011')" in caplog.text, \
        "against the box it is on, not against what the file once said"

def test_a_file_that_appears_is_resolved_like_the_one_a_start_reads(
        tmp_path, caplog):
    """A session started without a routing.conf, with `--osc-port 9000`, was
    refused the file that appeared later and named nothing -- the same
    file present at its start is applied. One for a box with 42 outputs on
    another port is not; it used to be, pinned to this session, without a
    word (both found by review)."""
    from oscmix_desk import CommandLine

    running = profiles.load_config(None)
    running.osc_port = 9000
    running.overrides = CommandLine(osc_port=9000)
    path = tmp_path / "routing.conf"
    path.write_text("[route:main]\nplayback = 1/2\noutput = 1/2\n")
    fresh = _reread("_reloaded_desk", running, path)
    assert fresh is not None
    assert fresh.osc_port == 9000
    path.write_text("[device]\nname = Some Box\n\n[osc]\nport = 9500\n\n"
                    "[route:far]\nplayback = 1/2\noutput = 41/42\n")
    with caplog.at_level("ERROR"):
        assert _reread("_reloaded_desk", running, path) is None
    assert "device name 'Some Box' (not 'Fireface UCX II')" in caplog.text
    assert "osc port" not in caplog.text, "--osc-port has the say in that"

def test_a_file_under_an_override_is_not_a_desk_for_elsewhere(tmp_path,
                                                              caplog):
    """`--osc-port 9000` over a file that moves from 7222 to 9500: the file's
    port was never used and is not used now. It was refused with "restart
    the session to follow it", and a restart with the same command line
    stays on 9000 (found by review)."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    running = profiles.load_config(path)
    running.osc_port = 9000
    running.overrides = running.overrides._replace(osc_port=9000)
    path.write_text("[osc]\nport = 9500\n"
                    "[route:main]\nplayback = 1/2\noutput = 3/4\n")
    with caplog.at_level("ERROR"):
        fresh = _reread("_reloaded_desk", running, path)
    assert fresh is not None
    assert fresh.osc_port == 9000
    assert [route.output for route in fresh.routes] == [(3, 4)]
    assert caplog.text == ""

def test_a_reload_names_a_desk_checked_for_another_interface(tmp_path,
                                                             caplog):
    """The start's notice covers the desk of that moment. A file with nothing
    in it to check, given routes later, reached the `--device` interface
    unannounced: outputs 29/30 of an 802 desk, on a box with twenty. A
    restart would have said so, and so does the reload (found by review)."""
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Fireface 802\n")
    running = profiles.load_config(path)
    cli._override_device(running, "Fireface UCX II")
    assert notices_mod.replaced_device_warning(running) is None, \
        "nothing in it was checked for a device"
    path.write_text("[device]\nname = Fireface 802\n"
                    "[route:x]\nplayback = 1/2\noutput = 29/30\n")
    with caplog.at_level("WARNING"):
        assert _reread("_reloaded_desk", running, path) is not None
    assert ("--device replaces [device] name after validation: this config "
            "was checked for 'Fireface 802' and is used for 'Fireface UCX II'"
            ) in caplog.text

def test_the_advice_fits_the_cause(tmp_path, caplog):
    """A routing.conf that moved is followed by a restart. A profile would be
    followed too -- off this interface, leaving it unmanaged -- so that
    case is sent to the profile instead (ADR 0026, found by review)."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    running = profiles.load_config(path)
    write_config(tmp_path / "profiles" / "far.conf", "[osc]\nport = 9500\n"
                 "[route:far]\nplayback = 1/2\noutput = 3/4\n")
    (tmp_path / "active-profile").write_text("far\n")
    with caplog.at_level("ERROR"):
        assert _reread("_reloaded_desk", running, path) is None
    assert ("take [osc] and [device] out of profile 'far', then reload the "
            "session, or --no-profile") in caplog.text
    assert "restart" not in caplog.text
    # routing.conf moved, with or without a profile active: an ordinary
    # profile states nothing, so neither of its remedies could work, and
    # `--no-profile` would be refused for a port nobody listens on.
    write_config(tmp_path / "profiles" / "plain.conf",
                 "[route:p]\nplayback = 1/2\noutput = 3/4\n")
    path.write_text("[osc]\nport = 9500\n"
                    "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    # The same for a profile that states a setting which did *not* move --
    # here the very serial the start pinned: comparing whole machines
    # blamed it, and named two remedies that could not work.
    running.serial = "24216011"
    write_config(tmp_path / "profiles" / "pinned.conf",
                 "[device]\nserial = 24216011\n"
                 "[route:p]\nplayback = 1/2\noutput = 3/4\n")
    for marker in ("plain\n", "pinned\n", None):
        caplog.clear()
        if marker:
            (tmp_path / "active-profile").write_text(marker)
        else:
            (tmp_path / "active-profile").unlink()
        with caplog.at_level("ERROR"):
            assert _reread("_reloaded_desk", running, path) is None
        assert ("restart the session to follow it (systemctl --user restart "
                "oscmix.service)") in caplog.text
        assert "profile" not in caplog.text
    # Both at once: the profile first, and `--no-profile` is not offered --
    # it writes where routing.conf says, and nothing listens there yet.
    write_config(tmp_path / "profiles" / "far.conf", "[osc]\nport = 9100\n"
                 "[route:far]\nplayback = 1/2\noutput = 3/4\n")
    (tmp_path / "active-profile").write_text("far\n")
    caplog.clear()
    with caplog.at_level("ERROR"):
        assert _reread("_reloaded_desk", running, path) is None
    assert ("osc port 9100 (not 7222) -- and a running session keeps the one "
            "it was started for, so it was not applied; take [osc] and "
            "[device] out of profile 'far', then restart the session to "
            "follow routing.conf (systemctl --user restart oscmix.service)"
            ) in caplog.text
    assert "--no-profile" not in caplog.text

def test_no_profile_is_named_only_where_it_can_work(tmp_path, caplog):
    """`--no-profile` writes where the bare routing.conf says: it never saw
    `--osc-port`, is refused together with it, and is refused for a port
    nobody listens on. It was offered to a session running under an
    override (found by review). What is left is the edit, and the reload
    that applies it -- another switch would go to the file's port too."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "p.conf", "[osc]\nrecv-port = 8333\n"
                 "[route:p]\nplayback = 1/2\noutput = 3/4\n")
    (tmp_path / "active-profile").write_text("p\n")
    under_an_override = profiles.load_config(path)
    under_an_override.osc_port = 9000
    under_an_override.overrides = under_an_override.overrides._replace(
        osc_port=9000)
    with caplog.at_level("ERROR"):
        assert _reread("_reloaded_desk", under_an_override, path) is None
    assert caplog.text.rstrip().endswith(
        "so it was not applied; take [osc] and [device] out of profile 'p', "
        "then reload the session")

    # Started under a profile that named port 9000, which now names 9001:
    # routing.conf never moved, and is still not where this session runs,
    # so the edit alone would leave a desk for 7222 -- a restart follows.
    write_config(tmp_path / "profiles" / "p.conf", "[osc]\nport = 9001\n"
                 "[route:p]\nplayback = 1/2\noutput = 3/4\n")
    started_under_it = profiles.load_config(path)
    started_under_it.osc_port = 9000
    caplog.clear()
    with caplog.at_level("ERROR"):
        assert _reread("_reloaded_desk", started_under_it, path) is None
    assert "osc port 9001 (not 9000)" in caplog.text
    assert ("out of profile 'p', then restart the session to follow "
            "routing.conf") in caplog.text
    assert "--no-profile" not in caplog.text

@pytest.mark.parametrize("reread", ["_desk_under_the_lock", "_reloaded_desk"])
def test_what_the_start_replaced_does_not_read_as_a_desk_for_elsewhere(
        tmp_path, caplog, reread):
    """`--device`, `--osc-port` and the serial a start pins change the live
    attributes, and a re-read file is resolved with the same command line
    before it is compared -- so none of them reads as a desk for elsewhere.
    A first cut compared live names with the bare file and refused a
    `--device` start its own desk at every SIGHUP."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    running = profiles.load_config(path)
    cli._override_device(running, "Some Box")
    running.osc_port, running.serial = 9000, "24216011"
    running.overrides = running.overrides._replace(osc_port=9000)
    with caplog.at_level("WARNING"):
        fresh = _reread(reread, running, path)
    assert fresh is not None
    assert (fresh.device_name, fresh.osc_port, fresh.serial) == (
        "Some Box", 9000, "24216011")
    assert "another backend" not in caplog.text
    # The reload says what its desk is used for. The start said so before
    # it looked for the interface, and under the lock repeats nothing.
    assert caplog.text.count("checked for") == (reread == "_reloaded_desk")

def test_another_desk_under_the_lock_is_spoken_about_even_in_the_same_words(
        tmp_path, caplog):
    """The notice about a profile for another machine does not name the
    profile. Compared by their text, a switch from one such profile to
    another during the start left the second unannounced; it is the desk
    that is compared (found by review)."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    for name, output in (("far", "3/4"), ("far2", "5/6")):
        write_config(tmp_path / "profiles" / ("%s.conf" % name),
                     "[osc]\nrecv-port = 8333\n"
                     "[route:x]\nplayback = 1/2\noutput = %s\n" % output)
    (tmp_path / "active-profile").write_text("far\n")
    started, _active = profiles.effective_config(path)
    with caplog.at_level("WARNING"):
        assert reload_mod._desk_under_the_lock(path, started) == started
    assert caplog.text == "", "the same desk, spoken about at the top"
    (tmp_path / "active-profile").write_text("far2\n")
    with caplog.at_level("WARNING"):
        applied = reload_mod._desk_under_the_lock(path, started)
    assert [route.output for route in applied.routes] == [(5, 6)]
    assert caplog.text.count("this profile names another backend") == 1

def test_a_start_that_finds_no_interface_has_said_why_it_looked_elsewhere(
        tmp_path, caplog, monkeypatch):
    """A profile that names another machine is what sends a start looking
    for another box, or into exit 2 beside the session holding that port:
    the starts that never reach the lock. Told only under the lock, those
    were the ones that were not told (found by review)."""
    path = write_config(tmp_path / "routing.conf",
                        "[route:main]\nplayback = 1/2\noutput = 1/2\n")
    write_config(tmp_path / "profiles" / "far.conf",
                 "[device]\nserial = 99887766\n"
                 "[route:far]\nplayback = 1/2\noutput = 3/4\n")
    (tmp_path / "active-profile").write_text("far\n")
    config, active = profiles.effective_config(path)
    assert active == "far"
    monkeypatch.setattr(session_module, "_find_client",
                        lambda *a: (None, 0))
    with caplog.at_level("WARNING"):
        assert session_module.run_session(
            argparse.Namespace(dry_run=False, config=path), config) == 0
    assert ("this profile names another backend or interface than its "
            "routing.conf -- serial '99887766' (not '')") in caplog.text

def test_a_start_gives_its_notices_about_the_desk_it_applies(
        tmp_path, caplog, monkeypatch):
    """The top of a start speaks about the file as read before the wait for
    the device and the lock. A file with nothing in it to check, given
    routes in that time, reached the `--device` interface unannounced:
    another desk under the lock is spoken about there, and the same one
    is not spoken about twice (found by review)."""
    path = write_config(tmp_path / "routing.conf",
                        "[device]\nname = Fireface 802\n")
    started = profiles.load_config(path)
    cli._override_device(started, "Fireface UCX II")
    applied = []
    monkeypatch.setattr(session_module, "apply_routing",
                        lambda config, *a, **k: applied.append(config))
    monkeypatch.setattr(session_module, "verify_and_repair",
                        lambda *a, **k: None)
    monkeypatch.setattr(session_module, "VERIFY_SETTLE", 0.0)
    path.write_text("[device]\nname = Fireface 802\n"
                    "[route:x]\nplayback = 1/2\noutput = 29/30\n")

    class Running:
        def poll(self):
            return None

    with caplog.at_level("WARNING"):
        verifier = session_module._apply_and_verify(Running(), started,
                                                    {"stop": False}, path)
    assert verifier is not None
    verifier.join(timeout=5)
    assert [route.output for route in applied[0].routes] == [(29, 30)]
    notice = "was checked for 'Fireface 802' and is used for 'Fireface UCX II'"
    assert caplog.text.count(notice) == 1
    # The same file, read by a start that already said so: not twice.
    caplog.clear()
    restarted = profiles.load_config(path)
    cli._override_device(restarted, "Fireface UCX II")
    with caplog.at_level("WARNING"):
        notices_mod.log_desk_notices(restarted)
        session_module._apply_and_verify(Running(), restarted,
                                         {"stop": False}, path).join(timeout=5)
    assert caplog.text.count(notice) == 1
