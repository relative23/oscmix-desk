"""When the routing is re-applied, and how it is asked for.

Three triggers were on the roadmap: SIGHUP, resume, hotplug. Only two of
them needed building.

**Hotplug was already covered**, and building a second path for it would
have been the mistake this release keeps finding. `udev/90-rme-fireface.rules`
pulls `oscmix.service` in on `add`, and on `remove` the backend exits
with its device, so a replug is a full process restart with a full apply --
recorded in `tests/data/cold-plug-timeline.json`, whose condition line
reads "cold USB replug -- device unplugged 14.4 s, udev restarted the
unit".

**SIGHUP** is the mechanism, and it reconciles rather than restarting:
pinned settings are re-applied, remembered ones are left where the user
put them.

**Resume** queues an ordered system service from the sleep hook. It reaches
the user managers only after systemd has thawed them.
"""

import configparser
import json
import os
import subprocess
import sys

import pytest
from support import repo_file


def unit_text():
    return repo_file("systemd", "oscmix.service").read_text()


# --------------------------------------------------------------------------
# How a reconcile is asked for.
# --------------------------------------------------------------------------

def test_the_unit_reloads_by_signalling_only_the_main_process():
    """`systemctl kill` is a footgun here, and that was measured.

    `systemctl --user kill --signal=SIGHUP oscmix.service` signals *every*
    process in the unit. Neither oscmix nor alsaseqio installs a SIGHUP
    handler, so the default action applies and both die; the service went
    inactive on the machine this was tried on.

    ExecReload with $MAINPID reaches the session process alone, which is
    the only one that knows what a reload means.
    """
    text = unit_text()
    assert "ExecReload=" in text, "no way to reconcile without a restart"
    reload_line = next(line for line in text.splitlines()
                       if line.startswith("ExecReload="))
    assert "$MAINPID" in reload_line, (
        "a reload must not reach the backend processes: %s" % reload_line)
    assert "HUP" in reload_line


def test_the_unit_does_not_restart_to_reconcile():
    # A restart would tear the backend down and re-apply everything
    # indiscriminately, putting remembered faders back -- which is what
    # the pin/remember model exists to prevent.
    reload_line = next(line for line in unit_text().splitlines()
                       if line.startswith("ExecReload="))
    assert "restart" not in reload_line.lower()


# --------------------------------------------------------------------------
# Resume.
# --------------------------------------------------------------------------

@pytest.fixture
def resume_hook(tmp_path):
    """Execute the real hook with isolated system-manager command doubles."""
    log = tmp_path / 'calls.jsonl'
    login = tmp_path / 'loginctl'
    login.write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_USERS"\n'
                     'exit "${TEST_LOGIN_CODE:-0}"\n')
    control = tmp_path / 'systemctl'
    control.write_text('#!' + sys.executable + '\n' + r"""
import json
import os
import sys
args = sys.argv[1:]
with open(os.environ['TEST_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\n')
if 'start' in args:
    sys.exit(int(os.environ.get('TEST_QUEUE_CODE', '0')))
user = next(a for a in args if a.startswith('--machine='))[10:].split('@')[0]
if 'is-active' in args:
    sys.exit(0 if user in os.environ['TEST_ACTIVE'].split(',') else 3)
if 'reload' in args:
    sys.exit(1 if user == os.environ.get('TEST_FAIL_USER') else 0)
sys.exit(99)
""")
    login.chmod(0o755)
    control.chmod(0o755)

    def invoke(*args, **extra):
        env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
                   TEST_LOG=str(log), TEST_USERS='1000 alice no active\n1001 bob no active',
                   TEST_ACTIVE='alice,bob')
        env.update(extra)
        result = subprocess.run([str(repo_file('systemd/system-sleep/oscmix')), *args],
                                env=env, capture_output=True, text=True, timeout=5)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return invoke


@pytest.mark.parametrize('args', [(), ('pre', 'suspend'), ('unknown',)])
def test_resume_hook_does_nothing_before_waking(resume_hook, args):
    result, calls = resume_hook(*args)
    assert result.returncode == 0
    assert calls == []


def test_resume_hook_queues_only_one_nonblocking_system_job(resume_hook):
    result, calls = resume_hook('post', 'suspend')
    assert result.returncode == 0
    assert calls == [['--no-block', 'start', 'oscmix-resume.service']]


def test_resume_hook_reports_a_failed_queue_request(resume_hook):
    result, _ = resume_hook('post', 'suspend', TEST_QUEUE_CODE='1')
    assert result.returncode == 1


