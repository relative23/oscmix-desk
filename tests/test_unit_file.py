"""The systemd unit: hardening that a *user* service can actually apply.

Learned the hard way. A unit with the usual hardening block --
ProtectKernelTunables, ProtectClock, RestrictSUIDSGID, DeviceAllow,
IPAddressDeny -- refuses to start under `systemd --user` with
``218/CAPABILITIES``: the user manager is unprivileged, so it cannot drop
capabilities or program a cgroup controller. The audio stops and the
journal says "Failed to drop capabilities", which reads like a bug in
this project rather than a directive that does not belong here.

So the forbidden list below is not style. Each entry breaks the service.
"""

import pytest
from support import repo_file

# Applied and verified by starting the unit, not by reading the manual.
REQUIRED = [
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "ProtectSystem=strict",
    "ProtectHome=read-only",
    "LockPersonality=yes",
    "MemoryDenyWriteExecute=yes",
    "SystemCallArchitectures=native",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
    # Added in 0.4.x, each one started against a probe unit and then
    # against the real service. `systemd-analyze security --user` went
    # from 8.3 EXPOSED to 5.4 MEDIUM, and a tone still lands on every
    # configured output.
    "UMask=0077",
    "KeyringMode=private",
    "RestrictNamespaces=yes",
    "RestrictSUIDSGID=yes",
    "RestrictRealtime=yes",
    "ProtectKernelTunables=yes",
    "ProtectControlGroups=yes",
    "SystemCallFilter=@system-service",
]

# Directives this unit may not carry, and the reason is not the same for
# all of them. Measured on systemd 259 by starting a probe unit with each
# one in turn, because three entries here were listed as impossible and
# are not: `ProtectKernelTunables`, `ProtectControlGroups` and
# `RestrictSUIDSGID` were rejected in this list from 0.2.0 and start
# fine, which is why they moved to REQUIRED above. An assumption that
# has never been run is not a constraint.
#
# The user manager refuses these outright (probe unit fails to start):
FORBIDDEN = [
    "ProtectKernelModules",
    "ProtectKernelLogs",
    "ProtectClock",
    "CapabilityBoundingSet",
    "AmbientCapabilities",
    "DeviceAllow",
    "IPAddressAllow",
    "IPAddressDeny",
    "PrivateDevices",
    "User=",
    "Group=",
]

# These the user manager accepts and this service still must not have,
# which is the more interesting half: a probe unit proves nothing about
# a workload.
FORBIDDEN_THOUGH_ACCEPTED = {
    "PrivateNetwork":
        "the mixer GUI reaches the backend over 127.0.0.1; a private "
        "network namespace cuts it off and the meters go dead",
    "ProcSubset":
        "device discovery reads /proc/asound/seq/clients, which is not "
        "a process file",
    "PrivateUsers":
        "untested against ALSA device access, and an untested "
        "restriction on the audio path is not a hardening",
}


@pytest.fixture(scope="module")
def unit():
    return repo_file("systemd", "oscmix.service").read_text()


@pytest.mark.parametrize("directive", REQUIRED)
def test_the_hardening_that_works_is_present(unit, directive):
    assert directive in unit


@pytest.mark.parametrize("directive", sorted(FORBIDDEN_THOUGH_ACCEPTED))
def test_directives_the_workload_cannot_survive_stay_out(unit, directive):
    """Accepted by the user manager and still wrong here.

    The distinction matters: a probe unit that starts proves the manager
    allows the directive, not that this service works under it.
    """
    lines = [line.strip() for line in unit.splitlines()
             if not line.strip().startswith("#")]
    offenders = [line for line in lines if line.startswith(directive)]
    assert offenders == [], "%s: %s" % (directive,
                                        FORBIDDEN_THOUGH_ACCEPTED[directive])


