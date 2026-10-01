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

`oscmix-repositories-<snapshot>.tar.gz`, the signed repository site, is
attached after that workflow and is not listed in `SHA256SUMS`. The
repository publication workflow attests it and attaches that attestation as
`oscmix-repositories-<snapshot>-<run>-<attempt>.attestation.jsonl`; check it
with `gh attestation verify` and `--signer-workflow
relative23/oscmix-desk/.github/workflows/repository-publication.yml`.
Installing from the [package repositories](PACKAGE-REPOSITORIES.md) does not
use it.

## Verify before installing

Download all assets into a new directory and check the attestation, then the
checksums. `gh attestation verify` needs a recent GitHub CLI.

```sh
mkdir oscmix-desk-release && cd oscmix-desk-release
gh release download v0.8.1 --repo relative23/oscmix-desk
gh attestation verify SHA256SUMS \
  --repo relative23/oscmix-desk \
  --signer-workflow relative23/oscmix-desk/.github/workflows/release.yml \
  --source-ref refs/tags/v0.8.1 \
  --deny-self-hosted-runners --bundle attestation.jsonl
sha256sum --check SHA256SUMS
tar -xzf oscmix-desk-0.8.1.tar.gz
cd oscmix-desk-0.8.1
./install.sh
```

Replace `0.8.1` with the release you install. A changed file fails the
checksum check; a checksum without the attestation check proves nothing about
who built the file. See the
[GitHub CLI reference](https://cli.github.com/manual/gh_attestation_verify).

## Verification records

The release workflow refuses a tag whose records do not match its files.

| File | Records | Tied to the release by |
| --- | --- | --- |
| `software-qualification.json` (schema 2) | every gate with exit code, duration and log digest (the suite, Python 3.9 to 3.14, coverage, flake, soak, fault repeats, mutation), coverage and mutation counts, the run with the interface switched off | `tested_files_sha256` and `supporting_files_sha256` over the tagged tree, `backend_series_sha256` |
| `hardware-evidence.json` | tone routed through each declared sink and measured at its outputs (`routes`, `sink_channels`, `min_response_db`), device serial and firmware | `desk_source.runtime_sha256`, `running_backend` (running binary digest, upstream commit, patch series digest) |
| `write-sweep-ucx2.json` (schema 3) | each writable register written and read back, one backend operation per chunk (`transactions`), a verdict per register (`findings`), restoration | as the hardware evidence |
| `lifecycle.json` (schema 2) | installation, physical disconnect and reconnect of the interface, status output and restoration of the host | `installed_runtime_sha256` |

These are measurements of one host and one UCX II; they are not a guarantee
for another setup.

## Reproduce the source archive

```sh
python3 scripts/build-release.py --ref v0.8.1 --output build/first
python3 scripts/build-release.py --ref v0.8.1 --output build/second
diff -r build/first build/second
```

The builder reads Git objects, so uncommitted changes do not enter the
archive, and gzip carries no file name or timestamp. This covers the source
archive, not the separately compiled C backend. The attestation cannot
authenticate upstream oscmix's unsigned history; the pinned commit stands in
for that (see the [security model](SECURITY-MODEL.md)).
