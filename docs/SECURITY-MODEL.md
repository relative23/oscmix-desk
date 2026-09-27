# Security model

The trust boundaries of the 0.8.0 development implementation. Its final
hardware, host and publication qualification remains open in the
[release plan](plans/0.8.0-reliability-integration.md).

## Local control and cooperating writers

Desk and the matching upstream GTK companion use one backend-owned Unix
packet socket, selected by USB id and device serial. Coordinated mode opens
no UDP command or reply listener and has no automatic UDP fallback. The
socket is `0660` inside the shared `3770 root:audio` runtime directory.
Path permissions authorize local access; kernel peer credentials and the
exact backend/ALSA-bridge/device association identify the connection.
The backend epoch and connection lifetime bound its observations.

The backend serializes GTK edits with the desk's whole-operation lease.
The desk separately holds its device file lock for configuration and
profile-marker decisions. A slow consumer, expired lease, malformed delivery
or disconnected backend cannot supply authority for later writes. No queued
GUI edits or failed desk writes are replayed after release or reconnection.
See [the ownership contract](decisions/0030-backend-owned-control.md).

This is coordination between local device users, not isolation from them.
An account allowed to access the socket may implement a client of its own;
root and accounts with raw MIDI/USB access can bypass these clients or deny
service. Physical controls remain outside the lease. Such access can change
faders, routing and **phantom power**. Desk models `48v` as readable but
provides no writable configuration domain; the upstream GTK and protocol
still expose it. The write sweep also excludes reference-level changes
(ADR 0016).

Upstream's standalone UDP mode remains available only as a separate,
uncoordinated mode that desk and its launcher refuse. That mode normally
listens on `127.0.0.1:7222` without authenticating the sender; any local
process able to reach it can issue mixer commands. Loopback alone is not
user authentication, and the coordinated socket adds no protection to a
separately started UDP backend.

## What the service is allowed to do

The systemd resume helper is a separate, short root operation. Its root-owned
sleep hook queues a fixed system unit; after the sleep services finish, that
unit asks user managers to reload already active desks. Mixer code still runs
as its ordinary user. The helper does not read a user's routing configuration
or start an inactive desk, and is terminated after 30 seconds if it stalls.

`systemd/oscmix.service` is sandboxed as far as an unprivileged **user**
unit can be. The hardening that a user manager cannot apply is documented
in `tests/test_unit_file.py` along with why -- capability-dropping and
cgroup-based directives fail with `218/CAPABILITIES`, which stops the
audio rather than securing it.

