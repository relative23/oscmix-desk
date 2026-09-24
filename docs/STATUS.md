# Runtime status and the upstream mixer

Available from 0.7.3, in source and native installations:

```sh
oscmix-session --status
oscmix-session --status --json
```

Status reads configuration, procfs, installed files, GSettings and systemd
metadata. It sends no OSC request, binds no socket, opens no audio stream,
starts no service and writes no files. It works while the interface is
disconnected or the upstream GUI owns the reply port. Installer preflight
and `oscmix-setup --check` still diagnose their installation tasks.

The displayed profile is the configuration selected for this invocation.
A stale profile marker is reported separately from the fallback main desk.
Neither a stored profile name nor an active/ready service establishes what
the device currently holds. `verification` is always `not-performed`.
Use `--diff` when you want a hardware read-back.

## JSON schema 1

Top-level fields are `schema_version`, `desk_version`, `read_only`,
`configuration_valid`, `verification`, `sections` and `note`. Consumers
should check `schema_version`, tolerate additional fields and use state
fields rather than parse explanatory text. Unknown values are `null`;
unknown/unavailable observations have an explicit state and a detail.

| Section | Meaning |
| --- | --- |
| `installation` | Running Python/package path, entry point, resolved backend file/hash, native metadata and separate core/GTK maintenance markers |
| `configuration` | Main and selected file, stored/effective profile, device selector, ports; `loaded`, `fallback` or `invalid` |
| `service` | Independent systemd observation: `observed`, `unavailable` or `unknown`; never a verification result |
| `backend` | Exact interface/ALSA-client association: `ready`, `absent`, `conflict` or `unknown`; PID only when identified |
| `backend.running_file` | Kernel executable reference/hash for an identified backend; comparison with the resolved file, or an explicit read failure |
| `playback` | `observed`, `idle`, `unobserved`, `unsupported`, `unknown` or `not-applicable`, with available mode fields |
| `receive_port` | `free`, `occupied` or `unknown`; port, known owner PID and next action |
| `desktop` | Resolved GTK binary, saved connection settings and prerequisite/mismatch problem, or `null` when none |

`ready` means that the observed backend belongs to the selected interface
and targets the expected reply endpoint. `free` means no listener was
observed. These are momentary inspections, not reservations. File hashes
and package metadata describe software identity, not successful hardware
tests. A service observation and a manual backend observation remain separate;
status does not infer supervision from a process merely existing.

Exit 0 means the status report was produced with a valid effective config.
An absent device or optional GUI is still a useful status report. Exit 2
indicates invalid configuration; `--status --json` still prints its diagnosis.
Conflicting CLI actions are usage errors. `--json` requires `--status`.

## Opening the existing GTK mixer

Run `oscmix-launch` or use its app-menu entry. The launcher checks GTK's
executable and schema before considering a service start. GTK's persistent
GSettings are independent of `routing.conf`; if hosts or ports disagree,
the diagnostic names explicit `gsettings set` commands. Review those
commands before using them. The launcher does not silently rewrite settings.

A matching manual backend is reused. If none is running, start the reviewed
desk with `oscmix-session` in a terminal, or explicitly enable automatic
operation using the installation guide. The launcher starts only an enabled
default service whose user/config paths agree. Custom service commands or
environment files need an explicit start. Missing hardware, an unrelated
listener and a busy reply port produce a notification and a nonzero exit.

## Read-back while using the mixer

The pinned backend has one reply destination. GTK and desk cannot both own
its receive socket. GTK also sends writes outside the desk lock.

1. Close the upstream GUI and let an existing desk read-back finish.
2. Inspect `oscmix-session --status`, then run `oscmix-session --diff`.
3. If you want to restore PIN settings while keeping REMEMBER adjustments,
   request the service's selective reconciliation with
   `systemctl --user reload oscmix.service`. A manually supervised session
   accepts SIGHUP on its session PID. Wait for that operation to finish.
4. Open `oscmix-launch` again. If the reply port is still busy, wait and retry.

A profile switch made while GTK owns the receiver can apply settings but
cannot confirm them. Closing GTK does not replay that switch automatically.
Use `--diff` to inspect the result; explicitly switching again reapplies the
profile's initial values, including declared REMEMBER settings. In contrast,
a selective reconcile with no receiver writes nothing, because it cannot
know which REMEMBER adjustments to retain.

The [reply-distribution decision](decisions/0028-mixer-replies-and-writer-coordination.md)
separates a future shared read-back path from write coordination.
