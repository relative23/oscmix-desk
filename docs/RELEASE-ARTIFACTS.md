# Release artifacts

The 0.7.3 release workflow builds `oscmix-desk-0.7.3.tar.gz` from its Git
commit. It contains `install.sh`, the Python runtime, system integration,
tests and documentation. It also produces `release-manifest.json` with
the desk commit, pinned oscmix SHA and archive digest, plus `SHA256SUMS`.
Locally compiled oscmix binaries are not included.

The workflow builds twice and compares bytes, then runs the installer
tests from the extracted archive. GitHub's attestation identifies the
archive, manifest and checksums produced by this repository's workflow.
Hardware evidence is measured locally and attached separately; it is not
an audio test performed by a GitHub runner.

From 0.7.1, pushing the annotated release tag also starts this workflow.
After the archive checks, it requires the version's committed hardware
evidence and release notes, adds their checksums, and attests the files.
It also requires the completed software gates to identify the exact
runtime, tests and tools by file hash, plus a passing physical disconnect
and reconnect record. Intermediate candidate records cannot qualify a tag.
It creates a draft, uploads the complete asset set and then publishes it.
A failed run leaves a draft that can be resumed; an already published
release is not replaced by the tag path. Creating a release through the
GitHub UI remains supported. Run the release checklist before pushing
the tag: CI builds and attests the recorded measurements, not the device.

The tag path uses the repository's existing Actions token. No local
browser login or additional personal token is needed. A release published
with that token does not trigger another release-event workflow; any
additional package workflow must also handle the tag explicitly.

The 0.7.3 release workflow calls the distribution
workflow directly and waits for all seven source targets and four native
targets. Each native target supplies both core and optional GTK packages.
It collects their packages, build metadata, logs and checksums into the
same asset set before publishing. There is no dependency on a second
release event. Pushes to the installation branch exercise this complete
build/collection path with development packages and retain CI artifacts;
they do not create a release. Final package attestations require a matching
release tag, and the tag path still requires fresh matching hardware evidence.

## Verify before installing

After the release is published, download its assets into a new directory.
`SHA256SUMS` covers the source archive, native packages, manifests and
evidence, so the complete checksum check below needs all of those files.
Use a GitHub CLI with `gh attestation verify` available (older distro
versions may not provide it). Verify the expected repository, workflow
and release tag, then the checksums:

```sh
mkdir oscmix-desk-0.7.3-release
cd oscmix-desk-0.7.3-release
gh release download v0.7.3 --repo relative23/oscmix-desk
gh attestation verify SHA256SUMS \
  --repo relative23/oscmix-desk \
  --signer-workflow relative23/oscmix-desk/.github/workflows/release.yml \
  --source-ref refs/tags/v0.7.3 \
  --deny-self-hosted-runners --bundle attestation.jsonl
sha256sum --check SHA256SUMS
tar -xzf oscmix-desk-0.7.3.tar.gz
cd oscmix-desk-0.7.3
./install.sh
```

The [GitHub CLI verification reference](https://cli.github.com/manual/gh_attestation_verify)
describes these identity constraints. The authenticated checksum manifest
covers the archive and release manifest. A changed artifact fails the
digest check. A successful checksum alone proves no publisher identity;
keep the attestation check. Compare `desk_commit` with the tag's peeled
commit when auditing a specific revision.

A manual workflow run on a branch is candidate evidence: the release-tag
constraint above must fail for it. Until the workflow has run and its
bundle is attached, a locally built archive has checksums but **no GitHub
attestation**. The qualification record must distinguish these states.

## Reproduce the source archive

From a checkout containing the selected commit:

```sh
python3 scripts/build-release.py --ref v0.7.3 --output build/first
python3 scripts/build-release.py --ref v0.7.3 --output build/second
diff -r build/first build/second
```

The builder reads Git objects, so local uncommitted changes do not enter
the archive. Tar entries come from `git archive`; gzip has no stored
filename and a fixed timestamp. Reproducibility is measured for the source
archive on the recorded build environment. It does not assert bitwise
reproducibility across all Git/zlib versions or for the separately
compiled C backend. An attestation for our build also cannot authenticate
upstream's unsigned history; see the [security model](SECURITY-MODEL.md).
