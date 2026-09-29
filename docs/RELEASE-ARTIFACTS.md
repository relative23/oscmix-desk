# Release artifacts

Each release on GitHub carries:

- `oscmix-desk-<version>.tar.gz`: the source archive with `install.sh`, the
  Python runtime, system integration, tests and documentation. The oscmix
  backend is built from its pinned source during installation.
- `release-manifest.json`: the desk commit, the pinned oscmix commit and the
  archive digest.
- native packages for Debian, Ubuntu, Fedora, openSUSE and Arch, each with
  its build metadata and checksums;
- the hardware and software verification records of the release;
- `SHA256SUMS` over all of these, and `attestation.jsonl`, GitHub's signed
  statement that the release workflow of this repository produced them.

## Verify before installing

Download all assets into a new directory and check the attestation, then the
checksums. `gh attestation verify` needs a recent GitHub CLI.

```sh
mkdir oscmix-desk-release && cd oscmix-desk-release
gh release download v0.8.0 --repo relative23/oscmix-desk
gh attestation verify SHA256SUMS \
  --repo relative23/oscmix-desk \
  --signer-workflow relative23/oscmix-desk/.github/workflows/release.yml \
  --source-ref refs/tags/v0.8.0 \
  --deny-self-hosted-runners --bundle attestation.jsonl
sha256sum --check SHA256SUMS
tar -xzf oscmix-desk-0.8.0.tar.gz
cd oscmix-desk-0.8.0
./install.sh
```

Replace `0.8.0` with the release you install. A changed file fails the
checksum check; a checksum without the attestation check proves nothing about
who built the file. See the
[GitHub CLI reference](https://cli.github.com/manual/gh_attestation_verify).

## Reproduce the source archive

```sh
python3 scripts/build-release.py --ref v0.8.0 --output build/first
python3 scripts/build-release.py --ref v0.8.0 --output build/second
diff -r build/first build/second
```

The builder reads Git objects, so uncommitted changes do not enter the
archive, and gzip carries no file name or timestamp. This covers the source
archive, not the separately compiled C backend. The attestation cannot
authenticate upstream oscmix's unsigned history; the pinned commit stands in
for that (see the [security model](SECURITY-MODEL.md)).
