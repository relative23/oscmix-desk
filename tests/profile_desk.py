"""A desk with profiles beside it, for the tests of the switch, the lock
and the marker -- which were one file until the modules they test were
three."""

from conftest import free_udp_port, write_config

GOOD = """
[route:main]
output = 1/2
playback = 1/2
level = 0.0

[output:1]
volume = -10.0
"""


def desk(tmp_path, main=GOOD, **named):
    """routing.conf with free ports, plus named profiles beside it."""
    for name, text in named.items():
        write_config(tmp_path / "profiles" / ("%s.conf" % name), text)
    return write_config(tmp_path / "routing.conf",
                        "[osc]\nport = %d\nrecv-port = %d\n%s"
                        % (free_udp_port(), free_udp_port(), main))


TRACKING = """
[route:direct]
output = 5/6
playback = 5/6
"""


def shared_lock_dir(tmp_path, monkeypatch):
    """A stand-in for /run/oscmix-desk, which tests may not touch."""
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o1777, exist_ok=True)
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    return shared


def retargeting_desk(tmp_path):
    path = write_config(tmp_path / "routing.conf", "[osc]\nport = 9001\n" + GOOD)
    write_config(tmp_path / "profiles" / "here.conf", GOOD)
    write_config(tmp_path / "profiles" / "same.conf", "[osc]\nport = 9001\n" + GOOD)
    write_config(tmp_path / "profiles" / "there.conf",
                 "[osc]\nport = 9500\n\n[device]\nserial = 99887766\n" + GOOD)
    return path
