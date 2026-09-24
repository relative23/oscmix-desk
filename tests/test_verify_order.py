"""The last report received in an open read-back window is the observation."""

import itertools

import pytest
from support import write_config

from oscmix_desk import outcome, profiles, verify
from oscmix_desk.devices import UCX2
from oscmix_desk.model import ChannelSetting, Config
from oscmix_desk.registers import PIN


class OrderedBackend:
    """Separate listener batches, without any socket or hardware access."""

    def __init__(self, batches):
        self.batches = iter(batches)
        self.closed = False
        self.dumps = 0

    def listen(self):
        return self

    def request_dump(self):
        self.dumps += 1

    def messages(self, _timeout):
        yield from next(self.batches, [])

    def close(self):
        self.closed = True


def observe(monkeypatch, registers, batches):
    backend = OrderedBackend(batches)
    seen = []
    monkeypatch.setattr(verify.time, "sleep", lambda _: None)
    monkeypatch.setattr(verify.time, "monotonic",
                        lambda: next(ticks))
    ticks = itertools.count(0, 0.01)
    result = verify.verify_routing(
        registers, 0, 0, timeout=1, device_model=UCX2, backend=backend,
        on_observed=lambda path, args: seen.append((path, args)))
    assert backend.dumps == 1
    assert backend.closed
    return result, seen


@pytest.mark.parametrize("batched", [True, False])
@pytest.mark.parametrize(("values", "matches"), [
    ([1, 0], False), ([0, 1], True), ([1, 0, 1], True),
    ([0, 1, 0], False), ([1, 1], True), ([1, float("nan")], False),
    ([1, float("inf")], False), ([1, "invalid"], False),
])
def test_last_report_replaces_the_previous_classification(
        monkeypatch, batched, values, matches):
    first, last = "/output/5/stereo", "/output/7/stereo"
    registers = {first: ("i", (1,)), last: ("i", (1,))}
    reports = [(first, "i", (value,)) for value in values]
    reports.append((last, "i", (1,)))  # keep the window open for A's changes
    batches = [reports] if batched else [[report] for report in reports]
    result, seen = observe(monkeypatch, registers, batches)
    assert result.confirmed == ([first, last] if matches else [last])
    assert result.mismatched == ([] if matches else [first])
    assert result.unobserved == []
    assert seen == [(path, args) for path, _tags, args in reports]
    assert verify._unconfirmed(result, UCX2) == ([] if matches else [first])


def test_latest_remember_difference_is_kept_but_not_called_equal(monkeypatch):
    volume, link = "/output/5/volume", "/output/7/stereo"
    result, _ = observe(monkeypatch, {volume: ("f", (0.,)), link: ("i", (1,))}, [
        [(volume, "f", (0.,))], [(volume, "f", (-20.,))], [(link, "i", (1,))]])
    assert result.confirmed == [link]
    assert result.mismatched == [volume]
    assert verify._unconfirmed(result, UCX2) == []
    assert verify._kept_by_the_device(result, UCX2) == [volume]


def test_summary_keeps_equality_policy_and_missing_reports_distinct(caplog):
    result = verify.VerifyResult(
        confirmed=["/output/5/stereo"], mismatched=["/output/5/volume"],
        unobserved=["/input/3/gain", "/mix/5/playback/1", "/mix/5/playback/2"])
    with caplog.at_level("INFO", logger="oscmix-session"):
        problems, kept = verify._report(result, Config(), UCX2, 1)
    assert problems == []
    assert kept == ["/output/5/volume"]
    assert "device value kept for /output/5/volume (remembered, not pinned)" in caplog.text
    assert "1 confirmed; 1 kept by REMEMBER" in caplog.text
    assert ("0 differing PIN; 0 missing prompt; 1 not observed; "
            "2 backend-unreportable") in caplog.text
    assert "under PIN/REMEMBER policy" in caplog.text


def test_summary_does_not_call_a_missing_prompt_or_pin_difference_verified(caplog):
    result = verify.VerifyResult(
        confirmed=[], mismatched=["/input/3/gain"],
        unobserved=["/output/5/stereo"])
    with caplog.at_level("INFO", logger="oscmix-session"):
        problems, kept = verify._report(result, Config(), UCX2, 2)
    assert problems == ["/input/3/gain", "/output/5/stereo"]
    assert kept == []
    assert "routing verified" not in caplog.text
    assert ("1 differing PIN; 1 missing prompt; 0 not observed; "
            "0 backend-unreportable") in caplog.text
    assert "after retry" in caplog.text


@pytest.fixture
def contradiction(monkeypatch, confirming_backend):
    """A fader drifts while another expected fader has yet to report."""
    reports = [('/output/5/volume', 'f', (0.,)),
               ('/output/5/volume', 'f', (-20.,)),
               ('/output/7/volume', 'f', (0.,))]
    confirming_backend._reports = lambda _: reports
    ticks = itertools.count(0, 0.1)
    monkeypatch.setattr(verify.time, 'sleep', lambda _: None)
    monkeypatch.setattr(verify.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(verify, 'VERIFY_TIMEOUT', 1.)
    return confirming_backend


def test_profile_confirmation_is_strict_even_for_a_remembered_fader(
        tmp_path, contradiction):
    write_config(tmp_path / 'profiles/levels.conf',
                 '[output:5]\nvolume=0\n[output:7]\nvolume=0\n')
    result = profiles.switch_profile('levels', tmp_path / 'routing.conf', backend=contradiction)
    assert result.state == outcome.APPLIED_UNVERIFIED
    assert result.read_back is True
    assert result.unverified == ['/output/5/volume']


@pytest.mark.parametrize('pin', [False, True])
def test_startup_uses_latest_report_for_repair_and_remember(
        monkeypatch, contradiction, pin, caplog):
    config = Config(channels=[ChannelSetting('output', n, 'volume', 0.) for n in (5, 7)],
                    policies={('output', 'volume'): PIN} if pin else {})
    writes = []
    monkeypatch.setattr(verify, 'loopback', lambda *_: contradiction)
    monkeypatch.setattr(verify, 'apply_routing', lambda *_a, **kw: writes.append(kw))
    with caplog.at_level('INFO', logger='oscmix-session'):
        verify.verify_and_repair(config)
    assert len(writes) == int(pin)
    assert ('unconfirmed after retry' in caplog.text) is pin
    assert ('1 kept by REMEMBER' in caplog.text) is not pin


def test_reconcile_does_not_overwrite_a_value_that_drifted_after_confirmation(contradiction):
    config = Config(channels=[ChannelSetting('output', n, 'volume', 0.) for n in (5, 7)])
    assert verify.reconcile_now(config, 'latest-report regression', backend=contradiction)
    assert contradiction.sent == [('/output/7/volume', 'f', (0.,))]
