"""A write that fails part of the way (third outside review, ADR 0027).

A switch promises an outcome and never an exception (ADR 0011). Until
0.7.0 a socket error after some of the registers had gone was a
traceback out of `--profile`, with nothing said about which part of the
profile was on the device.
"""

import errno

import pytest
from backend_doubles import RecordingBackend, echo_link_flags_only
from profile_desk import GOOD, TRACKING, desk
from support import free_udp_port

from oscmix_desk import backend as backend_mod
from oscmix_desk import cli, profiles
from oscmix_desk import marker as marker_mod
from oscmix_desk import outcome as outcome_mod
from oscmix_desk.constants import EXIT_CONFIG, EXIT_FAILURE
from oscmix_desk.errors import WriteFailed

GONE = OSError(errno.ENETUNREACH, "Network is unreachable")


class FailingBackend(RecordingBackend):
    """Hands ``allowed`` registers to the wire, then the network is gone --
    and says so the way the real backend does."""

    def __init__(self, allowed, dumps_fail=False):
        super().__init__(reports=echo_link_flags_only)
        self.allowed = allowed
        self.dumps_fail = dumps_fail

    def send(self, messages):
        burst = [message[0] for message in messages]
        room = max(self.allowed - len(self.sent), 0)
        super().send((path, "", ()) for path in burst[:room])
        if len(burst) > room:
            raise WriteFailed(GONE, burst[:room], burst[room:])

    def request_dump(self):
        if self.dumps_fail:
            raise WriteFailed(GONE, [], ["/refresh"])
        super().request_dump()


@pytest.fixture(autouse=True)
def _traits():
    RecordingBackend.traits = backend_mod.OSCMIX


def _whole(tmp_path, name="tracking"):
    """Every register a switch to the profile writes, in order."""
    path = desk(tmp_path, main=GOOD, tracking=TRACKING)
    device = FailingBackend(allowed=10 ** 6)
    applied = (profiles.switch_profile(name, config_path=path, backend=device,
                                       verify=False)
               if name != "routing.conf" else
               profiles.restore_main(config_path=path, backend=device,
                                     verify=False))
    assert applied.applied
    marker_mod.forget_active_profile(path)
    return path, [sent[0] for sent in device.sent]


def test_a_write_that_never_starts_is_a_refusal(tmp_path):
    path, _all = _whole(tmp_path)
    device = FailingBackend(allowed=0)
    outcome = profiles.switch_profile("tracking", config_path=path,
                                      backend=device, verify=False)
    assert outcome.state == outcome_mod.REFUSED
    assert not outcome.applied
    assert "cannot write to the backend on UDP" in outcome.reason
    assert "Network is unreachable" in outcome.reason
    assert marker_mod.active_profile(path) is None


@pytest.mark.parametrize("allowed", [1, 2])
def test_a_write_that_fails_part_of_the_way_says_how_far_it_came(tmp_path,
                                                                 allowed):
    """Half of the links, or all of them and nothing of the mix: whichever
    burst gives out, the two lists are the whole plan, in its order."""
    path, everything = _whole(tmp_path)
    assert [p.split("/")[1] for p in everything] == [
        "playback", "output", "mix"], "two links and a mix, or no test"
    device = FailingBackend(allowed)
    outcome = profiles.switch_profile("tracking", config_path=path,
                                      backend=device, verify=False)
    assert outcome.state == outcome_mod.WRITTEN_IN_PART
    assert outcome.applied, "something reached the device, and it says so"
    assert outcome.written == everything[:allowed]
    assert outcome.unwritten == everything[allowed:]
    assert (outcome.persisted, outcome.read_back) == (False, False)
    assert marker_mod.active_profile(path) is None, \
        "the desk in effect is still the one a reload writes back"
    line = outcome.describe()
    assert line.startswith("wrote %d of %d register(s) of 'tracking' and then "
                           "could not: cannot write to the backend on UDP"
                           % (allowed, len(everything)))
    assert "written: %s" % everything[0] in line
    assert line.endswith("the desk in effect has not changed, and a reload "
                         "or start writes it back")