@pytest.mark.parametrize("directive", FORBIDDEN)
def test_directives_that_break_a_user_unit_stay_out(unit, directive):
    lines = [line.strip() for line in unit.splitlines()
             if not line.strip().startswith("#")]
    offenders = [line for line in lines if line.startswith(directive)]
    assert offenders == [], (
        "%s cannot be applied by an unprivileged user manager; the unit "
        "would fail with 218/CAPABILITIES and the audio would stop"
        % directive)


def test_the_notify_and_restart_contract_is_intact(unit):
    # These encode the exit-code model: 2 means a config error no restart
    # can fix, and Type=notify means "started" implies routing applied.
    assert "Type=notify" in unit
    assert "RestartPreventExitStatus=2" in unit
    assert "Restart=on-failure" in unit


def test_the_stop_timeout_outlasts_the_kill_escalation(unit, session_mod):
    # supervise() waits CHILD_STOP_GRACE before SIGKILL. If systemd gave
    # up first it would kill the session instead, and a clean shutdown
    # would be reported as a failure.
    from oscmix_desk import constants

    stop_timeout = next(int(line.split("=")[1].rstrip("s"))
                        for line in unit.splitlines()
                        if line.startswith("TimeoutStopSec="))
    assert stop_timeout > constants.CHILD_STOP_GRACE


def directive(unit, name):
    """The value of a directive, from the unit rather than from memory."""
    for line in unit.splitlines():
        line = line.strip()
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("%s= is not in the unit" % name)


def seconds(raw):
    return float(raw.rstrip("s"))


def test_the_service_writes_only_the_lock_directory(unit):
    # The session writes exactly one thing: the device lock. Anything
    # more is a decision docs/SECURITY-MODEL.md argues first, and this
    # assertion keeps it from happening quietly. The line is not
    # decoration: measured under a manager that applies ProtectSystem,
    # the unit without it could not create a lock file at all (0.6.9).
    assert directive(unit, "ReadWritePaths") == "-/run/oscmix-desk"

# --------------------------------------------------------------------------
# The timing budget has to compose -- roadmap item H.
#
# Eight waits in constants.py and two systemd deadlines. The relationship
# between them used to live in a comment in the unit, where nothing
# checked it. `--timeout` is a command-line argument, so an edited
# ExecStart could push the start past TimeoutStartSec and have the unit
# killed *during* the apply: a torn routing state reached by editing a
# number, which is item A's failure with none of item A's difficulty.
# --------------------------------------------------------------------------

def test_the_worst_case_path_to_ready_fits_inside_the_start_deadline(unit):
    from oscmix_desk import constants

    budget = constants.startup_budget()
    start_deadline = seconds(directive(unit, "TimeoutStartSec"))
    assert budget < start_deadline, (
        "worst case to READY=1 is %.1fs but TimeoutStartSec is %.0fs -- "
        "systemd would kill the session mid-apply" % (budget, start_deadline)
    )
    # With margin, not merely inside it. The device wait is the dominant
    # term and it is a wall-clock wait on hardware enumeration, which is
    # not a quantity to leave a second of slack on.
    assert start_deadline - budget >= 10.0, (
        "only %.1fs of margin between the worst case (%.1fs) and "
        "TimeoutStartSec (%.0fs)"
        % (start_deadline - budget, budget, start_deadline)
    )


def test_the_units_own_execstart_stays_inside_the_budget(unit):
    # If ExecStart ever grows a --timeout, it is the number that decides
    # the budget, not DEFAULT_DEVICE_TIMEOUT. Parse what is actually
    # there rather than what the default happens to be.
    from oscmix_desk import constants

    exec_start = directive(unit, "ExecStart").split()
    device_timeout = constants.DEFAULT_DEVICE_TIMEOUT
    if "--timeout" in exec_start:
        device_timeout = float(exec_start[exec_start.index("--timeout") + 1])
    budget = constants.startup_budget(device_timeout)
    assert budget < seconds(directive(unit, "TimeoutStartSec"))


