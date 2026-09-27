"""Status is useful offline and must never apply, bind, or start anything."""

import hashlib
import json
import socket
from dataclasses import replace

import pytest
from support import write_config
from test_streams import recorded, write_card
from two_boxes import A

from oscmix_desk import cli, desktop, hostservice, status
from oscmix_desk.desktop import DesktopStatus
from oscmix_desk.model import CommandLine, Config


@pytest.fixture
def world(tmp_path, monkeypatch, endpoint):
    _config, _control, proc = endpoint
    monkeypatch.setenv('OSCMIX_PROC_ROOT', str(proc))
    monkeypatch.setattr(status, 'resolve_binary', lambda *_: None)
    monkeypatch.setattr(hostservice, 'STATE', tmp_path)
    monkeypatch.setattr(desktop, 'resolve_binary', lambda *_: None)
    monkeypatch.setattr(socket, 'socket', lambda *_a, **_kw: pytest.fail('status opened a socket'))
    monkeypatch.setattr(cli, 'run_session', lambda *_: pytest.fail('status started a session'))
    path = tmp_path / 'routing.conf'
    path.write_text('[device]\nserial=24216011\n[route:main]\nplayback=1/2\noutput=5/6\n')
    return path, proc


def test_json_describes_the_observation_without_claiming_verified(world, capsys):
    path, proc = world
    write_card(proc, recorded())
    assert cli.main(['--config', str(path), '--status', '--json']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['schema_version'] == 2
    assert report['read_only'] is True
    assert report['verification'] == 'not-performed'
    sections = report['sections']
    assert sections['backend']['state'] == 'ready'
    assert sections['backend']['device']['serial'] == A[1]
    assert sections['playback']['rate'] == 48000
    assert sections['backend']['endpoint'].endswith('24216011.control')
    assert sections['backend']['pid'] == 101
    assert 'receive_port' not in sections
    assert sections['desktop']['binary'] is None
    assert sections['service']['state'] == 'unavailable'


def test_text_status_and_idle_playback_are_explicit(world, capsys):
    path, proc = world
    row = recorded()
    write_card(proc, {**row, 'stream0': row['stream0'].replace('Status: Running', 'Status: Stop')})
    assert cli.main(['--config', str(path), '--status']) == 0
    text = capsys.readouterr().out
    assert 'state: idle' in text
    assert 'verification: not-performed' in text
    assert 'no active PCM' in text
    assert str(path) in text


def test_invalid_config_still_produces_json_diagnosis(world, capsys):
    path, _ = world
    path.write_text('[device]\nusb-id=bad\n')
    assert cli.main(['--config', str(path), '--status', '--json']) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['configuration_valid'] is False
    assert report['sections']['configuration']['state'] == 'invalid'
    assert 'usb-id' in report['sections']['configuration']['detail']
    assert report['sections']['backend']['state'] == 'unknown'
    assert report['sections']['playback']['state'] == 'unknown'
    for name in ('backend', 'playback'):
        assert 'invalid configuration' in report['sections'][name]['detail']


def test_stale_active_profile_is_not_reported_as_the_effective_one(world):
    path, _ = world
    marker = path.with_name('active-profile')
    marker.write_text('missing\n')
    section = status.collect_status(path, CommandLine())['sections']['configuration']
    assert section['state'] == 'fallback'
    assert section['stored_profile'] == 'missing'
    assert section['effective_profile'] is None
    assert 'main configuration selected' in section['detail']
    assert marker.read_text() == 'missing\n'


def test_active_profile_and_selected_file_are_reported_without_changing_them(world):
    path, _ = world
    profile = path.parent / 'profiles/tracking.conf'
    write_config(profile, '[output:5]\nvolume=-20\n')
    path.with_name('active-profile').write_text('tracking\n')
    section = status.collect_status(path, CommandLine())['sections']['configuration']
    assert section['effective_profile'] == 'tracking'
    assert section['selected_file'] == str(profile)


def test_unidentified_endpoint_is_unknown(world):
    path, proc = world
    (proc / '101/fd/3').unlink()
    section = status.collect_status(path, CommandLine())['sections']['backend']
    assert section['state'] == 'unknown'
    assert 'no identified listening owner' in section['detail']


def test_unreadable_proc_is_unknown_not_absence(world):
    path, proc = world
    (proc / 'net/unix').unlink()
    section = status.collect_status(path, CommandLine())['sections']['backend']
    assert section['state'] == 'unknown'


def test_known_failing_mode_and_corrupt_playback_remain_distinct(world):
    path, proc = world
    card = write_card(proc, recorded(192000, 20))
    section = status.collect_status(path, CommandLine())['sections']['playback']
    assert section['state'] == 'unsupported'
    assert 'failed' in section['problem']
    (card / 'stream0').write_text('corrupt')
    section = status.collect_status(path, CommandLine())['sections']['playback']
    assert section['state'] == 'unknown'
    assert 'identity' in section['detail']


def test_no_playback_observation_is_not_a_default_rate(world):
    _, proc = world
    missing = status._playback(Config(), proc)
    assert missing['state'] == 'unobserved'
    assert 'no matching' in missing['detail']
    other = replace(Config(), usb_id='1234:5678')
    unmeasured = status._playback(other, proc)
    assert unmeasured['state'] == 'not-applicable'
    assert 'UCX II' in unmeasured['detail']


def test_service_environment_is_not_leaked_in_status(world, monkeypatch):
    path, _ = world
    monkeypatch.setattr(status, 'service_status', lambda:
                        {'state': 'observed', 'ActiveState': 'active', 'Environment': 'SECRET=x'})
    report = status.collect_status(path, CommandLine())
    assert 'SECRET' not in json.dumps(report)
    assert report['verification'] == 'not-performed'


@pytest.mark.parametrize('metadata', [
    {'version': '0.7.3', 'source_commit': 'b' * 40, 'backend_commit': 'a' * 40},
    [], {'version': float('nan')},
])
def test_native_provenance_comes_from_the_resolved_file(world, monkeypatch, metadata):
    path, _ = world
    binary = path.parent / 'usr/bin/oscmix'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'backend')
    manifest = path.parent / 'usr/share/oscmix-desk/package.json'
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(metadata))
    monkeypatch.setattr(status, 'resolve_binary', lambda *_: str(binary))
    info = status.collect_status(path, CommandLine())['sections']['installation']
    assert info['resolved_backend'] == str(binary)
    assert len(info['backend_sha256']) == 64
    if isinstance(metadata, dict):
        expected = metadata if isinstance(metadata['version'], str) else {
            'version': None, 'source_commit': None, 'backend_commit': None}
        assert info['package_metadata'] == expected
        assert info['metadata_file'] == str(manifest)
    else:
        assert info['package_metadata'] is None
        assert 'must be an object' in info['detail']
    json.dumps(info, allow_nan=False)


