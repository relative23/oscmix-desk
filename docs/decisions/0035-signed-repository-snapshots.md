# 0035 -- Stage complete signed repositories and retain referenced content

**Status:** staging implemented for 0.8.0; publication and complete key-lifecycle
qualification remain open.

## Decision

Use the repository-owned HTTPS endpoint `https://relative23.github.io/oscmix-desk/`
for distribution-specific APT and RPM channels. Keep the existing Arch package
and source artifacts. `scripts/build-repository.py` prepares one complete channel
in a new directory; it never publishes or modifies its inputs. Publication must
retain immutable versioned snapshots and promote only a completely verified set
of channels. No Pages source or production channel is configured by this script.

Before signing, require the selected native distribution/architecture, an explicit
source commit, matching build digests and a core/GTK pair with an exact dependency.
Release staging refuses development/dirty packages. Artifact authentication through
the release workflow's attestations is a separate input requirement; a matching
local checksum alone does not establish who built a package.

APT uses signed `InRelease` and `Release.gpg`, SHA-256 indexes, repository-scoped
`Signed-By` trust and retained `by-hash` files. RPM uses signed packages and
`repomd.xml`, checksummed metadata names and both package and metadata verification
in the client. Signing must leave each RPM's immutable header and payload digests
unchanged. The original unsigned build manifest is retained; the signed repository
manifest maps its original checksum to the signed RPM's checksum and location.
The release workflow must attest the final signed output as well as its inputs.

An authenticated previous repository supplies old packages and metadata. All its
listed file digests are checked before reuse. Package versions cannot be replaced
with new bytes; a new package revision is required. Package payload paths include
their hashes. Old APT indexes and RPM metadata remain available so an in-flight
client can finish against the metadata it already authenticated. Explicit rollback
selects a retained version from current metadata rather than disabling verification
to use an expired repository snapshot.

Each update or renewal needs an epoch strictly later than its predecessor. Mutable
indexes and signatures use that timestamp at whole-second resolution. Publication
must preserve changed HTTP validators: an actual APT run retained the old index
when a fast test deployment exposed different bytes with the same Last-Modified
second and incorrectly answered its conditional GET with 304.

APT metadata expires after 30 days by default (at most 90). Renewal produces a new
signed snapshot without rebuilding or renaming packages. RPM metadata has no
equivalent expiry field in this implementation; package-manager key-expiry behavior
must be measured independently. Do not claim identical freshness or key-rotation
behavior across APT, DNF and zypper before those checks pass.

The chosen signing fingerprint and certificate are explicit inputs. Only a public
OpenPGP certificate may enter the output. The signing keyring and passphrase file
remain outside staging; commands receive the passphrase filename, never its contents
in argv or logs. The project certificate and custody/rotation rules are recorded in
[the key documentation](../../packaging/keys/README.md).

## Qualification

`tests/repository_lifecycle.py` runs actual clients in disposable native containers
against a loopback HTTP server. It checks initial core-only/GTK installation,
altered metadata, missing and truncated packages, an actual killed transaction
after the maintenance fence appears, recovery, explicit downgrade, unavailable
server and preserved user files. These checks are separate from published HTTPS,
wrong/expired/rotated/revoked keys and the final 0.7.3-to-0.8.0 transition.

An interrupted DEB transaction has two recovery needs: drain dpkg's journal with
`dpkg --configure -a`, then repair incomplete files with
`apt-get --fix-broken install --reinstall` for the complete selected package pair.
The first command can report an inconsistent package requiring reinstallation;
that diagnostic is retained, and only the successful full repair clears the gate.
Do not clear maintenance markers manually.

The metadata and trust choices follow the primary
[APT authentication documentation](https://manpages.debian.org/trixie/apt/apt-secure.8.en.html),
[DNF5 configuration reference](https://dnf5.readthedocs.io/en/latest/dnf5.conf.5.html),
[zypper documentation](https://github.com/openSUSE/zypper/blob/master/doc/zypper.8.txt)
and [createrepo-c interface](https://manpages.debian.org/trixie/createrepo-c/createrepo_c.8.en.html).
