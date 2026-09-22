PYTHON ?= python3
SCRIPTS = bin/oscmix-session bin/oscmix-launch
PACKAGE = src/oscmix_desk
SHELL_SCRIPTS = install.sh uninstall.sh scripts/verify-unit.sh scripts/install-payload.sh scripts/stage-install.sh packaging/oscmix-desk.install systemd/system-sleep/oscmix
# Repeats for the flakiness gate. The suite binds real UDP sockets and
# runs background threads, so a single green run proves little.
REPEAT ?= 5
# Restart cycles for `make soak`. The scheduled workflow runs 200.
SOAK_CYCLES ?= 50

.PHONY: all check test lint typecheck deadcode coverage mutation flake soak \
	verify-hardware install uninstall clean

all: check

# The fast gates, in the order that fails fastest. CI adds `coverage`
# (with its ratchet), `flake`, the unit-file verification and the build
# of the pinned oscmix; the release checklist runs those by hand.
check: lint typecheck deadcode test

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m py_compile $(SCRIPTS)
	$(PYTHON) -m ruff check .
	shellcheck $(SHELL_SCRIPTS)

# --strict on the package: it is the whole runtime, and nothing in it has
# an excuse for an untyped boundary. The bin/ shims stay on the relaxed
# setting because they exist to bootstrap sys.path before any import.
typecheck:
	$(PYTHON) -m mypy --strict $(PACKAGE)
	$(PYTHON) -m mypy --ignore-missing-imports --scripts-are-modules $(SCRIPTS)

# Runtime and tooling only. A monkeypatch stub must accept the signature
# it replaces whether or not it uses every parameter, so "unused" in a
# test says nothing; in the runtime it is a defect.
#
# 60 % rather than 80: at 80 vulture reports only what it is nearly
# certain of, and three functions with no caller at all sat below that
# line through five releases. What is legitimately unused by the runtime
# -- test contracts, documented data -- is listed with its reason in
# quality/vulture-allowlist.py, which vulture reads as usage.
deadcode:
	$(PYTHON) -m vulture $(PACKAGE) $(SCRIPTS) scripts/ packaging/oscmix-setup packaging/package-guard quality/vulture-allowlist.py --min-confidence 60

# parallel mode plus a combine step: the integration tests measure the
# session subprocess too, and each process writes its own data file.
coverage:
	$(PYTHON) -m coverage erase
	$(PYTHON) -m coverage run -m pytest -q
	$(PYTHON) -m coverage combine
	$(PYTHON) -m coverage report

# The only check that measures audio rather than messages. Needs a
# connected interface, a running backend and a quiet bus; exits 77 and
# says why when any of those is missing, so it is safe to wire into CI.
verify-hardware:
	$(PYTHON) scripts/verify-hardware.py --evidence hardware-evidence.json

# Answers what coverage cannot: whether the assertions catch a wrong
# value or merely execute the line. Slow (~15 min), so it is not part of
# `check`; the baseline in quality/ turns the result into a ratchet.
mutation:
	$(PYTHON) -m mutmut run --max-children 4
	$(PYTHON) scripts/mutation-policy.py

# Runs the suite repeatedly: races in the UDP/threading fakes only show up
# across runs, and one such race was shipped before this gate existed.
flake:
	@for i in $$(seq 1 $(REPEAT)); do \
		echo "--- run $$i/$(REPEAT)"; \
		$(PYTHON) -m pytest -q || exit 1; \
	done

# A convenience, not the gate. The gate is .github/workflows/soak.yml,
# which runs on a schedule -- a soak that has to be invoked by hand is a
# soak that does not run. This target exists to reproduce a scheduled
# failure locally. ~1.2 s per cycle.
soak:
	OSCMIX_SOAK_CYCLES=$(SOAK_CYCLES) $(PYTHON) -m pytest tests/test_soak.py -q

install:
	./install.sh

uninstall:
	./uninstall.sh

clean:
	rm -rf build tests/__pycache__ .pytest_cache .ruff_cache .mypy_cache \
		.coverage htmlcov mutants hardware-evidence.json
