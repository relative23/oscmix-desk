# Security model

## Local control and cooperating writers

Desk and the matching upstream GTK mixer talk to the backend over one Unix
packet socket per interface, selected by USB id and device serial. There is
no UDP listener and no UDP fallback. The socket is `0660` inside the shared
`3770 root:audio` runtime directory `/run/oscmix-desk`. File permissions
decide who may connect; kernel peer credentials and the backend, ALSA bridge
and device association identify the connection.

A desk operation holds the backend's operation lease from its first write to
its last read-back; GTK edits wait for it. The desk also holds a device file
lock for configuration and profile-marker changes. A slow consumer, expired
lease, malformed delivery or disconnected backend cannot authorize later
writes, and nothing is replayed after a reconnect.

This coordinates cooperating local users; it does not isolate them. Any
account with access to the socket can write its own client. Root and accounts
with raw MIDI/USB access can bypass the clients. Physical controls are outside
the lease. Such access can change faders, routing and **phantom power**: the
desk reads `48v` but cannot set it, while upstream GTK and the protocol can.

Upstream's standalone UDP mode is not used. It listens on `127.0.0.1:7222`
without authenticating the sender; the desk and its launcher refuse to work
with it.

## What the service is allowed to do

`systemd/oscmix.service` runs as the user, sandboxed as far as a user
manager allows: `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome=read-only`, `PrivateTmp`, `LockPersonality`,
`MemoryDenyWriteExecute`, `SystemCallArchitectures=native`,
`SystemCallFilter=@system-service`, `RestrictAddressFamilies` limited to UNIX
and IP sockets, `UMask=0077`, `KeyringMode=private`, `RestrictNamespaces`,
`RestrictSUIDSGID`, `RestrictRealtime`, `ProtectKernelTunables` and
`ProtectControlGroups`. `tests/test_unit_file.py` holds the unit to this
list; capability and cgroup directives are left out because a user manager
refuses to start a unit with them.

Declared is not always applied. With
`kernel.apparmor_restrict_unprivileged_userns=1` (the Ubuntu default since
24.04) the user manager silently drops `ProtectSystem`, `ProtectHome` and
`PrivateTmp`; `NoNewPrivileges`, the seccomp filter,
`MemoryDenyWriteExecute`, `RestrictAddressFamilies` and `LockPersonality`
still apply. Check a machine with:

```sh
pid=$(systemctl --user show -p MainPID --value oscmix.service)
grep ' / / ' /proc/$pid/mountinfo      # "ro," means the mount sandbox applies
grep -E 'NoNewPrivs|Seccomp:' /proc/$pid/status
```

NixOS and Silverblue use the same user unit. OpenRC and runit run the session
as the explicitly registered ordinary user without a systemd sandbox.

The systemd resume hook is a short root operation: it queues a fixed system
unit that asks the user managers to reload already active desks after the
sleep services finish. It does not read routing configuration, does not start
an inactive desk and is stopped after 30 seconds.

## What the session writes

The session creates its device lock, and the backend its control socket and
owner lock, in `/run/oscmix-desk/`. Without root integration the runtime
directory falls back to `$XDG_RUNTIME_DIR/oscmix-desk/`, then to the
configuration directory, with a narrower coordination scope. A permission
failure on the selected directory is a refusal, never a reason to pick a
different one. `ReadWritePaths` names only the shared runtime directory.

The routing configuration is read-only to the service. The profile marker and
`--dump-config` output are written by the CLI, outside the service, to a
temporary file that is then renamed.

## Who can reach the lock

`/run/oscmix-desk` belongs to `root:audio` with mode 3770. Members of `audio`
can hold a lock there; nobody else can. A member can still hold a lock
indefinitely or plant a file at another member's lock path -- the latter is
refused with its reason, the former is a wait. That is the trust the group
grants, and it is the group that may drive the interface at all. The PIN and
REMEMBER policy decides whether a later desk operation overwrites a manual
change; it never saves that change into the configuration.

## Signalling other processes

The session may terminate a leftover `oscmix` backend of the same user for
the same device endpoint when no supervising session is alive. It opens a
pidfd, checks the endpoint owner, the backend/bridge identity and the missing
supervisor again, and signals through that handle. A process it cannot verify
is reported, never signalled, and it never falls back to a bare PID.

## Supply chain

`install.sh` builds the pinned upstream commit with the versioned patch series
in `patches/backend-series.json`, verifying every patch hash and recording the
resulting build identity. `OSCMIX_REF` must equal the pinned SHA; `--no-build`
verifies the installed series. Upstream publishes no signed tags, so the pinned
revision and patch hashes identify the inputs but do not prove them correct.

Releases carry checksums and a GitHub build attestation
([verification](RELEASE-ARTIFACTS.md)). The package repositories are signed
with the project key ([key](../packaging/keys/README.md)); private keys never
enter the repository, CI or the published site.

## Not in scope

Isolation from other authorized hardware users, remote access, and the audio
data itself. This project configures a mixer; it does not carry audio.
