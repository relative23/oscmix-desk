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

Pushing the annotated release tag starts the release workflow. After the
archive checks, it requires the version's committed hardware
evidence and release notes, adds their checksums, and attests the files.
It also requires the completed software gates to identify the exact
runtime, tests and tools by file hash, plus a passing physical disconnect
and reconnect record. Intermediate candidate records cannot qualify a tag.
For 0.8.0, it creates or resumes a draft and uploads the complete asset set.
The draft remains unpublished while the separately signed package sources
are verified and qualified. The repository-publication workflow verifies
the native build provenance, current signatures and selected predecessor,
then attests the signed output before an explicitly selected Pages deployment.
Private signing keys remain local. Publish the draft only after the final
repository, HTTPS and other release gates pass; verify the public release
downloads afterwards. See the [repository procedure](PACKAGE-REPOSITORIES.md#publishing-and-recovering-a-channel)
and [release checklist](RELEASE-CHECKLIST.md).

A failed build leaves a draft that can be resumed. An already published
release is never replaced by the tag path. The workflow no longer reacts
to releases published through the GitHub UI. Complete the local release
gates before pushing the tag: CI builds and attests the recorded
measurements, not the device. The 0.8.0 publication workflow is implemented;
its actual Pages, independent recovery-proof and HTTPS qualification is
still pending.

The tag path uses the repository's existing Actions token. The repository
publication workflow is dispatched separately from the qualified `main`
revision; it defaults to verification and attestation without deployment.

The historical 0.7.3 release included all seven source targets and four
native targets, each supplying core and optional GTK packages. In 0.8.0,
native targets also include Debian 13 and Ubuntu 26.04. APT/RPM
targets additionally build the separate repository-client package; the
complete native bundle, build metadata, logs and checksums are attached to
the draft. The release workflow calls the distribution workflow directly
and waits for its result. Pushes to the installation branch exercise the
build/collection path with development packages and retain CI artifacts;
they do not create a release. Final package attestations require a matching
release tag and fresh matching hardware evidence.

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