def test_missing_backend_file_is_diagnosed(world, monkeypatch):
    path, _ = world
    monkeypatch.setattr(status, 'resolve_binary', lambda *_: str(path.parent / 'gone'))
    info = status.collect_status(path, CommandLine())['sections']['installation']
    assert 'gone' in info['detail']


def test_incomplete_component_maintenance_and_failed_inspection_are_distinct(world):
    path, _ = world
    (path.parent / 'package-update').touch()
    (path.parent / 'service-update').write_text('{"stopped":true}')
    gtk = path.parent / 'gtk-package-update'
    gtk.symlink_to(gtk)
    info = status.collect_status(path, CommandLine())['sections']['installation']['maintenance']
    assert info['core']['pending'] is True
    assert info['gtk']['pending'] is None
    assert 'gtk-package-update' in info['gtk']['detail']
    assert info['service']['pending'] is True
    assert 'host service' in info['service']['detail']
    assert 'repair' in info['core']['detail']


@pytest.mark.parametrize('same', [False, True])
def test_running_binary_is_compared_with_resolved_binary(world, monkeypatch, same):
    path, proc = world
    selected = path.parent / 'new' / 'oscmix'
    selected.parent.mkdir()
    selected.write_bytes(b'new-backend')
    running = selected if same else path.parent / 'old' / 'oscmix'
    if not same:
        running.parent.mkdir()
        running.write_bytes(b'previous-backend')
    (proc / '101/exe').unlink()
    (proc / '101/exe').symlink_to(running)
    monkeypatch.setattr(status, 'resolve_binary', lambda *_: str(selected))
    info = status.collect_status(path, CommandLine())['sections']['backend']['running_file']
    assert info['state'] == 'observed'
    assert info['matches_resolved'] is same
    assert info['executable'] == str(running)
    expected_content = b'new-backend' if same else b'previous-backend'
    assert info['sha256'] == hashlib.sha256(expected_content).hexdigest()
    assert info['note']


