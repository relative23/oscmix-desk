"""Extra repository authentication must fail closed without changing other trust."""

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

DIRECTORY = Path(__file__).resolve().parents[1] / 'packaging/repository'
PRIMARY = 'A' * 40
SIGNER = 'B' * 40
VALID = ('[GNUPG:] GOODSIG ' + SIGNER[-16:] + ' Test key\n'
         '[GNUPG:] VALIDSIG ' + SIGNER + ' 2026-09-25 100 0 4 0 1 8 00 ' + PRIMARY + '\n')


def load(name):
    spec = importlib.util.spec_from_file_location('repository_' + name, DIRECTORY / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def verifier():
    return load('verify')


@pytest.fixture
def apt(monkeypatch, verifier):
    # The installed method imports the fixed sibling. Restore global module
    # search state after loading the real entry point in this test process.
    monkeypatch.setitem(sys.modules, 'verify', verifier)
    monkeypatch.setattr(sys, 'path', list(sys.path))
    return load('apt-method')


def test_current_signature_and_unrelated_expired_subkey(verifier):
    assert verifier.signature_status(VALID, PRIMARY, 1000) == SIGNER
    assert verifier.signature_status('[GNUPG:] KEYEXPIRED 12\n' + VALID, PRIMARY, 1000) == SIGNER


@pytest.mark.parametrize('status', [
    '', VALID.replace('GOODSIG', 'EXPKEYSIG'), VALID.replace('GOODSIG', 'REVKEYSIG'),
    VALID + VALID, VALID + '[GNUPG:] BADSIG 123 another\n',
    VALID + '[GNUPG:] NO_PUBKEY 123\n', VALID + '[GNUPG:] FAILURE gpgv 1\n',
    VALID.replace(PRIMARY, 'C' * 40), VALID.replace(' 8 00 ', ' 2 00 '),
    VALID.replace(' 100 0 ', ' 1400 0 '), VALID.replace(' 100 0 ', ' 100 1000 '),
    VALID.replace(' 100 0 ', ' invalid 0 '),
    VALID.replace(' ' + PRIMARY, ''), VALID.replace('VALIDSIG ' + SIGNER, 'VALIDSIG bad'),
])
def test_refuses_noncurrent_untrusted_weak_or_malformed_signature(verifier, status):
    with pytest.raises(ValueError, match=r'metadata|invalid literal'):
        verifier.signature_status(status, PRIMARY, 1000)


@pytest.mark.parametrize('inline', [False, True])
def test_apt_checks_the_exact_native_signature_and_data_files(apt, monkeypatch, inline):
    calls = []
    monkeypatch.setattr(apt, 'verify', lambda *args: calls.append(args))
    signature = '/cache/a%20b_InRelease' if inline else '/cache/a%20b_Release.gpg'
    source = '/cache/a b_InRelease' if inline else '/cache/a b_Release'
    apt.check_request(('600 URI Acquire\nURI: sqv:' + signature + '\nFilename: ' + source +
                       '\nSigned-By: ' + str(apt.CERTIFICATE) + '\n\n').encode())
    assert calls == [(Path(signature.replace('%20', ' ')), None if inline else Path(source))]


def test_apt_delegates_other_repositories_even_with_repeated_unrelated_headers(apt, monkeypatch):
    monkeypatch.setattr(apt, 'verify', lambda *_: pytest.fail('other repository changed'))
    apt.check_request(b'600 URI Acquire\nURI: sqv:/cache/other_InRelease\n'
                      b'Filename: /cache/other_InRelease\nSigned-By: /keys/distribution.gpg\n'
                      b'Target-Source: first\nTarget-Source: second\n\n')


def test_apt_rejects_ambiguous_project_verification_request(apt):
    with pytest.raises(ValueError, match='duplicate'):
        apt.check_request(('600 URI Acquire\nSigned-By: ' + str(apt.CERTIFICATE) +
                           '\nURI: sqv:/first\nURI: sqv:/second\n\n').encode())


def test_apt_frame_preserves_bytes_and_bounds_truncated_or_large_input(apt):
    content = b'601 Configuration\nConfig-Item: one\nConfig-Item: two\n\n'
    stream = io.BytesIO(content)
    assert apt.frame(stream) == content
    assert apt.frame(stream) == b''
    with pytest.raises(ValueError, match='truncated'):
        apt.frame(io.BytesIO(content[:-1]))
    with pytest.raises(ValueError, match='oversized'):
        apt.frame(io.BytesIO(b'x' * (apt.MAX_FRAME + 1)))


@pytest.mark.parametrize('kind', ['file', 'directory', 'dangling-symlink'])
def test_unfinished_client_replacement_blocks_verification_first(
        verifier, monkeypatch, tmp_path, kind):
    fence = tmp_path / 'update.json'
    if kind == 'file':
        fence.write_text('interrupted, even if incomplete JSON')
    elif kind == 'directory':
        fence.mkdir()
    else:
        fence.symlink_to(tmp_path / 'absent')
    monkeypatch.setattr(verifier, 'FENCE', fence)
    monkeypatch.setattr(verifier, 'trusted_file',
                        lambda *_: pytest.fail('maintenance read trust or metadata'))
    with pytest.raises(ValueError, match='maintenance'):
        verifier.verify(tmp_path / 'InRelease')


@pytest.fixture
def subscription(monkeypatch, tmp_path):
    module = load('configure')
    # Native root ownership is exercised in the disposable real-manager tests.
    # Here inject only the filesystem boundary to fault individual journal steps.
    monkeypatch.setattr(module, 'root_path', lambda path, **_: path)
    monkeypatch.setattr(module, 'FENCE', tmp_path / 'update.json')
    monkeypatch.setattr(module, 'REGISTRATION', tmp_path / 'state.json')
    monkeypatch.setattr(module, 'SOURCES', {'apt': tmp_path / 'project.sources'})
    monkeypatch.setattr(module, 'HOOKS', {'apt': tmp_path / 'method.conf'})
    return module


def test_interrupted_definition_removal_keeps_the_fence_and_unowned_file(subscription):
    source = subscription.SOURCES['apt']
    source.write_text('registered project source')
    foreign = subscription.HOOKS['apt']
    foreign.write_text('unregistered administrator configuration')
    record = dict(schema=1, enabled=True,
                  files={str(source): subscription.sha(source.read_bytes())})
    subscription.write_json(subscription.REGISTRATION, record)
    with pytest.raises(ValueError, match='unregistered'):
        subscription.begin(record)
    assert not source.exists()
    assert foreign.read_text() == 'unregistered administrator configuration'
    assert subscription.FENCE.exists()
    assert subscription.registration() == record


def test_modified_owned_definition_is_preserved_and_stays_disabled(subscription):
    source = subscription.SOURCES['apt']
    source.write_text('administrator change')
    record = dict(schema=1, enabled=True, files={str(source): subscription.sha(b'original')})
    subscription.begin(record)
    assert not source.exists()
    backups = list(source.parent.glob(source.name + '.saved-*'))
    assert len(backups) == 1
    assert backups[0].read_text() == 'administrator change'
    assert subscription.registration()['enabled'] is False
    assert subscription.FENCE.exists()


def test_definition_ownership_survives_an_interrupted_first_subscription(
        subscription, monkeypatch):
    source = subscription.SOURCES['apt']
    record = subscription.registration()
    atomic = subscription.atomic

    def failed(path, data):
        if path == source:
            raise OSError('simulated full filesystem')
        atomic(path, data)

    monkeypatch.setattr(subscription, 'atomic', failed)
    with pytest.raises(OSError, match='full filesystem'):
        subscription.define(record, source, 'source definition')
    assert not source.exists()
    assert json.loads(subscription.REGISTRATION.read_text())['files'] == {
        str(source): subscription.sha(b'source definition')}
    monkeypatch.setattr(subscription, 'atomic', atomic)
    subscription.define(subscription.registration(), source, 'source definition')
    assert source.read_text() == 'source definition'


@pytest.mark.parametrize('action', ['enable', 'refresh-key', 'package-begin',
                                  'package-finish', 'native-key-sync'])
def test_ostree_refuses_subscription_before_touching_host_state(
        subscription, monkeypatch, tmp_path, capsys, action):
    marker = tmp_path / 'ostree-booted'
    marker.touch()
    monkeypatch.setattr(subscription, 'OSTREE_BOOTED', marker)
    monkeypatch.setattr(subscription.os, 'getuid', lambda: 0)
    monkeypatch.setattr(subscription, 'root_path',
                        lambda *_args, **_kwargs: pytest.fail('refusal touched host state'))
    monkeypatch.setattr(sys, 'argv', ['oscmix-repository', action, '--certificate',
                                     '/unread/public.asc', '--fingerprint', PRIMARY])
    assert subscription.main() == 1
    assert 'rpm-ostree does not run these verification hooks' in capsys.readouterr().err
    assert not subscription.REGISTRATION.exists()
    assert not subscription.FENCE.exists()


@pytest.mark.parametrize('action', ['disable', 'package-remove'])
def test_ostree_can_remove_an_existing_subscription(
        subscription, monkeypatch, tmp_path, action):
    marker = tmp_path / 'ostree-booted'
    marker.touch()
    monkeypatch.setattr(subscription, 'OSTREE_BOOTED', marker)
    monkeypatch.setattr(subscription, 'STATE', tmp_path)
    monkeypatch.setattr(subscription, 'PENDING_KEY', tmp_path / 'native-key.json')
    monkeypatch.setattr(subscription.os, 'getuid', lambda: 0)
    source = subscription.SOURCES['apt']
    source.write_text('old subscription')
    subscription.write_json(subscription.REGISTRATION, dict(
        schema=1, enabled=True, files={str(source): subscription.sha(source.read_bytes())}))
    monkeypatch.setattr(sys, 'argv', ['oscmix-repository', action])
    assert subscription.main() == 0
    assert not source.exists()
    assert subscription.registration()['enabled'] is False
    assert not subscription.FENCE.exists()
