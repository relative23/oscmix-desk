"""Two identical boxes, the desk for the second: what the tests of the
interface's identity share -- one file until it was 1900 lines."""

from pathlib import Path

DESK = "[route:main]\nplayback = 1/2\noutput = 1/2\n"


def lock_dir(tmp_path, monkeypatch):
    shared = tmp_path / "locks"
    shared.mkdir(mode=0o770, exist_ok=True)
    monkeypatch.setenv("OSCMIX_LOCK_DIR", str(shared))
    return shared


A = (24, "24216011")
B = (28, "99887766")


def add_clients(proc, text):
    clients = proc / "asound" / "seq" / "clients"
    clients.write_bytes(clients.read_bytes() + text)


def unit(environ=None, argv=("oscmix-session",), cwd="/"):
    """A stubbed `unit_process` answer."""
    from oscmix_desk.process import UnitProcess

    return lambda *a: UnitProcess(argv=tuple(argv), environ=dict(environ or {}),
                                  cwd=Path(cwd))
