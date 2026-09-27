"""Exercise publication transitions over actual loopback HTTP in the tool container."""

import functools
import hashlib
import http.server
import importlib.util
import json
import shutil
import threading
import urllib.error
from pathlib import Path

SPEC = importlib.util.spec_from_file_location(
    'repository_publication',
    Path(__file__).resolve().parents[1] / 'scripts/repository-publication.py')
PUBLISHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PUBLISHER)


class Server(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.server.fault == 'stale-304' and self.headers.get('If-None-Match'):
            self.send_response(304)
            self.end_headers()
            return
        if self.server.fault == 'partial' and self.path == '/opensuse16/repository.json':
            data = b'truncated deployment'
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()

    def end_headers(self):
        path = Path(self.translate_path(self.path))
        if path.is_file():
            self.send_header('ETag', '"' + hashlib.sha256(path.read_bytes()).hexdigest() + '"')
        super().end_headers()


def exercise(root):
    output = root / 'http-transitions'
    output.mkdir()
    served = output / 'served'
    first = root / 'first-verified'
    second = root / 'second-verified'
    served.symlink_to(first, target_is_directory=True)
    server = http.server.ThreadingHTTPServer(
        ('127.0.0.1', 0), functools.partial(Server, directory=str(served)))
    server.fault = None
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    original_base = PUBLISHER.BASE
    PUBLISHER.BASE = 'http://127.0.0.1:%d/' % server.server_port
    record = dict(schema=1, production_key_used=False, published_https=False,
                  transport='actual loopback HTTP', hardware=False, checks={})
    try:
        digest = PUBLISHER.SITE.BUILDER.sha(first / 'publication.json')
        previous = PUBLISHER.transition(second, digest)
        assert len(previous['conditional_requests']) == 5
        record['checks']['complete-predecessor-checked'] = True
        prepared = dict(verified=dict(sha256=PUBLISHER.SITE.BUILDER.sha(root / 'second.tar.gz')),
                        transition=previous)
        recheck = output / 'recheck'
        shutil.copytree(second, recheck / 'site')
        (recheck / 'prepared.json').write_text(json.dumps(prepared))
        assert PUBLISHER.recheck(recheck)['transition']['previous'] == digest
        record['checks']['freshness-and-predecessor-rechecked'] = True
        served.unlink()
        served.symlink_to(second, target_is_directory=True)
        try:
            PUBLISHER.transition(second, digest)
        except ValueError:
            record['checks']['stale-promotion-refused'] = True
        else:
            raise AssertionError('stale predecessor was accepted')
        for case in ('complete', 'partial', 'stale-304'):
            directory = output / case
            directory.mkdir()
            shutil.copytree(second, directory / 'site')
            (directory / 'prepared.json').write_text(json.dumps(prepared))
            server.fault = None if case == 'complete' else case
            if case == 'complete':
                checked = PUBLISHER.verify_remote(directory)
                assert checked['passed']
                assert checked['published_https'] is False
                assert len(checked['conditional_requests']) == 10
                record['checks']['all-published-files-checked'] = len(checked['files'])
                record['checks']['changed-native-validator-responses'] = 10
            else:
                try:
                    PUBLISHER.verify_remote(directory)
                except (ValueError, urllib.error.HTTPError):
                    assert not (directory / 'https-verification.json').exists()
                    record['checks'][case + '-refused'] = True
                else:
                    raise AssertionError(case + ' was accepted')
        record['result'] = 'passed'
    finally:
        PUBLISHER.BASE = original_base
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        (output / 'result.json').write_text(json.dumps(record, indent=2) + '\n')
    return record