def test_a_restore_that_fails_part_of_the_way_keeps_the_profile(tmp_path):
    path, _all = _whole(tmp_path)
    (tmp_path / "active-profile").write_text("tracking\n")
    outcome = profiles.restore_main(config_path=path,
                                    backend=FailingBackend(2), verify=False)
    assert outcome.state == outcome_mod.WRITTEN_IN_PART
    assert outcome.name == "routing.conf"
    assert len(outcome.written) == 2
    assert marker_mod.active_profile(path) == "tracking"


def test_a_read_back_that_cannot_be_asked_for_is_not_a_traceback(tmp_path):
    """All of it went out, and the request for the state did not."""
    path, _all = _whole(tmp_path)
    device = FailingBackend(allowed=10 ** 6, dumps_fail=True)
    outcome = profiles.switch_profile("tracking", config_path=path,
                                      backend=device)
    assert outcome.state == outcome_mod.APPLIED_UNVERIFIED
    assert (outcome.read_back, outcome.persisted) == (False, True)
    assert outcome.reason == "Network is unreachable"


def test_the_command_exits_1_and_asks_the_unit_to_put_the_desk_back(
        tmp_path, monkeypatch, capsys):
    path = desk(tmp_path, main=GOOD, tracking=TRACKING)
    reloads = []
    monkeypatch.setattr(cli, "reload_service",
                        lambda: reloads.append(1) or cli.RELOAD_DONE)
    monkeypatch.setattr(cli, "_unit_desk", lambda: (True, path))
    for allowed, code, reloaded in ((2, EXIT_FAILURE, [1]),
                                    (0, EXIT_CONFIG, [])):
        del reloads[:]
        monkeypatch.setattr(
            cli, "switch_profile",
            lambda name, config_path, n=allowed: profiles.switch_profile(
                name, config_path=config_path, backend=FailingBackend(n),
                verify=False))
        assert cli.main(["--config", str(path), "--profile", "tracking"]) \
            == code
        assert reloads == reloaded
    assert "nothing written" in capsys.readouterr().out


# --------------------------------------------------------------------------
# The accounting itself, against a socket that gives out.
# --------------------------------------------------------------------------

class _Socket:
    def __init__(self, allowed, log):
        self.allowed, self.log = allowed, log

    def sendto(self, data, _address):
        if len(self.log) >= self.allowed:
            raise OSError(errno.ENETUNREACH, "Network is unreachable")
        self.log.append(data)

    def close(self):
        self.log.append("closed")


def test_the_backend_says_which_of_a_burst_went_out(monkeypatch):
    sent = []
    device = backend_mod.loopback(free_udp_port(), free_udp_port())
    monkeypatch.setattr(backend_mod.socket, "socket",
                        lambda *a: _Socket(2, sent))
    burst = [("/output/%d/volume" % n, "f", (0.0,)) for n in (1, 2, 3, 4)]
    with pytest.raises(WriteFailed) as failed:
        device.send(burst)
    assert failed.value.written == ("/output/1/volume", "/output/2/volume")
    assert failed.value.unwritten == ("/output/3/volume", "/output/4/volume")
    assert (failed.value.errno, failed.value.strerror) == (
        errno.ENETUNREACH, "Network is unreachable")
    assert sent[-1] == "closed", "and the socket is not left open"


def test_a_socket_that_cannot_be_had_is_a_write_that_never_started(
        monkeypatch):
    def none_left(*_a):
        raise OSError(errno.EMFILE, "Too many open files")

    device = backend_mod.loopback(free_udp_port(), free_udp_port())
    monkeypatch.setattr(backend_mod.socket, "socket", none_left)
    with pytest.raises(WriteFailed) as failed:
        device.send([("/output/1/volume", "f", (0.0,))])
    assert (failed.value.written, failed.value.unwritten) == (
        (), ("/output/1/volume",))
    assert isinstance(failed.value, OSError), \
        "so a start, a verifier and a reconcile stand down as they did"
