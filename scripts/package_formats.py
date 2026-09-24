"""RPM and Arch packaging of the common staged payload; no host installation."""

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess

from package_gtk import ARCH_INSTALL, ARCH_PREPARE, RPM_SPEC


def run(args, cwd=None, env=None):
    return subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def target_name(record):
    values = dict(line.split('=', 1) for line in record['os_release'].splitlines() if '=' in line)
    return values['ID'].strip('"') + values.get('VERSION_ID', '').strip('"')


def guard(root, action, gtk=False):
    source = (root / 'packaging/package-guard').read_text()
    arguments = [action, '--component', 'gtk' if gtk else 'core']
    return source.replace('args = parser.parse_args()',
                          'args = parser.parse_args(' + repr(arguments) + ')')


def archive(stage, destination, epoch):
    run(['tar', '--sort=name', '--mtime=@' + str(epoch), '--owner=0', '--group=0',
         '--numeric-owner', '-cf', destination, '-C', stage, '.'])


def rpm(root, stage, work, output, record, gtk):
    top = work / 'rpm'
    for directory in ('SOURCES', 'SPECS', 'BUILD', 'RPMS', 'SRPMS', 'BUILDROOT'):
        (top / directory).mkdir(parents=True)
    archive(stage, top / 'SOURCES/payload.tar', record['source_date_epoch'])
    release = ('0.dev.' if record['development'] else '') + str(record['package_revision'])
    spec = '''Name: oscmix-desk
Version: @VERSION@
Release: @RELEASE@
Summary: Declarative mixer state for the RME Fireface UCX II
License: MIT AND ISC AND Unlicense
URL: https://github.com/relative23/oscmix-desk
Source0: payload.tar
Requires: python3 >= 3.9
Requires(pre): /usr/bin/python3
Requires(post): /usr/sbin/groupadd
Requires(post): /usr/bin/systemd-tmpfiles
%global debug_package %{nil}
%global __brp_python_bytecompile %{nil}
%global _build_id_links none

%description
Declarative mixer state and lifecycle with the pinned oscmix backend.
Install files, review your desk, then explicitly activate it with oscmix-setup.

%prep
%build
%install
mkdir -p %{buildroot}
tar -xf %{SOURCE0} -C %{buildroot}

%pre
/usr/bin/python3 -I <<'PYTHON_GUARD'
@INSTALL_GUARD@
PYTHON_GUARD

%post
getent group audio >/dev/null || groupadd -r audio || exit 1
systemd-tmpfiles --create /usr/lib/tmpfiles.d/oscmix-desk.conf || exit 1
if command -v udevadm >/dev/null 2>&1; then udevadm control --reload-rules || :; fi
@GTK_SCHEMA@
echo 'Run oscmix-setup as your audio user; no service was started.'

%preun
if [ "$1" -eq 0 ]; then
/usr/bin/python3 -I <<'PYTHON_GUARD'
@REMOVE_GUARD@
PYTHON_GUARD
fi

%postun
if [ "$1" -eq 0 ]; then rm -f /var/lib/oscmix-desk/package-update; fi

%posttrans
/usr/lib/oscmix-desk/package-guard finish

%files
%defattr(-,root,root)
/usr/bin/*
/usr/lib/oscmix-desk
/usr/lib/systemd/user/oscmix.service
/usr/lib/systemd/system-sleep/oscmix
/usr/lib/tmpfiles.d/oscmix-desk.conf
/usr/lib/udev/rules.d/90-rme-fireface.rules
/usr/share/oscmix-desk
%doc /usr/share/doc/oscmix-desk
%license /usr/share/licenses/oscmix-desk
@GTK_FILES@
'''
    if gtk:
        spec = RPM_SPEC
    substitutions = {
        'VERSION': record['version'], 'RELEASE': release,
        'INSTALL_GUARD': guard(root, 'install', gtk).replace('%', '%%'),
        'REMOVE_GUARD': guard(root, 'remove', gtk).replace('%', '%%'),
        'FINISH_GUARD': guard(root, 'finish', gtk).replace('%', '%%'),
        'GTK_SCHEMA': ('glib-compile-schemas /usr/share/glib-2.0/schemas || exit 1'
                       if gtk else ''),
        'GTK_FILES': ('/usr/share/applications/oscmix-gtk.desktop\n'
                      '/usr/share/icons/hicolor/scalable/apps/oscmix.svg\n'
                      '/usr/share/glib-2.0/schemas/oscmix.gschema.xml' if gtk else ''),
    }
    for key, value in substitutions.items():
        spec = spec.replace('@' + key + '@', value)
    spec_path = top / 'SPECS/oscmix-desk.spec'
    spec_path.write_text(spec)
    environment = dict(os.environ, SOURCE_DATE_EPOCH=str(record['source_date_epoch']))
    run(['rpmbuild', '-bb', '--define', '_topdir ' + str(top),
         '--define', '_buildhost reproducible.oscmix-desk',
         '--define', 'use_source_date_epoch_as_buildtime 1',
         '--define', 'clamp_mtime_to_source_date_epoch 1', spec_path], env=environment)
    packages = list((top / 'RPMS').rglob('*.rpm'))
    if len(packages) != 1:
        raise ValueError('expected exactly one native RPM, got ' + str(packages))
    record['package_version'] = record['version'] + '-' + release
    record['architecture'] = run(['rpm', '-qp', '--queryformat', '%{ARCH}', packages[0]])
    result = output / (packages[0].stem + '_' + target_name(record) + '.rpm')
    shutil.copy2(packages[0], result)
    result.with_suffix('.spec').write_text(spec)
    return result


