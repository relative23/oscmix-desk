# 0036: check the project signing key at native metadata verification

Status: development implementation with native bootstrap/key-update checks;
final artifact and publication qualification remain open. The maintainer approved the additional repository
client integration after the native policy gap was measured.

The signed repository generator already rejects expired and revoked signatures.
That does not establish client behavior. Actual Debian 13 APT accepted an older
signature from a currently expired subkey. Fedora and openSUSE also accepted a
signature generated after the signing subkey had expired; Fedora accepted a
revoked subkey after its updated certificate had been imported. A later control
run with DNF5 5.4.5 shows the same cases. These are failed authentication gates,
not a reason to relax the expected result. Unrelated expired subkeys must still
allow a signature by another current, unrevoked subkey in the same certificate.

Add one stdlib Python verifier outside the mixer runtime. It receives existing
local metadata/signature paths, reads the explicitly installed project trust
anchor and invokes GnuPG in a disposable verification home. It downloads nothing,
uses no personal keyring and retains no successful-verification cache. Require
one GOODSIG and one VALIDSIG, the expected full primary fingerprint and SHA256,
and reject explicit expired, revoked, bad, missing or otherwise failed signatures.
A bare gpgv exit zero is insufficient. Trust files and their parents must belong
to root and must not be writable by other users or be symlinks.

Use the native manager's existing metadata, not a separate fetch that could
authenticate different bytes:

- APT's gpgv/sqv acquisition-method adapters add the check only when Signed-By
  names the installed project certificate. The request's exact signature and
  data filenames are used. The original native method still performs its normal
  verification. Other repositories are delegated unchanged, including their
  choice of sqv versus gpgv. No distribution-owned binary is replaced.
- The libdnf5 plugin selects repositories using that exact local project key
  URI. After repositories load it checks their actual Repo object's cache and
  loaded primary-metadata path; it never searches old cache directories by glob.
  Incoming transactions repeat the check for their selected project repositories.
  Existing native metadata and RPM signature checks must stay enabled.
- The project zypper definition selects a mandatory `repo_sigcheck_plugin`.
  libzypp supplies the actual downloaded master index and detached signature.
  Missing plugins and ERROR replies reject the repository. The plugin requests
  no downloaded trust key and does not affect definitions that do not select it.

All adapters fail closed on missing verifier/trust files or verification errors.
They do not activate hardware, install packages, obtain privilege or implement an
updater. A separate, root-run subscription installer owns the trust configuration
and the exact project RPM certificate. The native package manager remains responsible for dependency resolution,
payload hashes, its package database and transaction recovery. Administrators
can still deliberately override their own package-manager configuration; this
integration is not protection from a hostile root user.

The native `oscmix-desk-repository` bootstrap package owns these helpers and their
payload hashes. Installing it subscribes to no channel and starts no mixer.
`oscmix-repository enable` explicitly creates the selected distribution's source
and verification configuration. Its journal is written before removing the source
and hooks for an update. Only complete, verified installed payloads can restore
the source. A modified owned definition is backed up and left disabled; unowned
configuration is refused. Disable/removal retain public trust/registration state.

Certificate updates merge the existing public certificate, preserving revocations
when an older helper is reinstalled. A primary-key change needs an explicit full
fingerprint and public certificate. APT cache invalidation uses its actual native
URI encoding and waits at most ten seconds for its POSIX lists lock; a busy cache
leaves maintenance active. It deletes only that source's index files. A certificate
digest gives RPM repositories a new cache/keyring identity. Explicit subscription
and key-update commands replace only the exact project RPM certificate.

RPM's own database lock prevents a scriptlet from importing a key. Fedora's native
client imports the new installed certificate when the new repository is used.
For libzypp, its current process already holds a keyring snapshot: importing from
the signature-plugin startup still left that invocation with the old key. A
commit-end hook therefore completes an explicitly pending project key update
after RPM finishes. The next native invocation starts with the updated keyring.
This hook downloads nothing and does nothing without that pending project record.
The mandatory signature hook refuses a still-pending native-key update, including
one left by direct low-level RPM use or an interrupted completion. Status reports
it, and explicit subscription repair completes it. No global automatic key-trust
policy is enabled for other repositories.

The [bootstrap development record](../evidence/0.8.0/repository-bootstrap-development.json)
identifies actual DEB/RPM repeat builds and five native lifecycle checks: opt-in
activation, packaged subkey rotation, revoked-key cache invalidation, rollback
without losing revocations, killed updates, incomplete-payload refusal and
preserved administrator edits. These checks use disposable keys. Inclusion in
final native bundles/indexes, offline custody and published HTTPS checks still
belong to N6. The mixer-runtime standard-library contract and existing standalone
release-artifact installation remain separate.

References: [APT method selection](https://github.com/Debian/apt/blob/main/apt-pkg/acquire-worker.cc),
[libdnf5 plugin hooks](https://dnf5.readthedocs.io/en/latest/tutorial/plugins/libdnf5-plugins.html),
[libdnf5 repository API](https://dnf5.readthedocs.io/en/stable/api/python/libdnf5_repo.html),
[libzypp mandatory signature checks](https://opensuse.github.io/libzypp/plugin-sigcheck.html).
[libzypp 17.38.15 verification order](https://github.com/openSUSE/libzypp/blob/17.38.15/zypp/zypp/ng/repo/workflows/repodownloaderwf.cc),
[libzypp commit notifications](https://opensuse.github.io/libzypp/plugin-commit.html).
