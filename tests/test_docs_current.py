"""The documentation states what this tree is and does.

2026-11-14: a week after 0.10.0 the README still called 0.9.0 the latest release, and the
verification recipe downloaded v0.9.0. Every fact the docs repeat from the code or from
the release history is checked here, so a change that makes one stale fails `make check`
instead of reaching a reader: the version in README, CHANGELOG, upgrade notes and release
commands; the Python floor; each distribution and repository channel; each patch of the
backend series; each mixer probe; and every link between the files.
"""

import json
import os
import posixpath
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest
from support import repo_file

from oscmix_desk.constants import __version__

ROOT = repo_file("README.md").parent
DATED = re.compile(r"^## (\d+\.\d+\.\d+) -- (\d{4}-\d{2}-\d{2})$", re.MULTILINE)
#: How the docs name each build target, as (family, release); release "" for rolling ones.
#: A new target fails here until it is named, and then until the docs name it.
DISTRIBUTIONS = {"debian13": ("Debian", "13"), "ubuntu2404": ("Ubuntu", "24.04"),
                 "ubuntu2604": ("Ubuntu", "26.04"), "fedora44": ("Fedora", "44"),
                 "fedora45": ("Fedora", "45"), "opensuse16": ("openSUSE", "16"),
                 "arch": ("Arch", ""), "alpine322": ("Alpine", "3.22")}


def text(*parts):
    return repo_file(*parts).read_text()


def version_key(version):
    return tuple(int(part) for part in version.split("."))


def latest_release():
    return DATED.search(text("CHANGELOG.md")).group(1)


def script_targets(name):
    """The TARGETS keys a script declares, without running it."""
    source = text("scripts", name)
    block = source.split("TARGETS = {", 1)[1].split("\n}", 1)[0]
    return re.findall(r"^\s+'([a-z0-9]+)': \(", block, re.MULTILINE)


def test_the_readme_names_the_latest_release():
    stated = re.search(r"The latest release is \*\*(\d+\.\d+\.\d+)\*\*\.", text("README.md"))
    assert stated, "README.md lost its latest-release line"
    assert stated.group(1) == latest_release(), "README.md names %s, CHANGELOG.md %s" % (
        stated.group(1), latest_release())


def test_the_version_and_the_changelog_agree():
    top = re.search(r"^## (.+)$", text("CHANGELOG.md"), re.MULTILINE).group(1)
    latest = latest_release()
    if top == "Unreleased":
        assert __version__ == latest, "bump __version__ with the section heading"
    elif top.endswith(" -- unreleased"):
        assert top == __version__ + " -- unreleased", top
        assert version_key(__version__) > version_key(latest), (__version__, latest)
    else:
        assert top == "%s -- %s" % (__version__, DATED.search(text("CHANGELOG.md")).group(2))


def test_the_upgrade_notes_cover_this_version():
    assert ("## Upgrading to %s\n" % __version__) in text("docs", "UPGRADING.md")


def test_the_verification_recipe_uses_the_latest_release():
    recipe = text("docs", "RELEASE-ARTIFACTS.md")
    named = set(re.findall(r"(?:v|oscmix-desk-|Replace `)(\d+\.\d+\.\d+)", recipe))
    assert named == {latest_release()}, "RELEASE-ARTIFACTS.md commands name %s" % sorted(named)


def test_the_docs_state_the_python_floor_the_installer_checks():
    floor = re.search(r"sys\.version_info >= \((\d+), (\d+)\)",
                      text("scripts", "install-preflight.py"))
    assert floor, "install-preflight.py lost its Python check"
    floor = "%s.%s" % floor.groups()
    assert "python3 (>= %s)" % floor in text("scripts", "build-package.py")
    for name in ("README.md", "CONTRIBUTING.md", "docs/INSTALLATION.md"):
        stated = re.findall(r"\b(3\.\d+) or newer", text(name))
        assert stated, name + " does not state the Python floor"
        assert set(stated) == {floor}, (name, stated, floor)


def named(paragraph, target):
    family, release = DISTRIBUTIONS[target]
    return family in paragraph and release in paragraph


def paragraph_with(document, opening):
    found = [block for block in document.split("\n\n") if opening in block]
    assert len(found) == 1, opening
    return found[0]


def test_every_qualified_distribution_is_in_the_installation_guide():
    guide = text("docs", "INSTALLATION.md")
    source = paragraph_with(guide, "The source installer is tested on")
    native = paragraph_with(guide, "Use a package built for your distribution release")
    for target in script_targets("qualify-distribution.py"):
        assert named(source, target), "source installation: " + target
        if target != "alpine322":
            assert named(native, target), "native packages: " + target


def test_every_repository_channel_is_listed():
    rows = [line for line in text("docs", "PACKAGE-REPOSITORIES.md").splitlines()
            if line.startswith("| ") and "Channel" not in line]
    for target in script_targets("build-repository.py"):
        assert any(named(row, target) for row in rows), "PACKAGE-REPOSITORIES.md: " + target


def test_every_patch_of_the_backend_series_has_its_section():
    series = json.loads(text("patches", "backend-series.json"))
    readme = text("patches", "README.md")
    for entry in series["patches"]:
        number = entry["file"][:4]
        assert re.search(r"^## %s -- " % number, readme, re.MULTILINE), entry["file"]


def test_every_mixer_probe_is_described():
    readme = text("tests", "backend_mixer", "README.md")
    for probe in sorted(repo_file("tests", "backend_mixer").glob("*.c")):
        assert ("| %s |" % probe.stem in readme or "`%s`" % probe.name in readme), probe.name


def tracked():
    """The tracked files: from Git, or the list the pre-commit hook hands over with its copy."""
    listed = os.environ.get("OSCMIX_DOCS_TRACKED")
    if listed:
        return {name for name in Path(listed).read_text().split("\0") if name}
    try:
        names = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True,
                               check=True, timeout=30).stdout.decode().split("\0")
    except (OSError, subprocess.SubprocessError):
        names = []
    if not any(names):
        pytest.skip("no Git checkout: links are checked where the tracked files are known")
    return {name for name in names if name}


def slugs(markdown):
    """GitHub's heading anchors: lower case, punctuation dropped, spaces to hyphens."""
    seen, result = {}, set()
    for heading in re.findall(r"^#+ (.+?)\s*$", markdown, re.MULTILINE):
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        result.add(slug if count == 0 else "%s-%d" % (slug, count))
    return result


def test_every_link_between_the_files_resolves():
    files = tracked()
    directories = {str(parent) for name in files for parent in PurePosixPath(name).parents}
    broken = []
    for name in sorted(n for n in files if n.endswith(".md")):
        here = PurePosixPath(name).parent
        for target in re.findall(r"\]\(([^)\s]+)\)", text(name)):
            if re.match(r"[a-z]+:", target):
                continue
            path, _, anchor = target.partition("#")
            resolved = posixpath.normpath(str(here / path)) if path else name
            if resolved not in files and resolved not in directories:
                broken.append("%s -> %s" % (name, target))
            elif anchor and resolved.endswith(".md") and anchor not in slugs(text(resolved)):
                broken.append("%s -> %s (no such heading)" % (name, target))
    assert broken == [], broken
