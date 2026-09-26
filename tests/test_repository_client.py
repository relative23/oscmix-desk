"""Extra repository authentication must fail closed without changing other trust."""

import importlib.util
import io
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
