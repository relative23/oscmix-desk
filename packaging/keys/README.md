# Repository signing key

`oscmix-desk-archive.asc` is the public OpenPGP certificate for the package
repositories at `https://relative23.github.io/oscmix-desk/`.
`archive-key.json` records its fingerprints, expiration and file hash.

- Certification key: `3C2F6FF8D8E2DFB4A048D23E87552FB27943AE1F`
- Signing subkey: `3816B9E1EAA0A36A5251CDE8571F4D4F8E1E000B`
- RSA 4096, SHA-256 signatures. The certification key expires after five
  years, the signing subkey after one year.

Compare the full fingerprint before trusting the certificate:

```sh
gpg --show-keys --with-fingerprint --with-subkey-fingerprints oscmix-desk-archive.asc
```

The subscription helper configures the key for this repository only (APT
`Signed-By`, per-repository RPM keys) and refuses expired or revoked
signatures. Never disable package or metadata signature checks to complete an
upgrade.

A new signing subkey is added with the certification key and published in an
updated certificate before it signs anything; old signatures and snapshots are
kept. A compromised subkey is revoked and replaced. A compromised certification
key requires a new certificate whose fingerprint is announced independently of
the repository.
