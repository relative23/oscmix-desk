# Repository signing identity

The 0.8.0 repository is being prepared; no published package channel is
qualified yet. `oscmix-desk-archive.asc` is the public OpenPGP certificate
for the project-owned endpoint `https://relative23.github.io/oscmix-desk/`.
`archive-key.json` records its fingerprints, expiration and file hash.

- Certification key: `3C2F6FF8D8E2DFB4A048D23E87552FB27943AE1F`.
- Initial signing subkey: `3816B9E1EAA0A36A5251CDE8571F4D4F8E1E000B`.
- RSA 4096; repository signatures use SHA-256. Certification expires after
  five years, the first signing subkey after one year.

The protected local signing keyring contains only the signing subkey's
private material. The primary secret, encrypted recovery exports and
revocation certificate have separate storage outside the project. The
bootstrap keyrings remain local. A standard tar/OpenPGP encrypted recovery archive
has been copied to removable media, read back after unmounting and remounting,
and restored in isolated temporary keyrings: the recovered primary
certified a disposable test subkey, and the recovered release subkey signed a
verified challenge while its primary secret remained unavailable. The original
keyrings were not modified. The medium was safely unmounted and powered off.
Physical removal and independent passphrase custody are still unconfirmed.

Before publishing the signed channels, verify one encrypted copy outside the
signing computer and separate passphrase recovery. An existing USB stick is
sufficient; copying the archive does not require formatting it. A second copy
stored elsewhere is recommended additional protection, not a release condition.
Keep the passphrase separately in an independently recoverable password manager
or a checked paper record stored securely away from the recovery medium. A
password manager is not required. Retain a short restore procedure and full
fingerprints; a further protected emergency copy is recommended.
Include the revocation certificate inside the encrypted archive; possession of
that certificate can invalidate the key. Refresh the backup after each key change
and restore-test at least yearly. Move the primary secret fully offline only
after the backup and passphrase recovery are verified.

Neither private material nor passphrases belong in a
source checkout, build artifact, log, public CI secret dump or Pages tree.
A local detached signature was verified with the exported public key using
`gpgv`; APT/RPM client qualification remains a separate release gate.

Rotation uses the certification key to add a new signing subkey and publish
the updated certificate before signing repository metadata with that subkey.
Retain the old public subkey and signed snapshots for rollback; do not delete
old signatures or change an existing snapshot in place. A compromised signing
subkey must be revoked using the certification key and replaced. A compromised
certification key requires a new trust anchor with independently verified
fingerprint; an unauthenticated repository cannot bootstrap its own replacement.
Expiration, wrong keys, revoked/rotated subkeys and altered metadata/packages
must be tested with each actual package manager before publication.

Use repository-scoped APT `Signed-By` configuration and the corresponding
explicit RPM repository key configuration. Never disable package or metadata
signature checks to complete an upgrade. The final installation instructions
will name qualified channels after their actual publication tests.

The key separation follows GnuPG's
[secret-subkey export contract](https://gnupg.org/documentation/manuals/gnupg/Operational-GPG-Commands.html)
and [key-management interface](https://gnupg.org/documentation/manuals/gnupg/OpenPGP-Key-Management.html).
