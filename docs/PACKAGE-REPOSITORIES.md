# Signed package repositories (0.8.0 development)

The production endpoint is planned at
`https://relative23.github.io/oscmix-desk/`. It is **not published or qualified
yet**. Continue using authenticated release artifacts until the final channel
instructions and publication evidence are available.

The prepared channels cover Debian 13, Ubuntu 24.04/26.04, Fedora 44 and
openSUSE Leap 16 on x86_64. Each carries the core and exactly matching upstream
GTK companion. Arch retains its native artifact/recipe, and the source archive
remains available. Installing packages does not enable a service or apply a desk.

The Fedora subscription uses DNF5 on a mutable Fedora host. Silverblue's
`rpm-ostree` does not run that verifier. The subscription helper therefore refuses
installation and activation on an OSTree host before modifying trust or source
configuration; status, disable and removal remain available. Use the authenticated
core/GTK RPM files and the [Silverblue host layering procedure](SILVERBLUE.md).

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
exact core/GTK relationship. An initial channel requires the core, GTK and
repository-client package. Later authenticated snapshots can update that client
alone while retaining the previous core/GTK pair, or add a complete new pair.
This keeps certificate maintenance independent of a mixer/backend rebuild.
A checksum is an integrity check, not independent
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
A previously published package version cannot change its payload; increment the
package revision instead. RPM signature rotation verifies unchanged header/payload
digests, gives the newly signed form a new content-addressed path and retains the
previous bytes. `packages` names the currently indexed form; `retained_packages`
names older signature forms available only for previously authenticated indexes.
Publication checks the actual RPM primary index against that distinction and
authenticates the original build of both forms. Old APT `by-hash`
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
subkey rotation. Confirmed offline custody, final-candidate native-bundle/index
qualification and published HTTPS key-lifecycle qualification remain release gates.

Native clients do not all enforce the same current-key policy. Development tests
found actual expired/revoked-signature acceptance; [ADR 0036](decisions/0036-repository-client-verification.md)
records the additional repository-scoped verification. The APT adapter retains
the selected native gpgv/sqv verifier, the libdnf5 plugin checks the loaded
repository's actual cache, and zypper selects a mandatory signature plugin. They
share a local-file verifier and use the installed project certificate. Other
repositories keep their native verification behavior.

## Preparing the complete publication input

`scripts/prepare-repository-site.py` assembles all five signed channels in one
archive. It uses only the selected public certificate. The input directory must
contain exactly the five target directories, each with its complete signed file
index, native packages and original build manifests. All channels must name the
selected source commit and snapshot. Unlisted files, incomplete version pairs,
development payloads in release mode, links and unexpected metadata paths are
refused. The output archive is deterministic, is never replaced, and receives
its final name only after its actual archived contents pass verification.

```sh
python3 scripts/prepare-repository-site.py stage \
  --channels /staging/channels --snapshot "$snapshot_id" \
  --expected-commit "$release_commit" \
  --public-key packaging/keys/oscmix-desk-archive.asc \
  --output "/staging/oscmix-repositories-$snapshot_id.tar.gz"

python3 scripts/prepare-repository-site.py verify \
  --archive "/staging/oscmix-repositories-$snapshot_id.tar.gz" \
  --sha256 "$archive_sha256" --snapshot "$snapshot_id" \
  --expected-commit "$release_commit" \
  --public-key packaging/keys/oscmix-desk-archive.asc \
  --output /staging/verified-site
```

Select `archive_sha256` from the reviewed local staging result. An unauthenticated
checksum downloaded beside an archive does not establish its origin. Verification
requires a new output directory and checks every channel before exposing that
directory. Archives are limited to 50,000 regular files and 900 MiB of file
contents; links, special files, duplicate entries and escaping paths are rejected.
Hitting a limit requires an explicit retention decision, not silently deleting
packages still referenced by published metadata.

Release-mode `verify` additionally requires a GitHub CLI with attestation support.
It authenticates each original build manifest against this project's release
workflow, the exact release tag and source commit, excluding self-hosted runners.
DEBs must retain their attested build digest. For each signed RPM it retrieves
the original release artifact, checks its digest against the attested manifest,
compares the immutable-header and payload digests, and verifies the signed RPM
with an isolated RPM database containing only the selected project certificate.
A missing or failed attestation prevents creation of the verified site.

For offline verification, `--release-inputs DIR` supplies
`DIR/vVERSION/attestation.jsonl` and the original unsigned RPM files for every
retained release. `--trusted-root FILE` supplies a previously authenticated
`gh attestation trusted-root` export. Neither path contains signing secrets.
`--development` is solely for unpublished fixtures; its archives carry that
designation and cannot pass release-mode verification.