def test_status_can_report_a_valid_installed_desktop(world, monkeypatch):
    path, _ = world
    monkeypatch.setattr(status, 'inspect_desktop', lambda: DesktopStatus('/gtk', None))
    assert status.collect_status(path, CommandLine())['sections']['desktop']['problem'] is None


@pytest.mark.parametrize('args', [
    ['--json'], ['--json', '--diff'], ['--status', '--profile', 'x'],
    ['--status', '--dry-run'], ['--status', '--snapshot'],
])
def test_status_json_and_actions_cannot_accidentally_apply(args):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2


def test_json_status_keeps_effective_configuration_identity(world, capsys):
    path, _ = world
    assert cli.main(['--config', str(path), '--status', '--json',
                     '--device', 'Fireface UCX II', '--osc-port', '9345']) == 0
    report = json.loads(capsys.readouterr().out)
    info = report['sections']['configuration']
    assert info['state'] == 'loaded'
    assert info['path'] == str(path)
    assert info['selected_file'] == str(path)
    assert info['device_name'] == 'Fireface UCX II'
    assert info['usb_id'] == '2a39:3fd9'
    assert info['configured_serial'] == '24216011'
    assert info['send_port'] == 9345
    assert info['receive_port'] == 8222
    assert 'detail' not in info
    assert report['configuration_valid'] is True
    assert report['desk_version'] == status.__version__
    assert report['note']


def test_status_retains_service_identity_but_excludes_private_environment(world, monkeypatch):
    path, _ = world
    public = dict(state='observed', manager='openrc', ActiveState='active',
                  UnitFileState='enabled', MainPID='1234', StatusText='discovering',
                  FragmentPath='/var/lib/oscmix-desk/service.json', detail='process inspection')
    monkeypatch.setattr(status, 'service_status', lambda:
                        {**public, 'Environment': 'PRIVATE=value', 'unrelated': 'hidden'})
    assert status.collect_status(path, CommandLine())['sections']['service'] == public


def test_installation_status_identifies_the_interpreter_and_absent_binary(world, monkeypatch):
    path, _ = world
    monkeypatch.setattr(status.sys, 'argv', ['/chosen/bin/oscmix-session', '--status'])
    info = status.collect_status(path, CommandLine())['sections']['installation']
    assert info['runtime'] == str(status.Path(status.__file__).resolve().parent)
    assert info['entry_point'] == '/chosen/bin/oscmix-session'
    assert info['python'] == status.sys.version.split()[0]
    assert info['resolved_backend'] is None
    assert info['backend_sha256'] is None
    assert info['package_metadata'] is None
    assert info['provenance_note']
    assert info['maintenance'] == {
        'core': {'pending': False}, 'gtk': {'pending': False}, 'service': {'pending': False}}


def test_status_resolves_and_hashes_the_explicit_backend_without_executing_it(world, monkeypatch):
    from oscmix_desk.discovery import resolve_binary

    path, _ = world
    binary = path.parent / 'chosen-backend'
    content = b'not an executable program; status must only read this file'
    binary.write_bytes(content)
    binary.chmod(0o700)
    monkeypatch.setenv('OSCMIX_BIN_BACKEND', str(binary))
    monkeypatch.setattr(status, 'resolve_binary', resolve_binary)
    info = status.collect_status(path, CommandLine())['sections']['installation']
    assert info['resolved_backend'] == str(binary)
    assert info['backend_sha256'] == hashlib.sha256(content).hexdigest()


