# Signed package repositories (0.8.0 development)

The production endpoint is planned at
`https://relative23.github.io/oscmix-desk/`. It is **not published or qualified
yet**. Continue using authenticated release artifacts until the final channel
instructions and publication evidence are available.

The prepared channels cover Debian 13, Ubuntu 24.04/26.04, Fedora 44 and
openSUSE Leap 16 on x86_64. Each carries the core and exactly matching upstream
GTK companion. Arch retains its native artifact/recipe, and the source archive
remains available. Installing packages does not enable a service or apply a desk.

## Staging a channel

`scripts/build-repository.py` requires Python 3.9+, GnuPG and the native metadata
tools: `apt-ftparchive`/`dpkg-deb` for APT, or `createrepo_c`, `rpm`, `rpmkeys`
and `rpmsign` for RPM. These are release tools, not desk runtime dependencies.
Run them in an isolated tool environment with an explicitly selected signing
keyring. Keep private keys and the mode-0600 passphrase file outside the checkout,
staging and published artifacts. Use only the signing subkey; the certification
key is not needed for a normal publication.

Authenticate the native build artifacts and their manifests first, using the
release workflow's attestations. The generator checks their hashes, actual native
package headers, target distribution/architecture, expected source commit and
exact core/GTK relationship. A checksum is an integrity check, not independent
build authentication. Release staging rejects development or dirty packages;
`--development` is reserved for unpublished qualification fixtures.

For example, after supplying the protected paths and expected commit:

```sh
python3 scripts/build-repository.py --target debian13 \
  --packages /qualified/debian13/package \
  --expected-commit "$release_commit" --snapshot "$snapshot_id" \
  --output /staging/new-debian13 \
  --gnupghome /protected/release-keyring \
  --public-key packaging/keys/oscmix-desk-archive.asc \
  --signing-key 3816B9E1EAA0A36A5251CDE8571F4D4F8E1E000B \
  --passphrase-file /protected/signing-passphrase --epoch "$(date +%s)"
```

The output directory must be new. The signed `repository.json` records the
generator identity, target, source commit, signing subkey, input manifests,
package checksums and every published file digest. No private input is copied.
For RPM, `provenance/*.rpm.json` is the original **unsigned build manifest**;
`repository.json` maps `unsigned_sha256` to the final signed package's `sha256`.
Signing verifies unchanged immutable-header and payload digests. Do not compare
a signed RPM against its unsigned artifact checksum as though they were the
same byte stream. Final publication must also carry authenticated provenance
for the signed output.

For an update, add `--previous /authenticated/current-channel`. Its signature
and listed file hashes are verified before old packages and metadata are copied.
A previously published package version cannot be replaced; increment the package
revision instead. New packages use content-addressed paths, and old APT `by-hash`
indexes and checksummed RPM metadata remain available to in-flight clients.
Every update/renewal needs an epoch later than its predecessor; the mutable indexes
and signatures receive that whole-second mtime. Preserve changed HTTP validators
when publishing and test conditional requests against the real HTTPS endpoint.
Different signed bytes with the same Last-Modified second can make a client retain
its previous index after a 304 response.

Metadata renewal omits `--packages` and supplies `--previous`, the unchanged
package source commit and a new snapshot identifier/output directory. APT metadata
expires after 30 days by default. Renew it before expiry; do not tell clients to
disable `Check-Valid-Until`. Explicit downgrade uses a retained package version
from the current authenticated index. The
[public-key record](../packaging/keys/README.md) describes custody, expiry and
subkey rotation. Offline custody and actual wrong/expired/rotated/revoked-key
client qualification remain release gates.

## Client and failure qualification

`tests/repository_lifecycle.py` runs in a disposable root container made from
the corresponding native qualification image. It serves the signed `first` and
`second` snapshot directories over loopback HTTP, then uses actual APT, DNF or
zypper. The container has no host bus, home, sound device or runtime external
network. RPM tests isolate both repository definitions and, for zypper, repository
services. APT uses a scoped `Signed-By`; RPM clients verify both metadata and
package signatures against the deliberately imported test certificate.

The test covers core-only installation, later GTK addition, altered metadata,
missing/truncated packages, an actual killed package transaction, repair, explicit
downgrade, removal, unavailable server and preserved configuration/profile/marker
bytes, owner and mode. It checks installed payload hashes after each successful
transition. Native package revision transitions are development checks; the final
0.7.3-to-0.8.0 release transition still has to be performed.

For DEB interruption recovery, leave the mixer stopped and run
`sudo dpkg --configure -a` first to process the pending dpkg journal. If it reports
that a package is inconsistent and requires reinstallation, repair the complete
chosen pair with `sudo apt-get --fix-broken install --reinstall oscmix-desk
oscmix-desk-gtk` from the authenticated selected channel. Pin the intended version
when several versions are available. For a core-only installation, omit the GTK
package. Finish only when the package manager succeeds, the installed files match
the chosen pair and both applicable maintenance markers have cleared. Never delete
a marker to make a failed installation start.

The loopback checks do not qualify TLS/HTTPS publication, key lifecycle, final
payloads or device activation. Publishing must follow the full release gates,
retain complete immutable snapshots and verify downloads with all five actual
clients. A partially generated directory or a successful `gpgv` invocation alone
does not satisfy N6. The publication workflow and final subscription instructions
are still pending. [ADR 0035](decisions/0035-signed-repository-snapshots.md) records
the architecture and primary metadata/trust references. The
[development qualification record](evidence/0.8.0/signed-repository-development.json)
names the actual five managers, generator/test identities and signed snapshots.
