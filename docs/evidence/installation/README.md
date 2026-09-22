# Installation qualification in development

These results describe the installation milestone following 0.7.1.
They are **not published native packages or new hardware qualification**.
The [machine-readable record](qualification.json) includes source hashes,
resolved container identities, log/artifact hashes and the scope of each run.

| Environment, x86_64 | Source build/install and simulated lifecycle | Native package transitions and repeated build | 0.7.0 source migration and return |
| --- | --- | --- | --- |
| Debian 13 | Passed | Not qualified | Not qualified |
| Ubuntu 24.04 | Passed | DEB passed, byte-identical repeat | Passed; also real user manager in VM |
| Ubuntu 26.04 | Passed | Not a publication target in this run | Not qualified |
| Fedora 44 | Passed | RPM passed, byte-identical repeat | Passed with simulated user bus |
| openSUSE Leap 16 | Passed | RPM passed, byte-identical repeat | Passed with simulated user bus |
| Arch | Passed | Arch package passed, byte-identical repeat | Passed with simulated user bus |
| Alpine 3.22, musl | Passed, manual mode | Not qualified | Not qualified |

The actual package managers exercise fresh install, refusal with an active
mixer process, upgrade, recovery after an interrupted transaction, downgrade
and removal. Installed payload hashes match the package manifest. Containers
that exclude optional documentation by policy record those exclusions; no
missing runtime file is excused. Config/profile/active-marker data survives.

The migration invokes the actual 0.7.0 installer and the installed native
`oscmix-setup` as an ordinary user. It verifies restored file contents and
permissions, version resolution and an installed dry run. Container service
calls are simulated. The Ubuntu VM additionally checks the actual vendor
unit, readiness, explicit opt-in, migration/recovery, a pinned-setting SIGHUP
and the maintenance fence across a real reboot. Its backend is simulated;
the VM has no audio/USB passthrough.

The combined source tree passes 1,678 tests and its static checks. The
distribution workflow was syntax-checked with actionlint 1.7.12, and the
same container entry point passes locally. GitHub release attestations can
only be generated when the workflow runs on the eventual release commit;
these local results do not substitute for that provenance.

No new GUI, physical multi-device check, higher-rate measurement, aarch64
binary, additional init adapter or immutable-system package is established
by this matrix. Ubuntu's actual user service also showed why unit-file
settings alone are insufficient proof: AppArmor/user-manager restrictions
left the mount namespace shared despite configured mount protections.
