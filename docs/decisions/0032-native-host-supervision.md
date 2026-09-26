# 0032: OpenRC and runit supervise the same unprivileged session

Status: implementation in progress; VM qualification is required before release.

The systemd user service remains the default on its existing hosts. Alpine
and Void use their native supervisor with one explicit, root-owned registration
of the audio user, installed command and configuration file. Runtime selection
comes from that registration, not an OS-name guess. A second registration is
refused; the runtime's device/serial selection and shared lock still decide
which interface is controlled.

The existing per-user source payload is installed first. A separate administrator
step installs small OpenRC/runit adapters and a root-owned launcher. It never
starts the mixer. Explicit enable allows automatic hardware operation. OpenRC's
`command_user` and runit's `chpst` drop privilege before the launcher or any
user-installed Python/C code executes. The launcher clears inherited environment
settings and execs the ordinary `oscmix-session --config ...`; it owns no child
process or additional transport. Root is used for manager configuration, shared
`root:audio` mode 3770 lock-directory provisioning and logging only.

The native manager restarts a disconnected/crashed session with a minimum
five-second delay. A session without hardware retains the common bounded device
wait, then exits; supervision tries again. This supplies boot and hotplug
behavior without a second discovery daemon or udev command runner. Reload sends
SIGHUP to the desk process alone. The runtime resolves that process through the
native supervisor and checks its UID, exact command, configuration and manager
context. Every supported reload uses the same pidfd, revalidation and readiness
check. A matching Python argv can appear before its SIGHUP handler is installed;
signalling that child would terminate it. The pinned child must show a caught
SIGHUP in `/proc/PID/status` within five seconds. Missing identity/status, a
replacement child or an expired deadline refuses the request without signalling.
The check never signals the ALSA bridge or backend. OpenRC's root-owned PID file and root supervisor
command identify the parent without reading its protected `/proc/.../exe`
link. The final child must still belong to the registered audio user and match
the full command/context. Status only reads identity and manager metadata.
Runit's `supervise/pid` is read through a root-owned traversable directory;
its control and liveness FIFOs remain root-only. The child's parent must be the
root `runsv` for this service. The runtime does not run `sv status`, which needs
write access to runit's liveness FIFO even for a status request.
An enabled but administratively stopped system service requires an explicit
administrator start; the GTK launcher does not obtain privilege implicitly.

The administrator adapter installs a byte-identical copy of the stdlib-only
`hostservice.py` leaf and executes it as the registered user. It does not import
user-controlled code as root or duplicate the runtime's signal decision.
OpenRC's reload action and runit's `control/h` call that same adapter. Runit's
hook must exit successfully even after refusal: a nonzero hook exit makes
`runsv` fall back to a bare SIGHUP. It logs refusal instead. Consequently,
`sv hup` acknowledges queueing only; `oscmix-service reload` is the synchronous
interface that reports readiness/identity failure to its caller. The real-VM
[startup regression record](../evidence/0.8.0/native-reload-development.json)
covers all three entry points with the child held before handler installation,
and successful reload of that same child after startup resumes.

`oscmix-service maintenance-begin` records active/enabled state in a persistent
root-owned fence before stopping. Source installation must observe a confirmed
stopped fence for this registered home. Boot/start and direct CLI activation
refuse incomplete maintenance. Runit also keeps a persistent `down` file across
reboots. Finish checks the installed Python command and exact core/GTK build
pair as the audio user, then restores the previous activation state. An interrupted
operation retains its fence and can be resumed. Configuration, profiles and the
active marker remain user-owned. Removal of service integration preserves these
files and never removes a shared lock inode underneath another process.
`update`, invoked from the selected checkout while fenced, replaces the root
adapters too. Its incomplete-install journal refuses activation after a partial
replacement and allows the same operation to be rerun before maintenance ends.

The adapters claim no systemd sandbox on OpenRC/runit. They use ordinary UID/group
isolation and the common authenticated backend protocol. Suspend integration must
use the host's actual supplied resume mechanism and target this registered service.
The installer adds hooks in existing zzz/elogind hook directories. They call the
bounded `resume` action, which only reloads an enabled active desk outside all
maintenance fences. It never enables or starts an inactive service. A VM
lifecycle check, not the mere presence of a shell hook, establishes support.

References: [OpenRC supervision](https://github.com/OpenRC/openrc/blob/master/supervise-daemon-guide.md),
[runit service control](https://smarden.org/runit/sv.8),
[chpst user/group handling](https://smarden.org/runit/chpst.8),
[Void's zzz hooks](https://github.com/void-linux/void-runit/blob/master/zzz),
[elogind hook contract](https://github.com/elogind/elogind/blob/main/man/loginctl.xml).
