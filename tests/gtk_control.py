"""Real upstream GTK + coordinated C backend over simulated MIDI.

Requires a private dbus-run-session and Xvfb; no ALSA, USB or PCM is opened.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import gi

gi.require_version('Atspi', '2.0')
from control_peer import BEGIN, BUSY, END, GUI, OK, REFRESH, SimulatedMidi, wait_for  # noqa: E402
from gi.repository import Atspi, GLib  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def descendants(root=None):
    pending = [Atspi.get_desktop(0) if root is None else root]
    visited = 0
    while pending and visited < 15000:
        item = pending.pop()
        if item is None:
            continue
        visited += 1
        try:
            yield item
            pending.extend(item.get_child_at_index(index)
                           for index in range(item.get_child_count()))
        except GLib.Error:
            continue


def label(text):
    for item in descendants():
        try:
            if item.get_name() == text:
                return item
        except GLib.Error:
            continue
    return None


def fader(value):
    for item in descendants():
        try:
            if item.get_role() == Atspi.Role.SPIN_BUTTON:
                interface = item.get_value_iface()
                if interface and abs(interface.get_current_value() - value) < 0.01:
                    return item
        except GLib.Error:
            continue
    return None


def open_gtk(args, midi, name, pid=None):
    env = dict(os.environ, OSCMIX_CONTROL_SOCKET=str(midi.path),
               OSCMIX_BACKEND_PID=str(midi.child.pid if pid is None else pid),
               GSETTINGS_BACKEND='memory', GTK_MODULES='gail:atk-bridge',
               GIO_USE_VFS='local',
               XDG_CONFIG_HOME=str(args.output / 'config'),
               XDG_DATA_HOME=str(args.output / 'data'), HOME=str(args.output / 'home'))
    env.pop('NO_AT_BRIDGE', None)
    env.pop('GTK_USE_PORTAL', None)
    log = (args.output / (name + '.log')).open('w')
    child = subprocess.Popen([str(args.gtk.resolve())], env=env, stdout=log,
                             stderr=subprocess.STDOUT)
    return child, log


def close_gtk(child, log):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
            raise AssertionError('GTK did not stop') from None
    log.close()


def exercise(args):
    midi = SimulatedMidi(args.backend.resolve(), args.output / 'c.sock')
    desk = midi.connect()
    child, log = open_gtk(args, midi, 'gui-first')
    results = {}
    try:
        wait_for(lambda: (0x3e04, 0x67cd) in midi.registers(), timeout=8)
        midi.inject((0x600, -330), (0x3065, 5))
        wait_for(lambda: label('Connected'), timeout=12)
        control = wait_for(lambda: fader(-33))
        assert control.get_state_set().contains(Atspi.StateType.SENSITIVE)
        assert control.get_value_iface().set_current_value(-40)
        wait_for(lambda: (0x600, 0xfe70) in midi.registers())
        results['gtk_edit'] = 'output 5 -40 dB emitted through real backend'
        assert desk.request(BEGIN)[2] == OK
        wait_for(lambda: label('Desk is applying settings; controls are read-only'))
        assert not control.get_state_set().contains(Atspi.StateType.SENSITIVE)
        before = midi.registers()
        control.get_value_iface().set_current_value(-41)
        competitor = midi.connect()
        try:
            assert competitor.request(BEGIN)[2] == BUSY
        finally:
            competitor.close()
        # A separate refresh consumer is also bounded by the complete operation.
        gui = midi.connect(GUI)
        try:
            assert gui.request(REFRESH)[2] == BUSY
        finally:
            gui.close()
        time.sleep(0.2)
        assert midi.registers() == before
        midi.inject((0x600, -400))
        wait_for(lambda: fader(-40))
        assert desk.request(END)[2] == OK
        wait_for(lambda: midi.registers().count((0x3e04, 0x67cd)) == 2, timeout=12)
        midi.inject((0x600, -400), (0x3065, 5))
        wait_for(lambda: label('Connected'), timeout=12)
        assert control.get_state_set().contains(Atspi.StateType.SENSITIVE)
        assert midi.registers().count((0x600, 0xfe66)) == 0
        results['whole_operation'] = (
            'GTK disabled; competing desk refused; rejected edit not replayed')
        close_gtk(child, log)
        assert desk.request(BEGIN)[2] == OK
        child, log = open_gtk(args, midi, 'desk-first')
        wait_for(lambda: label('Desk is applying settings; controls are read-only'), timeout=8)
        assert child.poll() is None
        assert desk.request(END)[2] == OK
        wait_for(lambda: midi.registers().count((0x3e04, 0x67cd)) == 3, timeout=12)
        midi.inject((0x600, -310), (0x3065, 5))
        wait_for(lambda: label('Connected'), timeout=12)
        wait_for(lambda: fader(-31))
        results['desk_first'] = (
            'GTK subscribed while lease held; received fresh state after release')
        midi.child.terminate()
        midi.child.wait(timeout=3)
        wait_for(lambda: label('Disconnected; reopen mixer to connect'))
        assert child.poll() is None
        results['backend_loss'] = 'visible disconnected state; no automatic replay'
    except Exception:
        visible = []
        for item in descendants():
            try:
                name = item.get_name()
                if name:
                    visible.append((item.get_role_name(), name))
            except GLib.Error:
                continue
        (args.output / 'accessible-failure.json').write_text(json.dumps(visible, indent=2))
        raise
    finally:
        close_gtk(child, log)
        desk.close()
        midi.close()
    # Wrong PID is rejected before HELLO/refresh, even at the correct pathname.
    other = SimulatedMidi(args.backend.resolve(), args.output / 'other.sock')
    probe = other.connect()
    child, log = open_gtk(args, other, 'wrong-identity', pid=os.getpid())
    try:
        wait_for(lambda: label('Disconnected; reopen mixer to connect'), timeout=8)
        assert other.registers() == []
        results['identity'] = 'mismatched kernel peer PID refused before refresh or writes'
    finally:
        close_gtk(child, log)
        probe.close()
        other.close()
    return results


def main():
    if os.environ.get('OSCMIX_QUALIFY_DESKTOP') != '1':
        raise RuntimeError('requires explicit opt-in and a private display/bus')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--gtk', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.mkdir(exist_ok=False, parents=True)
    # Enable the private accessibility bus explicitly; the user's desktop
    # preference must not decide whether this qualification observes a window.
    for prop in ('IsEnabled', 'ScreenReaderEnabled'):
        subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.a11y.Bus',
                        '--object-path', '/org/a11y/bus', '--method',
                        'org.freedesktop.DBus.Properties.Set', 'org.a11y.Status',
                        prop, '<true>'], check=True, capture_output=True)
    result = exercise(args)
    (args.output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
