"""Run the GTK integration in an isolated GNOME/KDE Wayland or Xfce/X11 session."""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def run(args, env):
    return subprocess.run(args, env=env, capture_output=True, text=True, check=True, timeout=20)


def stop(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def session(args):
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    runtime = root / 'runtime'
    runtime.mkdir(mode=0o700)
    wayland = args.desktop != 'xfce'
    name = {'gnome': 'GNOME', 'kde': 'KDE', 'xfce': 'XFCE'}[args.desktop]
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime), HOME=str(root / 'home'),
               XDG_CONFIG_HOME=str(root / 'config'), XDG_DATA_HOME=str(root / 'data'),
               XDG_CACHE_HOME=str(root / 'cache'), XDG_CURRENT_DESKTOP=name,
               XDG_SESSION_TYPE='wayland' if wayland else 'x11',
               LIBGL_ALWAYS_SOFTWARE='1', NO_AT_BRIDGE='0')
    env.pop('WAYLAND_DISPLAY', None)
    env.pop('GDK_BACKEND', None)
    env.pop('SESSION_MANAGER', None)
    if args.desktop == 'gnome':
        executable = 'gnome-shell'
        command = [executable, '--wayland', '--wayland-display=wayland-test', '--no-x11']
        if '--nested' in run([executable, '--help'], env).stdout:
            command.append('--nested')
    elif args.desktop == 'kde':
        executable = 'kwin_wayland'
        command = [executable, '--x11-display=' + env['DISPLAY'], '--socket=wayland-test',
                   '--no-lockscreen', '--no-global-shortcuts']
    else:
        executable = 'xfce4-session'
        command = [executable]
    version = run([executable, '--version'], env).stdout.strip()
    children = []
    with (root / 'session.log').open('w') as log:
        try:
            children.append(subprocess.Popen(command, env=env, stdout=log, stderr=log))
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if children[0].poll() is not None:
                    raise AssertionError('desktop exited; see ' + str(root / 'session.log'))
                if wayland:
                    ready = (runtime / 'wayland-test').exists()
                else:
                    ready = 'window id #' in run(
                        ['xprop', '-root', '_NET_SUPPORTING_WM_CHECK'], env).stdout
                if ready:
                    break
                time.sleep(0.2)
            else:
                raise AssertionError('desktop startup timed out')
            env['GDK_BACKEND'] = 'wayland' if wayland else 'x11'
            if wayland:
                env['WAYLAND_DISPLAY'] = 'wayland-test'
            if args.desktop == 'kde':
                children.append(subprocess.Popen(['plasmashell', '--no-respawn'],
                    env=dict(env, QT_QPA_PLATFORM='wayland'), stdout=log, stderr=log))
                time.sleep(2)
                assert children[-1].poll() is None, 'Plasma shell failed'
            run(['dbus-update-activation-environment', 'DISPLAY', 'XDG_RUNTIME_DIR',
                 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_TYPE'], env)
            result = subprocess.run([
                sys.executable, 'tests/gtk_lifecycle.py', '--gtk', '/usr/bin/oscmix-gtk',
                '--schema', '/usr/share/glib-2.0/schemas/oscmix.gschema.xml',
                '--output', str(root / 'gtk')], env=env, capture_output=True,
                text=True, timeout=90)
            (root / 'gtk-transcript.log').write_text(result.stdout + result.stderr)
            assert result.returncode == 0, result.stdout + result.stderr
        finally:
            for child in reversed(children):
                stop(child)
    report = dict(desktop=name, version=version, command=command, ok=True,
                  rendering='software, nested in Xvfb; GTK uses ' + env['GDK_BACKEND'],
                  gtk=json.loads((root / 'gtk/result.json').read_text()))
    (root / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)


def main():
    if not Path('/.dockerenv').is_file() or os.environ.get('OSCMIX_QUALIFY_DESKTOP') != '1':
        raise RuntimeError('requires a disposable container and explicit desktop qualification')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop', choices=['gnome', 'kde', 'xfce'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    session(parser.parse_args())


if __name__ == '__main__':
    main()
