# Contributing

## Development setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
make check PYTHON=.venv/bin/python
```

The runtime uses only the Python standard library (3.9 or newer). The test
suite needs no audio hardware; tests that need a device or a desktop session
skip or run in their own qualification scripts.

## Checks

| Command | What it runs |
| --- | --- |
| `make check` | ruff, `mypy --strict`, vulture and the test suite |
| `make coverage` | the suite with branch coverage; fails below `fail_under` in `pyproject.toml` |
| `make flake REPEAT=5` | the full suite repeatedly, for timing-dependent failures |
| `make soak SOAK_CYCLES=200` | repeated start/apply/stop cycles |
| `make mutation` | the suite in a forked child, then mutmut and `scripts/mutation-policy.py` |

Remove `mutants/` before a mutation run that should count as a fresh
measurement; the score floor is in `quality/mutation-baseline.json`.

Distribution builds run in Docker from a clean commit, as in CI:

```sh
python3 scripts/qualify-distribution.py --target alpine322 --output build/alpine-source
python3 scripts/qualify-distribution.py --target ubuntu2404 --native --development \
  --output build/ubuntu-packages
```

## Hardware verification

With a Fireface UCX II attached, a running backend and a quiet bus:

```sh
python3 scripts/verify-hardware.py --sink <stereo-sink> --evidence hardware-evidence.json
```

Name the sink explicitly. The artifact's `sink_channels` must map every used
sink to `["FL", "FR"]`, and the artifact's `firmware` must name the USB
revision and DSP version. `complete` must be true, `unmeasured` empty and
every entry in `routes` must have `ok: true` with its playback pair and sink.
When the register model changes, also run `scripts/sweep-writes.py` and keep
its result with the release.

## Releases

1. Update `__version__` in `src/oscmix_desk/constants.py` and add a dated
   section to `CHANGELOG.md`.
2. Run the checks above on the release commit, on every supported Python
   version, plus `make check` once with the interface switched off.
3. Record the hardware verification, the write sweep and the service
   lifecycle for the same runtime and backend build.
4. Create the draft release and attach the evidence files (`release-notes.md`,
   `software-qualification.json`, `lifecycle.json`, `hardware-evidence.json`,
   `write-sweep-ucx2.json`) before pushing the tag. The release workflow
   checks them against the tagged sources, builds and attests the artifacts
   and attaches everything to the draft.
5. Publish the signed package repositories with the publication workflow,
   verify the published downloads and repositories, then publish the draft.

See [release artifacts](docs/RELEASE-ARTIFACTS.md) for how users verify a
download.