def test_status_resolves_the_backend_on_path_without_executing_it(world, monkeypatch):
    from oscmix_desk.discovery import resolve_binary

    path, _ = world
    binary = path.parent / 'bin/oscmix'
    binary.parent.mkdir()
    binary.write_bytes(b'not executable code; inspect only')
    binary.chmod(0o700)
    monkeypatch.delenv('OSCMIX_BIN_BACKEND', raising=False)
    monkeypatch.setenv('HOME', str(path.parent / 'empty-home'))
    monkeypatch.setenv('PATH', str(binary.parent))
    monkeypatch.setattr(status, 'resolve_binary', resolve_binary)
    info = status.collect_status(path, CommandLine())['sections']['installation']
    assert info['resolved_backend'] == str(binary)
    assert info['backend_sha256'] == hashlib.sha256(binary.read_bytes()).hexdigest()


def test_status_hands_the_config_path_and_resolved_device_to_its_readers(world, monkeypatch):
    from pathlib import Path

    from oscmix_desk.diagnostics import BackendStatus
    from oscmix_desk.discovery import Device

    path, _ = world
    path.write_text('[device]\nname=Fireface UCX II\n')
    monkeypatch.delenv('OSCMIX_PROC_ROOT', raising=False)
    seen = []

    def inspect(config, proc_root, config_path):
        assert config.serial == ''
        assert proc_root == Path('/proc')
        assert config_path == path
        seen.append('backend')
        return BackendStatus('absent', 'not running', Device('2a39:3fd9', '24216011', 24))

    def playback(config, proc_root):
        assert config.serial == '24216011'
        assert proc_root == Path('/proc')
        seen.append('playback')
        return {'state': 'unobserved'}

    monkeypatch.setattr(status, 'backend_status', inspect)
    monkeypatch.setattr(status, '_playback', playback)
    report = status.collect_status(path, CommandLine())
    assert report['sections']['backend']['state'] == 'absent'
    assert seen == ['backend', 'playback']


def test_an_untrusted_backend_pid_does_not_become_a_running_file_identity(world, monkeypatch):
    from oscmix_desk.diagnostics import BackendStatus

    path, _ = world
    monkeypatch.setattr(status, 'backend_status', lambda *_:
                        BackendStatus('unknown', 'identity not established', pid=101))
    monkeypatch.setattr(status, '_running_backend', lambda *_:
                        pytest.fail('hashed an unidentified backend'))
    info = status.collect_status(path, CommandLine())['sections']['backend']
    assert info['state'] == 'unknown'
    assert info['pid'] == 101
    assert 'running_file' not in info


def test_unreadable_running_binary_keeps_the_inspection_error(world):
    path, proc = world
    # Endpoint/bridge identity is still observed; only hashing its kernel
    # executable reference fails after that process inspection.
    (proc / '101/exe').unlink()
    (proc / '101/exe').symlink_to(path.parent / 'missing/oscmix')
    info = status.collect_status(path, CommandLine())['sections']['backend']['running_file']
    assert info['state'] == 'unknown'
    assert 'No such file' in info['detail']
    assert 'matches_resolved' not in info


@pytest.mark.parametrize('active', [False, True])
def test_playback_status_retains_the_observed_card_and_serial(world, active):
    path, proc = world
    row = recorded()
    if not active:
        row = {**row, 'stream0': row['stream0'].replace('Status: Running', 'Status: Stop')}
    write_card(proc, row, number=2)
    write_card(proc, recorded(), number=7, serial='99887766')
    info = status.collect_status(path, CommandLine())['sections']['playback']
    assert info['state'] == ('observed' if active else 'idle')
    assert info['serial'] == '24216011'
    assert info['card'] == 2
    if active:
        assert info['problem'] is None
    else:
        assert info['detail']
