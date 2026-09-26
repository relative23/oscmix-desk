# 0034 — RPM composition cannot own live-host maintenance

**Status: implemented for 0.8.0; final-candidate qualification remains open.**

Fedora Silverblue layers our ordinary Fedora RPM payload into a new immutable
deployment. Its runtime uses the same systemd user unit, udev rules, shared locks
and explicit `oscmix-setup` opt-in as mutable Fedora. A second service adapter
or a separate runtime installation layout would duplicate those contracts.

The first actual Silverblue installation failed in the package's `%prein`:
rpm-ostree gave that hook a read-only `/var`, so it could not create the live
package-maintenance fence. This is a different execution context from DNF's
ordinary package transaction. The sandbox also has its own process namespace
and transient `/run`; a successful process scan there would not prove that the
host's mixers had stopped.

RPM hook invocations now explicitly identify themselves to the common package
guard. Only when that flag, `/run/ostree-booted` and `SYSTEMD_OFFLINE=1` are all
present does it defer maintenance. rpm-ostree supplies the latter two in its
script sandbox. Normal operator calls on a booted Silverblue host still check
the real processes and manage the existing persistent fence. Other native
package managers retain the original hook behavior.

Before an upgrade, rollback or removal, the operator stops every mixer and runs
the common guard on the host. Composition leaves that fence intact. After the
deployment has been booted and checked, the operator explicitly finishes
maintenance and may start the enabled service. A failed or interrupted
composition does not authorize clearing the fence. Removal saves the same
guard in root-owned persistent storage for completion after its package is gone.

This keeps one payload, one runtime and one maintenance rule. It introduces no
automatic updater or root mixer process. User configuration, profiles and the
active marker remain writable state outside the immutable deployment; a code
rollback cannot undo them or hardware state. An administrator can bypass the
documented deployment procedure, as with other privileged installation paths.

The [initial VM evidence](../evidence/0.8.0/silverblue-development.json) covers
the actual deployment lifecycle and preserved state with SELinux enforcing.
The ordinary Fedora DNF install/removal path also passes its transaction checks.
These development results do not close hardware, desktop, suspend, repository
authentication or final-candidate qualification.

See the [Silverblue operating instructions](../SILVERBLUE.md) and the
[rpm-ostree script implementation](https://github.com/coreos/rpm-ostree/blob/v2026.1/src/libpriv/rpmostree-scripts.cxx).
