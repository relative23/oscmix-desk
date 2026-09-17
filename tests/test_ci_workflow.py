"""Hygiene rules for the CI workflow itself.

Written after a job with no `timeout-minutes` hung on `sudo apt-get
update` and burned GitHub's 360-minute default -- six hours, on a job
whose measured maximum is under seven minutes. Four of the six jobs had
no timeout at the time; nothing said they should.

Parsed textually rather than with PyYAML, which is not a dev dependency
and would be a lot of weight for this. A textual parse can silently find
nothing and pass, so the test asserts it found the jobs it expects
before it asserts anything about them -- otherwise a formatting change
would turn this into a test that always succeeds.
"""

import re

from conftest import repo_file

WORKFLOW = ("quality", "test", "coverage", "flake", "mutation",
            "build-oscmix")


def _jobs(name="ci.yml"):
    """Job id -> its block of the workflow, for jobs that run somewhere."""
    text = repo_file(".github", "workflows", name).read_text()
    blocks = re.split(r"\n  (?=[\w-]+:\n)", text)
    found = {}
    for block in blocks:
        match = re.match(r"\s*([\w-]+):", block)
        if match and "runs-on:" in block:
            found[match.group(1)] = block
    return found


def test_the_parse_finds_the_jobs_it_is_about_to_judge():
    # The guard on the guard. Without it, a reformat that breaks the
    # regex makes every assertion below vacuously true.
    assert set(_jobs()) == set(WORKFLOW)


def test_every_ci_job_has_a_timeout():
    """No job may inherit GitHub's 360-minute default.

    That default is not a ceiling anybody chose for this repository. It
    was reached once, by `sudo apt-get update` hanging on a runner, on
    the one job that builds upstream -- and the run sat there for six
    hours before GitHub killed it.
    """
    missing = sorted(job for job, block in _jobs().items()
                     if "timeout-minutes:" not in block)
    assert missing == [], (
        "these inherit the 360-minute default: %s" % missing)


def test_no_timeout_is_anywhere_near_the_default():
    """A timeout of 300 would be a default with extra steps.

    Each one is meant to catch a hang, so it belongs well above the
    job's measured maximum and far below the default -- the numbers are
    recorded in the workflow next to them. 180 for the mutation job is
    the largest: 90 against a measured 80.9 was hit every night once the
    mutant count grew, and the job reported nothing for five nights.
    """
    too_generous = {}
    for job, block in _jobs().items():
        found = re.search(r"timeout-minutes:\s*(\d+)", block)
        if found and int(found.group(1)) > 180:
            too_generous[job] = int(found.group(1))
    assert too_generous == {}, (
        "these are long enough to hide a hang: %s" % too_generous)


# --------------------------------------------------------------------------
# The concurrency group, which decides whether a commit is tested at all.
# --------------------------------------------------------------------------

def _concurrency(name="ci.yml"):
    """The `concurrency:` block, comments stripped and folded to one line.

    `group:` is a folded scalar over two lines, so a test that matched
    the raw text would break on a rewrap rather than on a change of
    meaning.
    """
    text = repo_file(".github", "workflows", name).read_text()
    match = re.search(r"\nconcurrency:\n(.*?)\n\w", text, re.DOTALL)
    assert match, "no concurrency block -- this test judges nothing"
    body = [line.split("#")[0] for line in match.group(1).splitlines()]
    return " ".join(" ".join(body).split())


def test_a_push_gets_its_own_concurrency_group():
    """Otherwise a commit can reach main having run no jobs at all.

    A group holds one running run and one queued run, and a third
    arrival cancels the queued one. Three pushes to main inside an hour
    did exactly that: the middle run reported `cancelled` with zero jobs
    and nothing was ever tested on that commit. Keying the group on the
    commit means no push run can ever be queued behind another.
    """
    assert "github.sha" in _concurrency()


def test_a_pull_request_still_supersedes_its_own_older_revisions():
    """The behaviour the group existed for in the first place: pushing a
    fix to a PR should not leave the previous revision burning runners.
    Both halves have to be there, so the fix above cannot be "drop the
    grouping entirely"."""
    group = _concurrency()
    assert "github.ref" in group
    assert "github.event_name == 'pull_request'" in group
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" \
        in group


# --------------------------------------------------------------------------
# Which jobs run when.
# --------------------------------------------------------------------------

def _job_condition(job, name="ci.yml"):
    """The `if:` line of one job, or None when it has none."""
    block = _jobs(name)[job]
    match = re.search(r"^\s{4}if: (.+)$", block, re.MULTILINE)
    return match.group(1).strip() if match else None


def test_the_mutation_job_runs_nightly_and_not_on_every_push():
    """Measured 2026-08-24: 72 minutes, and the cost is mutants times
    suite time, so it grows multiplicatively with the project.

    The value was never in the gate. The score has never failed one, in
    any run. It was in reading the survivors, which found three real
    defects in 0.3.0 -- including pinning silently not working -- while
    the policy was green either way. A nightly score change still
    prompts that reading, a day later.
    """
    condition = _job_condition("mutation")
    assert condition is not None
    assert "schedule" in condition
    assert "push" not in condition


def test_every_other_job_still_runs_on_a_push():
    """The nightly move is for the one expensive job, not a retreat from
    per-push checking. Nothing else may quietly follow it."""
    for job in WORKFLOW:
        if job == "mutation":
            continue
        condition = _job_condition(job)
        assert condition is None or "push" in condition, (
            "%s no longer runs on a push: %s" % (job, condition))


def test_the_workflow_is_scheduled_at_all():
    """A job conditioned on `schedule` in a workflow with no schedule
    never runs again, and nothing would say so."""
    text = repo_file(".github", "workflows", "ci.yml").read_text()
    head = text[:text.index("\njobs:")]
    assert "schedule:" in head
    assert re.search(r"cron:", head)