def arch(root, stage, work, output, record, gtk):
    if os.getuid() == 0:
        raise ValueError('makepkg must run as an ordinary build user')
    if not gtk:
        for name in ('00-oscmix-desk-maintenance.hook', '00-oscmix-gtk-maintenance.hook'):
            hook = stage / 'usr/share/libalpm/hooks' / name
            hook.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / 'packaging' / name, hook)
    recipe = work / 'arch'
    recipe.mkdir()
    payload = recipe / 'payload.tar'
    archive(stage, payload, record['source_date_epoch'])
    install = ARCH_INSTALL if gtk else (root / 'packaging/oscmix-desk.install').read_text()
    install += ARCH_PREPARE
    for action in ('install', 'remove', 'finish'):
        install = install.replace('@' + action.upper() + '_GUARD@', guard(root, action, gtk))
    (recipe / 'oscmix-desk.install').write_text(install)
    version = record['version'] + ('.dev' if record['development'] else '')
    architecture = platform.machine()
    dependencies = ['python>=3.9', 'alsa-lib', 'glibc', 'systemd', 'shadow']
    if gtk:
        dependencies = ['oscmix-desk=' + version + '-' + str(record['package_revision']), 'gtk3']
    pkgbuild = '''pkgname=@NAME@
pkgver=@VERSION@
pkgrel=@REVISION@
pkgdesc='Declarative mixer state and lifecycle for the RME Fireface UCX II'
arch=(@ARCH@)
url='https://github.com/relative23/oscmix-desk'
license=('MIT' 'ISC' 'Unlicense')
depends=(@DEPENDS@)
options=('!debug' '!strip')
install=oscmix-desk.install
source=('payload.tar')
sha256sums=('@HASH@')
package() {
    cp -a "$srcdir/usr" "$pkgdir/"
}
'''
    for key, value in {
        'NAME': 'oscmix-desk-gtk' if gtk else 'oscmix-desk',
        'VERSION': version, 'REVISION': str(record['package_revision']),
        'ARCH': shlex.quote(architecture),
        'DEPENDS': ' '.join(shlex.quote(item) for item in dependencies),
        'HASH': hashlib.sha256(payload.read_bytes()).hexdigest(),
    }.items():
        pkgbuild = pkgbuild.replace('@' + key + '@', value)
    (recipe / 'PKGBUILD').write_text(pkgbuild)
    environment = dict(os.environ, SOURCE_DATE_EPOCH=str(record['source_date_epoch']),
                       PACKAGER='oscmix-desk reproducible build')
    run(['makepkg', '--noconfirm'], cwd=recipe, env=environment)
    packages = list(recipe.glob('*.pkg.tar.*'))
    if len(packages) != 1:
        raise ValueError('expected exactly one Arch package, got ' + str(packages))
    result = output / packages[0].name
    shutil.copy2(packages[0], result)
    result.with_suffix(result.suffix + '.PKGBUILD').write_text(pkgbuild)
    record['package_version'] = version + '-' + str(record['package_revision'])
    record['architecture'] = architecture
    return result


def installed_metadata(stage, record):
    """Queryable provenance remains available after removing the build directory."""
    name = 'gtk-package.json' if record.get('component') == 'gtk' else 'package.json'
    path = stage / 'usr/share/oscmix-desk' / name
    path.write_text(json.dumps(record, indent=2) + '\n')
