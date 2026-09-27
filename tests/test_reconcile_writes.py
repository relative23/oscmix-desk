"""What a reconcile writes, against a backend that can be inspected.

Pinned settings are re-applied, remembered ones are left where the user
put them, and what the dump cannot report is rewritten rather than
compared.
"""



import pytest
from backend_doubles import RecordingBackend
from reconcile_desk import config_of

from oscmix_desk.backend import OSCMIX
from oscmix_desk.errors import ReceivePortError


class _Device(RecordingBackend):
    traits = OSCMIX

    def __init__(self, reports):
        super().__init__(reports=lambda _sent: iter(reports))

def test_a_reconcile_leaves_a_remembered_value_alone(tmp_path, monkeypatch):
    """The behaviour the whole trigger exists for.

    Measured at the device first: SIGHUP with the fader moved to -20.0 dB
    logged "1 left to the device (/output/1/volume)" and the device still
    read -20.0 afterwards. This is that, in a form CI can run.
    """
    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    # A mismatch deliberately holds the observation window open for a
    # correcting report, so at the shipped 10 s this test would pay all
    # of it -- per mutant, in the mutation run. The outcome is what is
    # under test; tests/test_pin_remember.py owns the durations.
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/1/volume", "f", (-20.0,))])
    assert verify.reconcile_now(config_of(tmp_path), "test", backend=device)

    written = {path for path, _t, _a in device.sent}
    assert "/output/1/volume" not in written, (
        "the fader the user moved was written back")
    assert "/mix/1/playback/1" in written, (
        "the routing itself still has to be re-established")

def test_a_reconcile_corrects_a_pinned_value(tmp_path, monkeypatch):
    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/1/volume", "f", (-20.0,))])
    config = config_of(tmp_path, "\n[pin]\noutput.volume = pin\n")
    assert verify.reconcile_now(config, "test", backend=device)

    sent = {path: args for path, _t, args in device.sent}
    assert sent.get("/output/1/volume") == (-6.0,), (
        "a pinned register that drifted has to be written back")

def test_a_reconcile_refuses_rather_than_writing_blind(tmp_path):
    from oscmix_desk import verify

    class Deaf(_Device):
        def messages(self, _timeout):
            raise ReceivePortError(104, "backend disconnected")

    device = Deaf([])
    with pytest.raises(ReceivePortError, match="disconnected"):
        verify.reconcile_now(config_of(tmp_path), "test", device)
    assert device.sent == []

def test_a_stop_during_a_reconcile_writes_nothing(tmp_path):
    from oscmix_desk import verify

    device = _Device([("/output/1/stereo", "i", (1,))])
    assert verify.reconcile_now(config_of(tmp_path), "test",
                                should_stop=lambda: True,
                                backend=device) is False
    assert device.sent == []
    assert device.dumps == 0

def test_a_reconcile_corrects_what_the_register_table_pins(tmp_path,
                                                           monkeypatch):
    """The table's own policy, with no `[pin]` section anywhere.

    Found by mutation testing: replacing `device_for_name(...)` with
    `None` inside reconcile_now survived every test. With no device
    model the policy lookup falls through to REMEMBER for everything, so
    pinning stops working entirely -- and the only tests that covered
    the pinned branch used a `[pin]` override, which is consulted
    *before* the model and therefore kept working.

    `reflevel` is pinned by the table because it has to match the cable.
    Nothing overrides it here, so this fails if the model is not
    consulted.
    """
    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    config = config_of(tmp_path, "\n[output:5]\nreflevel = +4dBu\n")
    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/5/reflevel", "is", (2, "+19dBu")),
                      ("/output/1/volume", "f", (-6.0,))])
    assert verify.reconcile_now(config, "test", backend=device)

    sent = {path: args for path, _t, args in device.sent}
    # An enum is *written* as its index and *reported* as (index, name).
    # "+4dBu" is index 0 of ("+4dBu", "+13dBu", "+19dBu"); the device
    # above reports index 2, so this drifted.
    assert sent.get("/output/5/reflevel") == (0,), (
        "a register the table pins drifted and was not written back")

def test_a_reconcile_leaves_what_the_register_table_remembers(tmp_path,
                                                              monkeypatch):
    """The mirror, and the half that the same mutant also hid.

    With no device model everything reads as remembered, so a test that
    only checked the remembered direction would pass on a broken lookup.
    This pairs with the one above: same run, same backend, one register
    corrected and one left alone, decided only by the table.
    """
    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    config = config_of(tmp_path, "\n[output:5]\nreflevel = +4dBu\n")
    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/5/reflevel", "is", (2, "+19dBu")),
                      ("/output/1/volume", "f", (-20.0,))])
    assert verify.reconcile_now(config, "test", backend=device)

    written = {path for path, _t, _a in device.sent}
    assert "/output/5/reflevel" in written, "pinned by the table"
    assert "/output/1/volume" not in written, "remembered by the table"

def test_the_reconcile_log_does_not_claim_to_be_selective(tmp_path,
                                                          monkeypatch, caplog):
    """The write is not selective, so the line must not say it is.

    `reconcile_now` re-applies everything except what is kept -- it
    cannot do less, because the playback mix matrix is never reported
    and so can never be shown to be intact. An earlier wording said
    "N to correct", which reads as though only those N were written.
    """
    import logging

    from oscmix_desk import routing, verify

    monkeypatch.setattr(routing, "LINK_ECHO_TIMEOUT", 0.01)
    monkeypatch.setattr(verify, "VERIFY_TIMEOUT", 0.3)

    device = _Device([("/output/1/stereo", "i", (1,)),
                      ("/playback/1/stereo", "i", (1,)),
                      ("/output/1/volume", "f", (-6.0,))])
    with caplog.at_level(logging.INFO):
        verify.reconcile_now(config_of(tmp_path), "test", backend=device)
    line = next(r.getMessage() for r in caplog.records
                if "reconcile (test)" in r.getMessage())

    assert "re-applying" in line
    assert "to correct" not in line
    # Nothing drifted, and the routing was still written.
    assert "0 drifted" in line
    assert "/mix/1/playback/1" in {p for p, _t, _a in device.sent}