Declared: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`,
`PrivateTmp`, `LockPersonality`, `MemoryDenyWriteExecute`,
`SystemCallArchitectures=native`, `SystemCallFilter=@system-service`,
`RestrictAddressFamilies` limited to UNIX and IP sockets, `UMask=0077`,
`KeyringMode=private`, `RestrictNamespaces`, `RestrictSUIDSGID`,
`RestrictRealtime`, `ProtectKernelTunables` and `ProtectControlGroups`.
The unit file is the list of record; `tests/test_unit_file.py` holds it
against what a user manager was measured to accept.

### Declared is not the same as applied

On Ubuntu with `kernel.apparmor_restrict_unprivileged_userns=1` -- the
default since 24.04 -- the user manager may create a user namespace but
not a mount namespace inside it, and it drops `ProtectSystem`,
`ProtectHome` and `PrivateTmp` **without a word in the journal**. Measured
on the desk in 0.6.9: the running unit shares the host's mount namespace,
`/` is mounted read-write inside it, and it can create files in the home
directory and `/var/tmp`. What does apply there is everything that does
not need a mount: `NoNewPrivileges` and the seccomp filter (the unit's
`/proc/<pid>/status` shows `NoNewPrivs: 1` and `Seccomp: 2`), and with
them `MemoryDenyWriteExecute`, `RestrictAddressFamilies` and
`LockPersonality`.

Where a manager can apply the mount sandbox -- measured by running the
same directives under the system manager -- `/` is read-only and the
home directory refuses writes. Check a given machine with:

```sh
pid=$(systemctl --user show -p MainPID --value oscmix.service)
grep ' / / ' /proc/$pid/mountinfo      # "ro," means the sandbox applies
grep -E 'NoNewPrivs|Seccomp:' /proc/$pid/status
```

The directives stay in the unit where the host permits them. The historical
Ubuntu result does not qualify a new distribution or manager. NixOS and
Silverblue use the common user-unit payload; their effective restrictions
must be checked on the actual booted generation/deployment. OpenRC and runit
instead run the session as the explicitly registered ordinary user; they do
not claim systemd's namespace or seccomp sandbox. Their root-owned adapter
and persistent maintenance state control activation and update recovery.

### What the session writes

The session creates its device lock, and the backend creates the control
socket and lifetime owner lock beside it, under `/run/oscmix-desk/`.
The lock holds no configuration data; ownership lives on the open file
description. Runtime-path selection falls back to the absolute
`$XDG_RUNTIME_DIR/oscmix-desk/`, then the configuration directory when root
integration is absent (ADR 0023, ADR 0030). That fallback has a narrower
coordination scope. A permission failure on the selected shared path is a
refusal, not permission to pick a different owner. `ReadWritePaths` names
the shared runtime directory and nothing else.

That line is load-bearing. Under a sandbox that is actually applied, `/run`
is read-only without it and no lock file can be created, so every start
would be refused -- measured under the system manager in 0.6.9, where the
shipped 0.6.8 unit got "Read-only file system" and the one with
`ReadWritePaths=-/run/oscmix-desk` took the lock. The dash lets the unit
start on a machine without the directory.

The routing config stays read-only to the service. The profile marker and
`--dump-config` output are written by the CLI a user runs, outside the
service, which is the outcome the questions below were written to aim
for:

- **Which directory, and does the session need to write it, or only the
  CLI?** If only the interactive path writes, the service stays at the
  narrowest `ReadWritePaths` and the write happens outside it.
- **Does the routing config itself become writable?** It should not. A
  config the service can rewrite is no longer a reviewable source of
  truth, which is the project's premise.
- **What happens on a partial write?** A truncated file is an unbootable
  state. Write to a temporary file and rename.

`tests/test_unit_file.py` asserts `ReadWritePaths` exactly, so widening
it cannot happen quietly.

### Who can reach the lock

`/run/oscmix-desk` is 3770 root:audio. Members of `audio` can create,
open and hold a lock there; nobody else can reach one. Inside that group a
member can still hold a lock for ever or plant a file at another member's
lock path -- the second is refused with its reason rather than followed or
blocked on, the first is a wait. That is the trust the group grants, and
it is the same group that may drive the interface at all.

The file lock serializes desk writers and profile-marker updates. The
backend lease additionally coordinates the matching GTK companion. Neither
lock is an access-control boundary against another authorized device user.
The PIN/REMEMBER policy decides whether a later desk operation may overwrite
a manual adjustment; it does not save that adjustment into the config.

## Signalling other processes

`_cleanup_stale_backend` may terminate a leftover coordinated `oscmix`.
It considers only the calling user's identified backend at the selected
device endpoint, without a live supervising session. After opening a pidfd
it checks endpoint ownership, exact backend/bridge identity and supervision
again, then signals through that handle. Checking only
before opening it leaves a PID reuse window. A process it cannot verify
is reported, never signalled; an unavailable pidfd never falls back to
signalling a bare PID.

## The supply chain

`install.sh` fetches the pinned upstream commit and builds the exact
versioned backend/bridge/GTK patch series in `patches/backend-series.json`.
Preparation exports Git objects, verifies patch hashes and records the
resulting source/build identity. Uncommitted upstream edits are not build
inputs. `OSCMIX_REF` must equal that full pinned SHA; a moving branch is no
longer a supported override. `--no-build` verifies the installed series
instead of accepting a familiar executable name or the upstream SHA alone.
Source, native-package and Nix builds share this version contract.

There is no upstream signature verification: upstream publishes no signed tags.
That is a supply-chain limitation. A fixed upstream revision and patch
hashes identify inputs but do not prove their correctness. Hardware records
identify the actual patched build; old unpatched or earlier-series measurements
do not qualify a changed backend.

The source release workflow produces checksums and a GitHub build
attestation for this project's archive and manifest. The
[verification instructions](RELEASE-ARTIFACTS.md) constrain the repository,
workflow and release tag. That attestation identifies our build; it does
not authenticate upstream's unsigned history or prove the locally
compiled C binaries reproducible.

For 0.8.0, the repository publisher separately verifies each attested native build,
its actual indexed download identity and the final GPG-signed files. It attests the
complete signed archive and each channel manifest, using an explicitly selected
`main` workflow revision. Private GPG keys stay local. Recovery of an expired or
revoked historical snapshot requires that independent publication proof and the
exact source/signer workflow commit before reusing any file; live client checks
remain strict. The publisher also binds updates to a selected live predecessor
and preserves immutable downloads. The workflow is implemented but its real
publication/recovery and HTTPS gates remain open; see
[the operating procedure](PACKAGE-REPOSITORIES.md#publishing-and-recovering-a-channel).

## Not in scope

Isolation from other authorized hardware users, remote access, and anything
about the audio data itself. This project configures a mixer; it does not
carry audio.