def test_resume_service_waits_for_all_sleep_services_and_is_bounded():
    unit = configparser.ConfigParser(interpolation=None)
    unit.read(repo_file('systemd/oscmix-resume.service'))
    assert set(unit['Unit']['After'].split()) == {
        'systemd-' + mode + '.service' for mode in
        ('suspend', 'hibernate', 'hybrid-sleep', 'suspend-then-hibernate')}
    assert unit['Service']['Type'] == 'oneshot'
    assert unit['Service']['ExecStart'] == '/usr/lib/systemd/system-sleep/oscmix reload-active'
    assert 0 < int(unit['Service']['TimeoutStartSec']) <= 30
    assert 'Install' not in unit  # Queued by resume, never enabled at boot.


def test_resume_reloads_only_active_desks_without_starting_them(resume_hook):
    result, calls = resume_hook('reload-active', TEST_ACTIVE='alice')
    assert result.returncode == 0
    assert calls == [
        ['--user', '--machine=alice@.host', 'is-active', '--quiet', 'oscmix.service'],
        ['--user', '--machine=alice@.host', 'reload', 'oscmix.service'],
        ['--user', '--machine=bob@.host', 'is-active', '--quiet', 'oscmix.service']]


def test_resume_reports_reload_failure_and_still_reaches_other_users(resume_hook):
    result, calls = resume_hook('reload-active', TEST_FAIL_USER='alice')
    assert result.returncode == 1
    assert 'resume reload failed for alice' in result.stderr
    assert calls[-1] == ['--user', '--machine=bob@.host', 'reload', 'oscmix.service']


def test_resume_reports_failed_user_enumeration_without_writing(resume_hook):
    result, calls = resume_hook('reload-active', TEST_LOGIN_CODE='1')
    assert result.returncode == 1
    assert calls == []


# --------------------------------------------------------------------------
# Hotplug: covered already, and this says where.
# --------------------------------------------------------------------------

def test_hotplug_is_handled_by_udev_and_not_by_a_second_mechanism():
    """The trigger that needed no code.

    If this ever stops being true -- the rule loses its `add` pull-in --
    then hotplug silently stops re-applying anything, and the session has
    no path of its own to fall back on. The `remove` half is asserted too,
    though what ends the service on unplug is the backend exiting with
    its device, not `StopWhenUnneeded` (an enabled unit is never unneeded;
    ADR 0013, amended). So all of it is asserted here rather than assumed
    from a comment.
    """
    rules = repo_file("udev", "90-rme-fireface.rules").read_text()
    assert 'ACTION=="add"' in rules
    assert "SYSTEMD_USER_WANTS" in rules
    assert "oscmix.service" in rules
    assert 'ACTION=="remove"' in rules
    assert "StopWhenUnneeded=yes" in unit_text()


def test_no_timer_anywhere_triggers_a_reconcile():
    """Triggers are events, never a clock.

    A timer would make this a background process that fights the user on
    a schedule -- and, given that the device does not report a change,
    each tick would cost a full 2252-register dump. The roadmap ruled it
    out and this keeps it ruled out.
    """
    for path in repo_file("systemd").rglob("*"):
        if path.is_file():
            assert path.suffix != ".timer", path
    assert "OnUnitActiveSec" not in unit_text()
    assert "OnCalendar" not in unit_text()


# --------------------------------------------------------------------------
# The trigger that was measured and then not built.
# --------------------------------------------------------------------------

def test_no_sample_rate_trigger_exists_and_that_is_deliberate():
    """A rate change destroys nothing on this device, so nothing reacts.

    Measured on a UCX II across 48 kHz -> 44.1 kHz: 1931 of 1932 reported
    registers were identical, the one that differed was
    `/clock/samplerate` itself, and the playback mix matrix survived too
    -- shown by signal, since it is never reported. A 1 kHz tone at
    -40 dBFS into playback 1/2 still came out at outputs 1, 5 and 7.

    The trigger would have been the cheapest of the three: unlike the
    registers a config sets, `/clock/samplerate` *is* pushed when it
    changes, so no poll is needed. It is not built because there is
    nothing measured for it to repair, and this test exists so that
    stays a decision rather than becoming an oversight -- if somebody
    adds the handler, they have to come here and say what loss it fixes.

    docs/decisions/0013-reconcile-triggers.md, "Alternatives considered".
    """
    import ast

    # Parsed, not grepped: the comment in devices.py that records the
    # measurement mentions the register by name, and a text search would
    # have banned the explanation along with the feature. An AST sees
    # string literals only.
    #
    # devices.py is excluded, and the distinction is the point: that
    # file *declares* the register -- with no domain, because upstream's
    # node has no setter -- so the read-back and --dump-config know it
    # exists. Declaring is not reacting. Anywhere else, naming this path
    # in code means something is watching it.
    handlers = []
    for path in sorted(repo_file("src", "oscmix_desk").rglob("*.py")):
        if path.name == "devices.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and "clock/samplerate" in node.value):
                handlers.append("%s:%d" % (path.name, node.lineno))
    assert handlers == [], (
        "something now acts on the sample rate: %s -- ADR 0013 says the "
        "measured loss is zero, so say what changed" % handlers)
