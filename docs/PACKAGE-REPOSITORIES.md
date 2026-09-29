# Signed package repositories

Signed APT and RPM repositories are published at
`https://relative23.github.io/oscmix-desk/` since release 0.8.0. Releases
before 0.8.0 are available as [release artifacts](RELEASE-ARTIFACTS.md) only.

| Distribution | Channel |
| --- | --- |
| Debian 13 | APT |
| Ubuntu 24.04, 26.04 | APT |
| Fedora 44 | RPM (DNF5) |
| openSUSE Leap 16 | RPM (zypper) |

Each channel carries the core package (`oscmix-desk`), the matching GTK mixer
(`oscmix-desk-gtk`) and the subscription helper (`oscmix-desk-repository`).
Core and mixer versions must match exactly. Arch uses the release's native
package; Fedora Silverblue uses the release RPMs with
[host layering](SILVERBLUE.md).

## Subscribe

1. Download the subscription helper package for your distribution from the
   release and [verify it](RELEASE-ARTIFACTS.md).
2. Install it with your package manager and enable the subscription:

   ```sh
   sudo oscmix-repository enable
   oscmix-repository status        # read-only JSON
   ```

3. Install `oscmix-desk` (and `oscmix-desk-gtk` for the mixer) with apt, dnf
   or zypper as usual.

Installing packages never enables the service or applies a desk; see the
[installation guide](INSTALLATION.md) for activation.

The helper configures only this project's repository and its signing key. It
refuses signatures from expired or revoked keys, and packages from an
unauthenticated source are not installed. The public key and its fingerprints
are listed in [packaging/keys/README.md](../packaging/keys/README.md).

## Keys and recovery

A helper update can carry a new signing subkey; revocations are kept. To trust
a separately authenticated certificate:

```sh
sudo oscmix-repository refresh-key --certificate /path/to/public.asc \
  --fingerprint FULL_PRIMARY_FINGERPRINT
```

If an installation was interrupted, reinstall the helper package with your
package manager. `oscmix-repository status` reports `maintenance` or
`native_key_pending` while work is unfinished; `sudo oscmix-repository enable`
completes it. Do not bypass signature checks.

To stop using the repository:

```sh
sudo oscmix-repository disable
```

This removes the source definition. Routing configuration, profiles and the
service state are not changed.
