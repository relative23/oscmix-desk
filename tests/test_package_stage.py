"""The vendor payload runs from its final layout and starts nothing while staged."""

import os
import subprocess
import sys

from support import repo_file

from oscmix_desk import __version__


def test_stage_runs_and_does_not_activate_a_new_desk(tmp_path):
    project = repo_file('scripts/stage-install.sh').parent.parent
    backend = tmp_path / 'backend'
    backend.mkdir()
    for name in ('oscmix', 'alsaseqio'):
        binary = backend / name
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o755)
    (backend / 'LICENSE').write_text('backend license fixture\n')
    stage = tmp_path / 'stage'
    result = subprocess.run(['bash', str(project / 'scripts/stage-install.sh'),
                             '--destdir', str(stage), '--backend', str(backend)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    env = dict(os.environ)
    version = subprocess.run([sys.executable, str(stage / 'usr/bin/oscmix-session'),
                              '--version'], env=env, capture_output=True, text=True, check=True)
    assert version.stdout.strip() == __version__
    assert (stage / 'usr/bin/oscmix-setup').is_file()
    unit = (stage / 'usr/lib/systemd/user/oscmix.service').read_text()
    assert 'ExecStart=/usr/bin/oscmix-session' in unit
    assert 'ConditionPathExists=%E/oscmix/service-allowed' in unit
    assert not list(stage.rglob('service-allowed'))
    assert not list(stage.rglob('routing.conf'))
    assert not list(stage.rglob('*.wants'))
    assert not (stage / 'usr/share/applications/oscmix-gtk.desktop').exists()


def test_stage_refuses_to_merge_into_an_existing_installation(tmp_path):
    (tmp_path / 'existing').write_text('keep\n')
    result = subprocess.run(['bash', str(repo_file('scripts/stage-install.sh')),
                             '--destdir', str(tmp_path), '--backend', str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert 'not empty' in result.stderr
    assert (tmp_path / 'existing').read_text() == 'keep\n'


def test_companion_owns_no_core_file_and_no_activation_files(tmp_path):
    backend = tmp_path / 'backend'
    (backend / 'gtk').mkdir(parents=True)
    for name in ('oscmix', 'alsaseqio', 'gtk/oscmix-gtk'):
        (backend / name).write_text('#!/bin/sh\nexit 0\n')
        (backend / name).chmod(0o755)
    (backend / 'LICENSE').write_text('backend license\n')
    (backend / 'gtk/oscmix.gschema.xml').write_text('<schemalist/>\n')
    stages = [tmp_path / name for name in ('core', 'gtk')]
    for stage, options in zip(stages, [[], ['--gtk-only']]):
        subprocess.run(['bash', str(repo_file('scripts/stage-install.sh')),
                        '--destdir', str(stage), '--backend', str(backend), *options],
                       capture_output=True, text=True, check=True)
    core, gtk = [{str(p.relative_to(stage)) for p in stage.rglob('*') if p.is_file()}
                 for stage in stages]
    assert not core & gtk
    assert 'usr/bin/oscmix-gtk' in gtk
    assert 'usr/share/glib-2.0/schemas/oscmix.gschema.xml' in gtk
    assert not any('/lib/' in path for path in gtk)
    assert {path for path in gtk if path.startswith('usr/bin/')} == {'usr/bin/oscmix-gtk'}
    desktop = (stages[1] / 'usr/share/applications/oscmix-gtk.desktop').read_text()
    assert 'Exec="/usr/bin/oscmix-launch"' in desktop
