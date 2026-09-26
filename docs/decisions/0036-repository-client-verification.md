# 0036: check the project signing key at native metadata verification

Status: development implementation; installation, key-update and publication
qualification remain open. The maintainer approved the additional repository
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
updater. The native package manager remains responsible for dependency resolution,
payload hashes, its package database and transaction recovery. Administrators
can still deliberately override their own package-manager configuration; this
integration is not protection from a hostile root user.

These hooks alone do not finish N6. Their native packaging/bootstrap must own
installation and removal of the integration. Updating the current certificate
must replace the exact project RPM certificate and invalidate only the project
metadata caches, including APT/libzypp caches that would otherwise skip a fresh
signature check. A new DNF repository identity avoids reusing its older metadata
keyring. Test those operations through the actual installation interface, along
with interruption and rollback; direct development-file copies are not evidence
of a working bootstrap. The current mixer-runtime standard-library contract and
existing standalone release-artifact installation remain separate.

References: [APT method selection](https://github.com/Debian/apt/blob/main/apt-pkg/acquire-worker.cc),
[libdnf5 plugin hooks](https://dnf5.readthedocs.io/en/latest/tutorial/plugins/libdnf5-plugins.html),
[libdnf5 repository API](https://dnf5.readthedocs.io/en/stable/api/python/libdnf5_repo.html),
[libzypp mandatory signature checks](https://opensuse.github.io/libzypp/plugin-sigcheck.html).
