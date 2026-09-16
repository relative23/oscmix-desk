# 0017 -- The OSC port is the trust boundary, and it has none

**Status:** accepted (0.5.0)

## Decision

This project states plainly what it protects and what it does not:

- **It does not authenticate anything.** oscmix listens on
  `127.0.0.1:7222` and applies whatever arrives. Any process running as
  this user, and any process that can reach loopback, can set any
  register on the interface. There is no token, no socket permission,
  no peer check.
- **It keeps the backend on loopback by construction**, not by
  configuration. `session.py` passes `udp!127.0.0.1!<port>` literally
  and a `routing.conf` carries ports only, no host. A config file
  cannot put the mixer on the network.
- **The unit is hardened as far as a user manager allows**, which was
  measured rather than assumed. See below.

## What the port can actually do

Everything the device exposes, including the one register this project
deliberately withholds from configs. `/input/1/48v` is not settable
from a `routing.conf` -- it has no value domain, and the reason is in
`registers.py`: an off-by-one in phantom power is a damaged ribbon
microphone. That guard is in **this** project's config parser. It is
not on the port.

So a local process can switch phantom power on an input this project
refuses to name. That is worth writing down rather than leaving as an
unstated gap, and it is not fixable here: the port is oscmix's.

## What that is weighed against

The threat is a hostile or careless process already running as this
user. Such a process can also read the user's files, start binaries and
talk to the session bus. On a single-seat workstation the mixer is not
the weakest thing it can reach, and adding a shared secret to a
protocol whose other client is a GTK app the user did not write would
buy little.

Where it *would* matter is a multi-user machine, or one running
untrusted local code. This project does not defend that case, and says
so here rather than implying it does.

## Integrity of what gets built

`install.sh` fetches oscmix over HTTPS and compiles it. There is no
signature, because upstream publishes no signed tags (ADR 0008), and no
separate source hash is added, because **a git commit SHA already is
one**: the checkout is verified to have landed on exactly the pinned
40-character SHA, and that SHA covers the tree.

What the pin does not give is *authenticity* -- an assurance that the
commit is the maintainer's intent rather than an injected one. Nothing
available closes that, and inventing a second hash of the same content
would look like it did.

## The hardening, measured

`systemd-analyze security --user` scored the unit **8.3 EXPOSED**. Each
candidate directive was started against a probe unit and then against
the real service. The unit now scores **5.4 MEDIUM**, the routing
verifies against the device, and a tone still lands on every configured
output.

Three directives had been listed as impossible in a user unit since
0.2.0 and are not: `ProtectKernelTunables`, `ProtectControlGroups` and
`RestrictSUIDSGID` all start. They were assumptions that had never been
run.

Four the user manager really does refuse: `ProtectKernelModules`,
`ProtectKernelLogs`, `ProtectClock`, `CapabilityBoundingSet`.

And three it accepts that this service still must not have, which is the
half a probe unit cannot tell you:

| directive | why not |
|---|---|
| `PrivateNetwork` | the mixer GUI reaches the backend over 127.0.0.1 |
| `ProcSubset` | discovery reads `/proc/asound/seq/clients` |
| `PrivateUsers` | untested against ALSA device access |

`IPAddressAllow`/`IPAddressDeny` would be the natural way to pin the
service to loopback at the kernel level. A user manager cannot apply
them, so loopback stays a property of the argument list.
