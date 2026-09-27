# Runtime status and the upstream mixer

The 0.8.0 development runtime supports these commands in source and native
installations. Final qualification is tracked in the [release plan](plans/0.8.0-reliability-integration.md).

```sh
oscmix-session --status
oscmix-session --status --json
```

Status reads configuration, procfs, installed files and service-manager
metadata, and asks the GTK executable for its display-free protocol version.
It sends no OSC request, binds no socket, opens no audio stream,
starts no service and writes no files. It works while the interface is
disconnected or the upstream GUI is open. Installer preflight
and `oscmix-setup --check` still diagnose their installation tasks.

The displayed profile is the configuration selected for this invocation.
A stale profile marker is reported separately from the fallback main desk.
Neither a stored profile name nor an active/ready service establishes what
the device currently holds. `verification` is always `not-performed`.
Use `--diff` when you want a hardware read-back.

## JSON schema 2

Top-level fields are `schema_version`, `desk_version`, `read_only`,
`configuration_valid`, `verification`, `sections` and `note`. Consumers
should check `schema_version`, tolerate additional fields and use state
fields rather than parse explanatory text. Unknown values are `null`;
unknown/unavailable observations have an explicit state and a detail.

| Section | Meaning |
| --- | --- |
| `installation` | Running Python/package path, entry point, resolved backend file/hash, native metadata and separate core/GTK maintenance markers |
| `configuration` | Main and selected file, stored/effective profile, device selector and legacy port fields; `loaded`, `fallback` or `invalid` |
| `service` | Independent systemd or registered OpenRC/runit observation: `observed`, `unavailable` or `unknown`; never a verification result |
| `backend` | Exact interface/ALSA-client association: `ready`, `absent`, `conflict` or `unknown`; PID only when identified |
| `backend.running_file` | Kernel executable reference/hash for an identified backend; comparison with the resolved file, or an explicit read failure |
| `playback` | `observed`, `idle`, `unobserved`, `unsupported`, `unknown` or `not-applicable`, with available mode fields |
| `backend.endpoint` | Selected per-device Unix control socket path; status does not connect or reserve it |
| `desktop` | Resolved GTK binary, supported control protocol and prerequisite/mismatch problem, or `null` when none |

Schema 2 removes schema 1's `receive_port` section: desk and GTK no longer
compete for a UDP reply socket. `configuration.send_port` and `receive_port`
remain accepted configuration data for compatibility; they do not select
the coordinated endpoint. `desktop.connection` now describes ODK1 rather
than saved UDP settings. Native host registration can supply `service.manager`.

`ready` means that procfs associates the listening control endpoint with the
selected interface, backend and exclusive ALSA bridge. This is a momentary
inspection, not a protocol handshake or reservation. File hashes
and package metadata describe software identity, not successful hardware
tests. A service observation and a manual backend observation remain separate;
status does not infer supervision from a process merely existing.

Exit 0 means the status report was produced with a valid effective config.
An absent device or optional GUI is still a useful status report. Exit 2
indicates invalid configuration; `--status --json` still prints its diagnosis.
Conflicting CLI actions are usage errors. `--json` requires `--status`.

## Opening the existing GTK mixer

Run `oscmix-launch` or use its app-menu entry. The launcher checks the
companion's control protocol and installation maintenance state before
considering a service start. It selects the exact backend socket, process
and device serial; GTK checks the connected peer before requesting state.
The old UDP GSettings no longer select a connection. An unmodified upstream
or old companion must be upgraded together with the backend and bridge.

A matching manual backend is reused. If none is running, start the reviewed
desk with `oscmix-session` in a terminal, or explicitly enable automatic
operation using the installation guide. The launcher starts only an enabled
default service whose user/config paths agree. Custom service commands or
environment files need an explicit start. For registered OpenRC/runit
installations, the launcher waits for the already active supervisor; it does
not request administrator activation. Missing hardware, an unrelated
endpoint or incomplete maintenance produces a notification and nonzero exit.

## Read-back while using the mixer

GTK can remain open during `--diff`, a profile switch or selective reload.
Each consumer receives its own subscription. The backend coordinates refresh
windows and bounds waiting; unavailable or damaged feedback is reported as
failure rather than permission to write blindly.

A desk write operation holds one lease through application, verification and
repair. GTK keeps receiving observations and meters but disables editing
during that lease, then waits for fresh device observations. Edits rejected
while busy or disconnected are never replayed later. Closing GTK is necessary
for software replacement, not ordinary desk read-back.

To restore PIN values while retaining REMEMBER settings, use
`systemctl --user reload oscmix.service`, or `oscmix-service reload` for a
registered OpenRC/runit service. A manually supervised session accepts SIGHUP
on its session PID. An explicit `--profile NAME` or `--no-profile` reapplies
declared starting values and verifies them strictly. Later reconcile retains
every REMEMBER value even without feedback; link or stereo-partner dependencies
can still refuse a write that would change it indirectly.

A disconnected or replaced backend invalidates the connection. Inspect the
reported sent/pending paths and use a new explicit operation after recovery;
there is no automatic write replay or hardware rollback. Shared replies do
not make the playback matrix readable. See the
[ownership contract](decisions/0030-backend-owned-control.md) and
[measured capability limits](evidence/0.8.0/backend-capabilities.json).
