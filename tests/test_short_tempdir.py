"""Short socket directories must respect a usable configured temp root."""

# These path choices are mocked; the test creates no directory.
# ruff: noqa: S108

from pathlib import Path

import pytest
import support


@pytest.mark.parametrize(("configured", "expected"), [
    ("/tmp", "/tmp"),
    ("/var/tmp/c91", "/var/tmp/c91"),
    ("/tmp/" + "a" * 15, "/tmp/" + "a" * 15),
    ("/tmp/" + "a" * 16, "/tmp"),
    ("/tmp/" + "é" * 8, "/tmp"),
])
def test_short_socket_directories_use_short_tmpdir_with_a_byte_length_limit(
        monkeypatch, configured, expected):
    created = []

    def make_directory(*, prefix, **kwargs):
        created.append((prefix, kwargs["dir"]))
        return str(Path(kwargs["dir"]) / (prefix + "generated"))

    monkeypatch.setattr(support.tempfile, "gettempdir", lambda: configured)
    monkeypatch.setattr(support.tempfile, "mkdtemp", make_directory)
    monkeypatch.setattr(support.os, "access", lambda *_: True)
    monkeypatch.setattr(support, "SHORT_DIRECTORIES", [])
    path = support.short_directory("od-")
    assert created == [("od-", expected)]
    assert path == Path(expected) / "od-generated"
    assert len(support.SHORT_DIRECTORIES) == 1
    assert support.SHORT_DIRECTORIES[0] == path
