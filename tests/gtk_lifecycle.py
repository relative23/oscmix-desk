"""Opt-in desktop integration against the real GTK executable and a private fake mixer.

Run inside a private dbus-run-session and display, never the user's desktop bus:
  OSCMIX_QUALIFY_DESKTOP=1 dbus-run-session -- xvfb-run -a python3 tests/gtk_lifecycle.py \
      --gtk /path/to/oscmix-gtk --schema /path/to/oscmix.gschema.xml --output /tmp/new-result
Needs python3-gi/Atspi, the accessibility bridge, gsettings and the schema compiler.
No ALSA/MIDI/PCM interface is opened. Ports, settings, config and procfs are private.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from support import fake_proc, free_udp_port

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from oscmix_desk import backend, config, osc, outcome, profiles, verify  # noqa: E402


def wait_for(predicate, description, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError('timed out: ' + description)


class Mixer(threading.Thread):
    def __init__(self, reply_port):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.reply_port = reply_port
        self.messages = []
        self.values = {'/output/5/volume': ('f', (-20.,)),
                       '/output/7/volume': ('f', (-9.,)),
                       '/clock/samplerate': ('i', (96000,))}
        self.done = threading.Event()
        self.failure = None

    def run(self):
        try:
            while not self.done.is_set():
                try:
                    data, _ = self.sock.recvfrom(65536)
                except socket.timeout:
                    continue
                path, tags, args = osc.decode_osc(data)
                self.messages.append((path, tags, args))
                if path == '/refresh':
                    for key, (types, values) in list(self.values.items()):
                        self.sock.sendto(osc.encode_osc(key, types, *values),
                                         ('127.0.0.1', self.reply_port))
                else:
                    self.values[path] = tags, tuple(args)
        except Exception as exc:  # retain background errors as qualification failures
            self.failure = repr(exc)

    def close(self):
        self.done.set()
        self.join(timeout=5)
        self.sock.close()
        assert not self.is_alive()
        assert self.failure is None, self.failure


def received_label(text):
    # GI is a qualification dependency, never part of desk's runtime.
    import gi
    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi, GLib

    pending = [Atspi.get_desktop(0)]
    visited = 0
    while pending and visited < 15000:
        item = pending.pop()
        if item is None:
            continue
        visited += 1
        try:
            if item.get_name() == text:
                return True
            pending.extend(item.get_child_at_index(index)
                           for index in range(item.get_child_count()))
        except GLib.Error:
            continue
    return False


def setup(args, mixer):
    work = args.output.resolve()
    work.mkdir(parents=True, exist_ok=False)
    schema = work / 'schema'
    schema.mkdir()
    shutil.copy2(args.schema, schema / 'oscmix.gschema.xml')
    subprocess.run(['glib-compile-schemas', str(schema)], check=True)
    env = dict(os.environ, HOME=str(work / 'home'), XDG_CONFIG_HOME=str(work / 'config'),
               XDG_DATA_HOME=str(work / 'data'), GSETTINGS_SCHEMA_DIR=str(schema),
               GSETTINGS_BACKEND='keyfile', OSCMIX_NO_NOTIFY='1',
               OSCMIX_BIN_GTK=str(args.gtk.resolve()), GTK_MODULES='gail:atk-bridge')
    env.pop('NO_AT_BRIDGE', None)
    guard = work / 'bin'
    guard.mkdir()
    (guard / 'systemctl').write_text('#!/bin/sh\necho UNEXPECTED_SERVICE_CALL >&2\nexit 99\n')
    (guard / 'systemctl').chmod(0o755)
    env['PATH'] = str(guard) + os.pathsep + env['PATH']
    for key, value in [('send-host', '127.0.0.1'), ('recv-host', '127.0.0.1'),
                       ('send-port', str(mixer.port)), ('recv-port', str(mixer.reply_port))]:
        subprocess.run(['gsettings', 'set', 'oscmix', key, value], check=True, env=env)
    desk = work / 'routing.conf'
    desk.write_text('[osc]\nport=%d\nrecv-port=%d\n'
                    '[output:5]\nvolume=-6\n[output:7]\nvolume=-9\n' % (
                        mixer.port, mixer.reply_port))
    (work / 'profiles').mkdir()
    (work / 'profiles/levels.conf').write_text('[output:5]\nvolume=-6\n[output:7]\nvolume=-9\n')
    env['OSCMIX_CONFIG'] = str(desk)
    (work / 'runtime').mkdir(mode=0o700)
    os.environ.update(OSCMIX_LOCK_DIR=str(work / 'absent-shared-lock'),
                      XDG_RUNTIME_DIR=str(work / 'runtime'))
    return work, env, desk


def proc_state(work, env, mixer, name, busy):
    proc = fake_proc(work / name, boxes=[(42, '00000000')],
                     bound=[(mixer.port, 'oscmix', 42)] +
                     ([(mixer.reply_port, 'python3', None)] if busy else []))
    (proc / '40000/cmdline').write_bytes(
        b'oscmix\0-s\0' + ('udp!127.0.0.1!%d' % mixer.reply_port).encode() + b'\0')
    env['OSCMIX_PROC_ROOT'] = str(proc)
    os.environ['OSCMIX_PROC_ROOT'] = str(proc)


def open_gui(work, env, mixer, name):
    proc_state(work, env, mixer, name + '-proc', False)
    log = (work / (name + '.log')).open('w')
    child = subprocess.Popen([sys.executable, str(ROOT / 'bin/oscmix-launch')],
                             env=env, stdout=log, stderr=subprocess.STDOUT)
    return child, log


def close_gui(child, log):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
    log.close()


def exercise(args, mixer):
    work, env, desk = setup(args, mixer)
    model = config.load_config(desk)
    device = backend.Backend('127.0.0.1', mixer.port, mixer.reply_port)
    verify.DUMP_LISTEN_SETTLE = 0.02
    verify.VERIFY_TIMEOUT = 0.3
    profiles.VERIFY_TIMEOUT = 0.3
    mixer.start()
    child, log = open_gui(work, env, mixer, 'gui-first')
    try:
        wait_for(lambda: received_label('96000 Hz'), 'GTK displays the fake OSC sample rate')
        assert child.poll() is None
        before = len(mixer.messages)
        assert verify.verify_routing(verify.expected_registers(model), mixer.port,
                                      mixer.reply_port, backend=device) is None
        assert verify.reconcile_now(model, 'GUI owns replies', backend=device) is False
        assert len(mixer.messages) == before, 'busy reconcile sent a write/request'
        result = profiles.switch_profile('levels', desk, backend=device)
        assert result.state == outcome.APPLIED_UNVERIFIED
        assert result.read_back is False
        wait_for(lambda: mixer.values['/output/5/volume'] == ('f', (-6.,)), 'profile applied')
        mixer.values['/output/5/volume'] = ('f', (-20.,))  # later manual fader adjustment
    finally:
        close_gui(child, log)
    before = len(mixer.messages)
    assert verify.reconcile_now(model, 'deliberate retry after GUI close', backend=device)
    wait_for(lambda: len(mixer.messages) >= before + 2, 'refresh and one permitted fader write')
    writes = [m for m in mixer.messages[before:] if m[0] != '/refresh']
    assert writes == [('/output/7/volume', 'f', (-9.,))], writes
    assert mixer.values['/output/5/volume'] == ('f', (-20.,))
    listener = device.listen()
    assert listener is not None
    try:
        proc_state(work, env, mixer, 'desk-first-proc', True)
        refused = subprocess.run([sys.executable, str(ROOT / 'bin/oscmix-launch')],
                                 env=env, capture_output=True, text=True, timeout=10)
        assert refused.returncode == 1
        assert str(mixer.reply_port) in refused.stderr
        assert 'occupied' in refused.stderr
    finally:
        listener.close()
    child, log = open_gui(work, env, mixer, 'gui-after-readback')
    try:
        wait_for(lambda: received_label('96000 Hz'), 'GTK receives after desk releases replies')
        assert child.poll() is None
    finally:
        close_gui(child, log)
    receiver = device.listen()
    assert receiver is not None, 'GUI left a listener behind'
    receiver.close()
    return dict(ok=True, backend='isolated UDP fake', gtk=str(args.gtk.resolve()),
                gui_first='read-back busy; reconcile wrote nothing; profile applied unverified',
                recovery='REMEMBER fader preserved', desk_first='launcher refused busy receiver',
                gui_reopened=True, accessible_received_label='96000 Hz', shutdown='SIGTERM',
                messages=mixer.messages, display_backend=env.get('GDK_BACKEND'),
                desktop=env.get('XDG_CURRENT_DESKTOP'))


def main():
    if os.environ.get('OSCMIX_QUALIFY_DESKTOP') != '1':
        raise RuntimeError('requires explicit opt-in and an isolated desktop bus/display')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gtk', type=Path, required=True)
    parser.add_argument('--schema', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    mixer = Mixer(free_udp_port())
    try:
        report = exercise(args, mixer)
    except Exception:
        for transcript in sorted(args.output.glob('*.log')):
            print(str(transcript) + '\n' + transcript.read_text(), file=sys.stderr, flush=True)
        raise
    finally:
        if mixer.ident is not None:
            mixer.close()
        else:
            mixer.sock.close()
    (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
