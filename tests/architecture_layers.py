"""The import layering tests/test_architecture.py holds every module to."""

# Which modules a module may import. A module absent from a value list is
# forbidden, so adding a dependency is a deliberate edit here, not an
# accident in an import block.
ALLOWED_IMPORTS = {
    "constants": set(),
    "errors": set(),
    "log": set(),
    "osc": set(),
    # The ODK1 wire values, mirrored from control.h; nothing but struct.
    "protocol": set(),
    "arguments": {"constants"},
    "notify": {"log"},
    # `errors` since 0.6.9: two identical interfaces without
    # `[device] serial` are a configuration the user has to fix, and the
    # leaf that finds the interfaces is the one that can tell.
    # errors is itself a leaf, so no direction in the graph changes.
    "discovery": {"errors", "log"},
    # log is a leaf: one named logger, configured by the CLI entry point
    # before load_config runs. The section parsers have it for the
    # unknown-section warning -- a warning has to reach the
    # journal, and returning it up the call chain would be a second error
    # channel beside ConfigError for no benefit. The loader itself logs
    # nothing.
    # registers and devices are near-leaves like constants: the shape of
    # a register row, and the rows. devices sits on registers, since a
    # table is made of rows, and everything that asks which device a
    # config names reads devices.
    # `paths` since 0.8.1 for regular_file(): whether the file is there,
    # answered alike on every supported Python. paths sits on errors only.
    "autogain": {"devices", "errors", "model", "numeric", "osc", "registers"},
    # Pure unreadable mute/pair/complete-column ownership, below config/planning.
    "playback": {"devices", "errors", "model", "numeric", "registers"},
    # The manual's cross-channel DSP limits, below config and routing like playback.
    "limits": {"devices", "errors", "model", "numeric", "osc"},
    "config": {"autogain", "base", "constants", "deskconf", "devices", "errors", "limits",
               "mirrors", "model", "paths", "playback", "registers", "sections"},
    # The `[desk]` section's options, split out of config in 0.9.0.
    "deskconf": {"errors", "sections"},
    # `[desk] base`: the matrix beneath the routes, as cells; below config and reconcile.
    "base": {"constants", "errors", "model", "osc", "registers"},
    # `mirror =`: routes of one output declared again for another, at load time.
    "mirrors": {"errors", "model", "registers"},
    # link_messages/mix_messages moved down into reconcile: they are
    # pure message shapes, and keeping them here made reconcile sit
    # above routing while routing wanted to call it -- a cycle.
    # `errors` since 0.6.11, here and in verify: both have to tell a
    # receive port that cannot be bound from one that is held,
    # and the leaf is where that exception lives -- imported from there,
    # because `__init__` is the only module that re-exports.
    "routing": {"autogain", "backend", "constants", "errors", "log", "model", "observation",
                "osc", "peaksafe", "prechecks", "reconcile", "streams"},
    # What an apply reads and refuses before it writes, split out of routing in 0.9.0.
    "prechecks": {"autogain", "backend", "constants", "errors", "limits", "log", "model",
                  "observation", "osc", "reconcile"},
    # `[desk] peak-safe`: what a switch quiets around routing's own send order.
    "peaksafe": {"backend", "constants", "devices", "log", "model", "numeric", "osc",
                 "reconcile", "registers"},
    "streams": {"constants", "log", "model"},
    # `limits` since 0.9.0: a read-back warns when the device reports more
    # DSP load than it runs.
    "verify": {"autogain", "backend", "constants", "devices", "limits", "log", "model",
               "observation", "osc", "reconcile", "registers", "routing"},
    "desk": {"autogain", "backend", "connectcheck", "constants", "devices", "discovery",
             "errors", "journal", "locking", "log", "model", "osc", "paths", "reconcile",
             "routing", "seen", "verify"},
    # Beside the lock, below desk: what desk wrote, what it saw, its check at connect (0037).
    "journal": {"locking", "log", "osc"},
    "seen": {"journal", "log", "osc"},
    "connectcheck": {"autogain", "backend", "constants", "log", "model", "observation", "osc",
                     "reconcile", "seen"},
    "playstate": {"constants", "devices", "diagnostics", "discovery", "journal", "model", "osc",
                  "reconcile", "registers", "seen"},
    "observation": {"autogain", "numeric", "osc", "reconcile", "registers"},
    "pipewire": {"errors", "model"},
    "process": {"constants", "discovery", "hostservice", "log"},
    "hostservice": set(),
    # `locking` since 0.7.0: the unit takes the device lock itself around
    # its apply and its verifier, and the lock is a module of its own now
    # rather than a part of profiles. What the desk in effect is, session
    # asks reload.
    # `devices` since 0.7.0: a start says when the interface reports
    # another firmware than the one its register table was recorded on.
    # `watch` since 0.9.0: `[desk] enforce = watch` keeps a reader beside
    # the start's operation and asks supervise() for a reconcile.
    "session": {"constants", "desk", "devices", "discovery", "errors", "locking", "log",
                "model", "notices", "notify", "process", "reconcile", "reload", "watch"},
    # Below session, which starts it; beside verify, whose observation it
    # judges by. It only reads: a reconcile is session's and reload's.
    "watch": {"backend", "constants", "devices", "errors", "log", "model",
              "observation", "osc", "protocol", "reconcile", "registers"},
    # What a running session does with a desk it reads again, split out
    # of session in 0.7.0: below session, which starts it and hands it
    # the SIGHUP. `profiles` because a reload has to apply the same desk
    # a start does, and "the active profile, else routing.conf" is
    # answered in profiles.effective_config; profiles sits above verify
    # and imports nothing from here, so no cycle. `locking` for the lock
    # around every reconcile.
    # `marker` since 0.9.0: the watcher re-reads the desk only after the
    # marker, routing.conf or a profile changed.
    "reload": {"constants", "desk", "errors", "log", "marker", "model",
               "notices", "notify", "paths", "profiles", "reconcile"},
    # `process` since 0.6.3: an applied profile switch reloads the unit
    # so its own verifier cannot revert it; process already sits below
    # session and imports nothing above discovery.
    "cli": {"arguments", "constants", "errors", "hostservice", "log", "model", "notices",
            "outcome", "paths", "pipewire", "preview", "profiles", "reads", "session", "status"},
    # The three actions that read the device, split out of cli in 0.7.0.
    # `discovery` since 0.6.2: the snapshot header names the device's
    # serial and firmware, which are the leaf's to answer. `process` for
    # whose backend holds the port, `dump` for the device's state as a
    # config.
    # `autogain` since 0.9.0: a capture leaves the gains AutoSet owns to it.
    "reads": {"autogain", "constants", "desk", "devices", "discovery", "dump", "journal",
              "log", "model", "osc", "reconcile", "registers"},
    # Sits above verify because a switch has to report whether the
    # device confirmed it. Below cli because the outcome is a value, not
    # an exit code -- the mapping to one is the CLI's business.
    # `discovery` since 0.6.7: the device lock is keyed by the interface,
    # and the serial that names it is the leaf's to answer.
    # `process` since 0.6.9: a switch accepts the OSC port only from an
    # oscmix of this user that bridges the resolved interface, which is
    # the question the start's stale cleanup already asks through
    # process.socket_owner. process imports nothing above
    # discovery, so no cycle.
    "profiles": {"arguments", "backend", "config", "constants", "desk", "devices", "errors",
                 "hostservice", "log", "marker", "model", "notices", "outcome", "paths",
                 "process", "reconcile", "verify"},
    # The three things a switch is made of besides its order, split out of
    # profiles in 0.7.0. Each is a near-leaf: the lock knows its wait and
    # the journal, the marker knows what a profile name is, and an outcome
    # is a value that imports nothing.
    "locking": {"constants", "log"},
    "marker": {"errors", "log", "paths"},
    "outcome": set(),
    "launcher": {"config", "constants", "desktop", "diagnostics", "discovery", "hostservice",
                 "errors", "model", "paths"},
    "diagnostics": {"constants", "discovery", "errors", "hostservice", "locking", "model",
                    "paths", "process"},
    "desktop": {"diagnostics", "discovery"},
    "status": {"constants", "desktop", "diagnostics", "discovery", "errors", "hostservice",
               "marker", "model", "paths", "playstate", "profiles", "streams"},
    "preview": {"model", "reconcile"},
    # What config.py was until 0.7.0, by what each part is. `model` is the
    # desk as data and what nearly everything reads -- the reconciler, the
    # router, the verifier and the sink generator stopped depending on
    # the parser the day it moved out. `paths` is where a desk is looked
    # for, `sections` the parsers the register table drives, `notices`
    # what there is to say about a desk; `config` is the loader on top.
    "model": {"constants", "registers"},
    "paths": {"errors"},
    "sections": {"devices", "errors", "log", "mirrors", "model", "numeric", "registers"},
    "numeric": {"constants", "registers"},
    "notices": {"devices", "log", "model"},
    # A leaf: the shape of a row and of a device, and the questions asked
    # of a table somebody hands it.
    "registers": set(),
    # constants only for the fader range: the register table declares the
    # device's bounds, and writing -65.0/6.0 here as well would be the
    # same fact in two files -- which is how a validator and a register
    # table come to disagree.
    "devices": {"constants", "registers"},
    # The one place that opens a socket to the device. Its Traits name
    # the upstream behaviour the timing constants work around.
    # `errors` since 0.6.11: a receive port that cannot be bound for a
    # reason other than a holder is an exception every caller has to be
    # able to name, and errors is a leaf.
    # Shared read-only identity below every command; connection/lease ownership
    # above it. Diagnostics never imports this transport or a write command.
    "backend": {"constants", "diagnostics", "discovery", "errors", "model", "osc", "protocol"},
    # Pure: config + the message shapes + the register table. No
    # socket, no clock -- which is what lets it be tested against
    # recordings instead of hardware.
    # `osc` since 0.7.0, here and in whatever handles a register's value:
    # the leaf names what a value on this wire is, so that it is not an
    # `object` each reader casts past the type checker.
    "reconcile": {"autogain", "base", "constants", "devices", "model", "numeric", "osc",
                  "playback", "registers"},
    # The other direction, split out of reconcile in 0.7.0: what the device
    # reports, as a config and as its text. As pure as the reconciler,
    # whose message shapes and policy it reads.
    "dump": {"constants", "model", "numeric", "osc", "reconcile", "registers"},
    # The supported surface and nothing else since 0.7.0: what it imports
    # is what it re-exports, and the leaves it used to pull in for the
    # sake of their internals are reached through their own modules.
    "__init__": {"backend", "config", "constants", "desk", "errors", "launcher", "marker",
                 "model", "outcome", "paths", "pipewire", "profiles", "reconcile", "registers",
                 "verify"},

}