def test_the_largest_device_timeout_that_still_fits_is_stated(unit):
    # The number an operator actually needs when editing ExecStart: how
    # far --timeout may be raised before the unit starts killing itself
    # mid-apply. Derived, so it cannot go stale against the constants.
    from oscmix_desk import constants

    start_deadline = seconds(directive(unit, "TimeoutStartSec"))
    overhead = constants.startup_budget(0.0)
    headroom = start_deadline - overhead - 10.0   # same margin as above
    assert headroom > constants.DEFAULT_DEVICE_TIMEOUT, (
        "the default device timeout (%.0fs) already leaves no room to "
        "raise it" % constants.DEFAULT_DEVICE_TIMEOUT
    )
    assert constants.startup_budget(headroom) + 10.0 <= start_deadline


def test_the_stop_grace_fits_inside_the_stop_deadline(unit):
    # The other direction, and the one that was already covered: systemd
    # must not give up before supervise() has escalated to SIGKILL, or a
    # clean shutdown is reported as a failure.
    from oscmix_desk import constants

    stop_deadline = seconds(directive(unit, "TimeoutStopSec"))
    assert stop_deadline > constants.CHILD_STOP_GRACE
    # supervise polls in 0.5 s steps, so the escalation lands up to one
    # step after the grace period.
    assert stop_deadline > constants.CHILD_STOP_GRACE + 0.5


def test_the_budget_names_every_wait_on_the_path(unit):
    # A wait added to the startup path and not to startup_budget makes
    # the assertions above pass while the real path grows. This ties the
    # sum to its terms, so adding one without the other fails here.
    from oscmix_desk import constants

    expected = (constants.DEFAULT_DEVICE_TIMEOUT
                + constants.STALE_BACKEND_SETTLE
                + constants.PORT_READY_TIMEOUT
                + constants.SWITCH_LOCK_WAIT
                + max(constants.LINK_ECHO_TIMEOUT, constants.LINK_SETTLE))
    assert constants.startup_budget() == expected
    # The link barrier is one wait or the other, never both: the settle
    # only runs when the echo could not be observed at all.
    assert constants.startup_budget() < (
        constants.DEFAULT_DEVICE_TIMEOUT + constants.STALE_BACKEND_SETTLE
        + constants.PORT_READY_TIMEOUT + constants.SWITCH_LOCK_WAIT
        + constants.LINK_ECHO_TIMEOUT + constants.LINK_SETTLE)


def test_verification_is_off_the_startup_path_structurally(unit):
    # LINK_SYNC_BLIND_DELAY (5 s) and VERIFY_TIMEOUT (10 s) are excluded
    # from the budget because verification runs on a daemon thread after
    # READY=1, not because the arithmetic happens to work out -- with the
    # current 33 s of margin both would in fact still fit inside
    # TimeoutStartSec. An arithmetic argument would stop holding the
    # moment the margin shrank, so assert the structure instead: the
    # readiness signal is sent after _apply_and_verify returns, and that
    # function's waiting happens on a thread.
    import ast
    import inspect

    from oscmix_desk import session

    tree = ast.parse(inspect.getsource(session._apply_and_verify))
    starts_a_thread = any(
        isinstance(node, ast.Attribute) and node.attr == "Thread"
        for node in ast.walk(tree))
    assert starts_a_thread, (
        "_apply_and_verify no longer defers verification to a thread; "
        "the blind delay and the verify window are now on the path to "
        "READY=1 and startup_budget must account for them")

    # ... and READY=1 follows the apply, and only a successful one. Since
    # 0.6.10 the apply sits in _apply_or_fail: the try body calls
    # _apply_and_verify, the `else` sends READY, and no except branch may.
    # (ast.walk is breadth-first, not source order, which makes any index
    # comparison over it meaningless anyway.)
    def calls(statements, name):
        for statement in statements:
            for node in ast.walk(statement):
                if isinstance(node, ast.Call) and \
                        getattr(node.func, "id", getattr(node.func, "attr", "")) == name:
                    return True
        return False

    tree = ast.parse(inspect.getsource(session._apply_or_fail))
    tries = [node for node in ast.walk(tree) if isinstance(node, ast.Try)]
    assert len(tries) == 1, "the apply is guarded by exactly one try"
    guard = tries[0]
    assert calls(guard.body, "_apply_and_verify")
    assert calls(guard.orelse, "sd_notify"), (
        "READY=1 must follow the apply in the try's else branch")
    for handler in guard.handlers:
        assert not calls(handler.body, "sd_notify"), (
            "READY=1 in an except branch reports a desk that was not written")
    run = ast.parse(inspect.getsource(session.run_session))
    assert calls(run.body, "_apply_or_fail"), "run_session applies through it"