The [publication-input record](evidence/0.8.0/repository-publication-input-development.json)
includes a fresh full-site exercise with disposable signatures and a separate
fresh provenance probe against the six published 0.7.3 DEB/RPM artifacts. It covers
wrong source identity, changed manifests/DEBs/original RPMs, a validly signed but
different RPM payload and an unsigned RPM with otherwise matching content.
These checks do not qualify 0.8.0 payloads, Pages deployment, the final signed-output
attestation or HTTPS publication; those remain mandatory before release.

## Subscription package

The separate `oscmix-desk-repository` DEB/RPM installs the helpers and a public
certificate. It has no core/GTK dependency, creates no enabled package source
and starts no mixer. The build interface is
`scripts/build-package.py --repository-client-only --format deb|rpm --output DIR`;
release builds require a clean committed tree. Native DEB/RPM builds and release
collection now require this third package. The
[bundle/index development record](evidence/0.8.0/repository-native-bundles-development.json)
records all six native and five repository matrices, including repeat-build
equality, independent client updates retaining the existing pair, restrictive
installation umasks and APT lists-lock contention. These are development packages;
the final release candidate and published HTTPS channels still need qualification.

After authenticating and installing the native bootstrap artifact, its explicit
subscription interface is:

```sh
oscmix-repository status
sudo oscmix-repository enable
sudo oscmix-repository disable
```

Status is read-only JSON. `enabled` records the selected subscription;
`maintenance` and `native_key_pending` identify unfinished installation/trust
work. Enable validates the installed helper files and creates only the selected
distribution's source and hook configuration. A modified owned definition is
saved beside it and left disabled during package maintenance. Unregistered files
are refused. Disable/removal leave public certificate/registration state available
for recovery; they do not change user routing, profiles or service activation.

An installed helper update can carry a new signing subkey. Updating the same
primary certificate merges existing revocations, including during rollback.
For a separately authenticated certificate, `sudo oscmix-repository refresh-key
--certificate /path/to/public.asc --fingerprint FULL_PRIMARY_FINGERPRINT` requires
the complete primary fingerprint. A different primary requires independent
authentication of that fingerprint; the old package source cannot authorize its
own replacement trust anchor.

APT invalidates only the project's list files under the native lists lock. A busy
lock times out with maintenance active. RPM definitions receive a new identity
when their certificate changes. On openSUSE, the native zypper commit-end hook
completes a pending project key import after RPM releases its database lock.
Use zypper for the supported RPM subscription upgrade path. Direct low-level RPM
replacement can leave `native_key_pending` set; the mandatory signature hook then
refuses the channel until `sudo oscmix-repository enable` completes the checked
repair outside the RPM transaction. No global automatic key acceptance is enabled.

If installation was interrupted or a helper file is incomplete, reinstall the
authenticated native bootstrap package using the package manager. Do not remove
the journal or bypass signature checks. Complete recovery requires intact
installed helper hashes and both unfinished-work fields false; then verify a
normal native repository refresh. These development interfaces are exercised by
the [bootstrap record](evidence/0.8.0/repository-bootstrap-development.json);
qualified public download instructions still await the final release.

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

`scripts/qualify-repositories.py` creates public fixtures with disposable keys in
an isolated tmpfs, then runs both lifecycle and key checks in the actual native
client. It copies only explicitly selected public files into containers. The
test private keys are destroyed with their temporary signing environment, and
neither host credentials nor the production signing key enter these tests.
`--native-only` is an explicitly labelled diagnostic control, never a release
gate. CI uses the strict path. The [client development record](evidence/0.8.0/repository-client-development.json)
includes wrong/expired/revoked/rotated keys, unsigned or wrongly signed RPMs,
missing-verifier recovery, invalid trust configuration and unaffected unrelated
repositories. Those individual hook-fault tests retain their development installer.
The same command now separately runs `tests/repository_bootstrap.py` in another
disposable native container. It builds actual client DEBs/RPMs twice, installs
them through the native managers, and checks opt-in activation, packaged subkey
rotation, revoked-key cache invalidation, rollback preserving revocations, a killed
helper update, repair of damaged installed files, retained administrator edits
and removal. APT also checks actual lists-lock contention and recovery. Bootstrap
fixtures contain disposable certificates and loopback URLs; their local unsigned
RPM installation is explicitly separate from signed channel authentication.

The loopback checks do not qualify TLS/HTTPS publication, final payloads or device
activation. Publishing must follow the full release gates,
retain complete immutable snapshots and verify downloads with all five actual
clients. A partially generated directory or a successful `gpgv` invocation alone
does not satisfy N6. The publication workflow and final subscription instructions
are still pending. [ADR 0035](decisions/0035-signed-repository-snapshots.md) records
the architecture and primary metadata/trust references. The
[development qualification record](evidence/0.8.0/signed-repository-development.json)
names the actual five managers, generator/test identities and signed snapshots.
