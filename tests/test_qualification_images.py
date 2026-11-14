"""The disposable qualification images install what the installer tells users.

2026-11-13: the Debian/Ubuntu images installed `gcc make` while the installer and
the README name build-essential. On Ubuntu 26.04.1 gcc-15 only recommends
libc6-dev, so the backend build stopped at `assert.h` and the image tested a
setup no user is told to create.
"""

import importlib.util
import re

from support import repo_file


def test_debian_and_ubuntu_images_install_the_documented_build_prerequisites():
    spec = importlib.util.spec_from_file_location(
        "qualify_distribution", repo_file("scripts", "qualify-distribution.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    installed = set(module.APT.split("install -y", 1)[1].split())
    preflight = repo_file("scripts", "install-preflight.py").read_text()
    for family in ("debian", "ubuntu"):
        hint = re.search(r"'%s': 'apt install ([^']+)'" % family, preflight)
        assert hint, family
        missing = set(hint.group(1).split()) - installed
        assert not missing, (family, sorted(missing))