def test_systemd_analyze_accepts_the_unit():
    """Roadmap item J: a typo in a directive *name* passes every test above.

    Everything else in this file matches strings. systemd ignores keys it
    does not know, so `NoNewPrivilegs=yes` reads as hardening, disables
    it, and satisfies `"NoNewPrivileges=yes" in unit` -- no, it does not,
    but `ProtectSystm=strict` next to a correct NoNewPrivileges would.
    Only systemd knows the key names.

    Skipped rather than failed where systemd-analyze is absent: this has
    to stay runnable on a machine without systemd, and CI has one.
    """
    import subprocess

    from support import repo_file

    script = repo_file("scripts", "verify-unit.sh")
    result = subprocess.run(["sh", str(script)], capture_output=True,
                            text=True, timeout=60)
    if result.returncode == 77:
        pytest.skip("systemd-analyze is not installed")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_verify_script_fails_on_an_unknown_directive(tmp_path):
    # The check above only means something if the script can fail.
    # systemd-analyze reports an unknown key on stderr and still exits 0,
    # so the gate is the output; this proves the script reads it.
    import shutil
    import subprocess

    from support import repo_file

    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is not installed")

    broken = tmp_path / "oscmix.service"
    broken.write_text(repo_file("systemd", "oscmix.service").read_text()
                      .replace("NoNewPrivileges=yes", "NoNewPrivilegs=yes"))
    script = repo_file("scripts", "verify-unit.sh")
    result = subprocess.run(["sh", str(script), str(broken)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "NoNewPrivilegs" in result.stderr


def test_the_verify_script_tolerates_an_uninstalled_execstart(tmp_path):
    """The regression this check learned in CI.

    `systemd-analyze verify` also resolves `ExecStart=` and reports the
    binary as missing when it is not installed. That is the normal state
    of a fresh checkout, and it says nothing about whether the unit is
    well-formed -- but the first version of scripts/verify-unit.sh
    treated every line naming the unit as a finding, so the gate passed
    locally (where the unit *is* installed) and failed on the runner.

    Whether ExecStart points at something that exists and works is
    proven by tests/test_install_sh.py, which installs into a throwaway
    HOME and runs the result.
    """
    import shutil
    import subprocess

    from support import repo_file

    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze is not installed")

    uninstalled = tmp_path / "oscmix.service"
    uninstalled.write_text("\n".join(
        "ExecStart=/nonexistent/oscmix-session" if line.startswith("ExecStart=")
        else line
        for line in repo_file("systemd", "oscmix.service").read_text().splitlines()
    ) + "\n")

    script = repo_file("scripts", "verify-unit.sh")
    result = subprocess.run(["sh", str(script), str(uninstalled)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    # Reported, not hidden: a check that swallows findings is worse than
    # no check.
    assert "not installed on this machine" in result.stderr

    # ... and a real fault in the same file is still caught.
    both = tmp_path / "broken.service"
    both.write_text(uninstalled.read_text()
                    .replace("NoNewPrivileges=yes", "NoNewPrivilegs=yes"))
    result = subprocess.run(["sh", str(script), str(both)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 1
    assert "NoNewPrivilegs" in result.stderr
