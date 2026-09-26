"""Install the real verification hooks only in a disposable native test container.

Production bootstrap/package ownership is a separate qualification. This fixture
does not claim it by copying development files into the container.
"""

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = Path('/usr/lib/oscmix-desk-repository')
CERTIFICATE = Path('/usr/share/keyrings/oscmix-desk-repository.asc')
CONFIG = Path('/etc/oscmix-desk/repository.json')


def container():
    assert Path('/.dockerenv').is_file()
    assert os.getuid() == 0
    # Docker exposes read-only host USB descriptions in sysfs even without
    # a device mount. Guard the actual access paths, not those descriptions.
    assert not Path('/dev/snd').exists()
    assert not Path('/dev/bus/usb').exists()


def trust(certificate):
    container()
    with tempfile.TemporaryDirectory(prefix='oscmix-test-key-') as home:
        listed = subprocess.check_output(
            ['gpg', '--batch', '--no-options', '--homedir', home, '--with-colons',
             '--show-keys', str(certificate)], text=True)
    primary = next(line.split(':')[9] for line in listed.splitlines() if line.startswith('fpr:'))
    CERTIFICATE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(certificate, CERTIFICATE)
    CERTIFICATE.chmod(0o644)
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(dict(schema=1, primary=primary)) + '\n')
    CONFIG.chmod(0o644)
    return CERTIFICATE


def install(target):
    container()
    CLIENT.mkdir(parents=True, exist_ok=True)
    for name in ('verify.py', 'apt-method.py', 'zypp-sigcheck.py'):
        shutil.copyfile(ROOT / 'packaging/repository' / name, CLIENT / name)
        (CLIENT / name).chmod(0o755)
    if target.startswith(('debian', 'ubuntu')):
        for method in ('gpgv', 'sqv'):
            link = CLIENT / ('apt-' + method)
            link.unlink(missing_ok=True)
            link.symlink_to('apt-method.py')
        Path('/etc/apt/apt.conf.d/90oscmix-repository').write_text(
            'Dir::Bin::Methods::gpgv "' + str(CLIENT / 'apt-gpgv') + '";\n'
            'Dir::Bin::Methods::sqv "' + str(CLIENT / 'apt-sqv') + '";\n')
    elif target == 'opensuse16':
        link = Path('/usr/lib/zypp/plugins/sigcheck/oscmix-desk')
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(CLIENT / 'zypp-sigcheck.py')
    else:
        assert target == 'fedora44'
        flags = shlex.split(subprocess.check_output(
            ['pkg-config', '--cflags', '--libs', 'libdnf5'], text=True))
        output = Path('/usr/lib64/libdnf5/plugins/oscmix-repository.so')
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['g++', '-std=c++20', '-Wall', '-Wextra', '-Werror', '-O2', '-fPIC',
                        '-shared', str(ROOT / 'packaging/repository/dnf.cpp'),
                        '-o', str(output), *flags], check=True)
        directory = Path('/etc/dnf/libdnf5-plugins')
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'oscmix-repository.conf').write_text(
            '[main]\nname=oscmix-repository\nenabled=yes\n')


if __name__ == '__main__':
    import sys
    install(sys.argv[1])
