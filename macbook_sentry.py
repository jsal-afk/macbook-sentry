#!/usr/bin/env python3
"""Install and operate MacBook Sentry from the Mac's local Terminal."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
from socketserver import BaseRequestHandler, UnixStreamServer
import stat
import sys
from typing import cast
import subprocess
import tempfile
import threading
import time

import apple_shortcuts
import archive
from controller import Controller
from presence_receiver import Receiver
import state
import telegram_bot

ROOT = state.ROOT
SOCK = 'control.sock'
LOCAL = ('status', 'arm', 'away', 'home', 'pause', 'stop')
OWNER = re.compile(r'[!-~]{1,200}')
HOST = re.compile(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){2}ts\.net')


def load(path):
    data = state.read(path/'config.json')
    if data.get('version') != 1:
        raise ValueError('Unsupported configuration version')
    for key, lo, hi in (('retention_hours', 1, 720), ('away_lease_seconds', 15, 86400),
                        ('person_confidence', .01, 1), ('listen_port', 1024, 65535)):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not lo <= value <= hi:
            raise ValueError(f'Invalid {key}: expected {lo}–{hi}')
    if (type(data['listen_port']) is not int or type(data.get('detection')) is not bool
            or not isinstance(data.get('camera_input'), str) or not data['camera_input'].endswith(':none')):
        raise ValueError('Invalid detection, listen port, or video-only camera selection')
    for key in ('ffmpeg', 'ffprobe', 'microphone_capture', 'person_weights'):
        if not isinstance(data.get(key), str) or not Path(data[key]).is_absolute():
            raise ValueError(f'Invalid {key}: expected absolute path')
    archive_fields = {'volume', 'volume_uuid', 'archive_name', 'archive_owner'}
    if archive_fields.intersection(data) and not all(isinstance(data.get(k), str) for k in archive_fields):
        raise ValueError('Incomplete archive binding')
    phone_fields = {'phone_owner', 'phone_host', 'phone_secret'}
    if phone_fields.intersection(data) and not (
            all(isinstance(data.get(k), str) for k in phone_fields) and OWNER.fullmatch(data['phone_owner'])
            and HOST.fullmatch(data['phone_host']) and re.fullmatch(r'[0-9a-f]{64}', data['phone_secret'])):
        raise ValueError('Invalid phone configuration; rerun phone-setup')
    return data


def init(path):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('Use native Apple-silicon macOS Python')
    ffmpeg, ffprobe = (shutil.which(n) for n in ('ffmpeg', 'ffprobe'))
    if not ffmpeg or not ffprobe:
        raise RuntimeError('Install FFmpeg with Homebrew and add it to PATH')
    mic = path/'microphone-capture'
    with tempfile.TemporaryDirectory(dir=path) as tmp:
        compiled = Path(tmp)/mic.name
        subprocess.run(['/usr/bin/swiftc', '-O', '-module-cache-path', tmp,
                        '-Xlinker', '-sectcreate', '-Xlinker', '__TEXT', '-Xlinker', '__info_plist',
                        '-Xlinker', str(ROOT/'MicrophoneInfo.plist'),
                        str(ROOT/'MicrophoneCapture.swift'), '-o', str(compiled)], check=True)
        compiled.chmod(0o700)
        compiled.replace(mic)
    config = path/'config.json'
    if not config.exists() and not config.is_symlink():
        state.write(config, dict(version=1, ffmpeg=ffmpeg, ffprobe=ffprobe, microphone_capture=str(mic),
                                 person_weights=str(path/'rf-detr-nano.pth'), detection=False,
                                 camera_input='0:none', person_confidence=.45, retention_hours=24,
                                 away_lease_seconds=14400, listen_port=18765))
    load(path)
    print('Initialized without starting Sentry:', path)


def preflight(path, config):
    if not config.get('volume'):
        raise RuntimeError('Enroll the selected SSD first with archive')
    with archive.open_archive(config) as root:
        from sentry import check_space
        check_space(root)
    for key in ('ffmpeg', 'ffprobe', 'microphone_capture'):
        if not os.access(config[key], os.X_OK):
            raise RuntimeError('Missing executable: ' + key)
    if config['detection']:
        from person_detector import verify_weights
        verify_weights(Path(config['person_weights']))
        telegram_bot.validate_binding(state.read(path/'bot.json'))


class LocalHandler(BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        with self.request.makefile('rb') as stream:
            command = stream.readline(64).strip().decode('ascii')
        result = cast(LocalServer, self.server).dispatch(command)
        self.request.sendall(json.dumps(result).encode() + b'\n')


class LocalServer(UnixStreamServer):
    def __init__(self, dispatch):
        self.dispatch = dispatch
        super().__init__(SOCK, LocalHandler)

    def handle_error(self, request, client_address):
        pass


def serve(path, config, lock, test):
    controller = Controller(config, lock, test=test)
    stopped = threading.Event()

    def dispatch(command):
        error = None
        try:
            if command == 'arm':
                if test:
                    raise RuntimeError('Cannot arm in test mode')
                preflight(path, config)
                controller.arm()
            elif command in ('away', 'home'):
                error = controller.receive(command, time.time())
            elif command in ('pause', 'stop'):
                controller.pause()
                if command == 'stop':
                    stopped.set()
            elif command != 'status':
                error = 'unknown_command'
        except (ValueError, OSError, RuntimeError) as exc:
            error = message(exc)
        return controller.status() | {'command_error': error}

    def listen(server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1})
        thread.start()
        stack.callback(thread.join)
        stack.callback(server.shutdown)

    try:
        existing = Path(SOCK)
        if existing.exists() or existing.is_symlink():
            if not stat.S_ISSOCK(existing.lstat().st_mode):
                raise RuntimeError('Unsafe control socket path')
            existing.unlink()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stopped.set())
        with ExitStack() as stack:
            local = stack.enter_context(LocalServer(dispatch))
            stack.callback(existing.unlink, missing_ok=True)
            if config.get('phone_owner'):
                server = stack.enter_context(Receiver(config['listen_port'], config['phone_owner'],
                                                       config['phone_secret'], controller))
                listen(server)
            listen(local)
            print('Sentry running in', 'test' if test else 'unarmed', 'mode', flush=True)
            stopped.wait()
            controller.pause()
    finally:
        controller.close()


def local_status(command):
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(20)
        conn.connect(SOCK)
        conn.sendall(command.encode() + b'\n')
        with conn.makefile('rb') as response:
            return json.loads(response.readline(4096))


def stopped_state():
    return {'mode': 'stopped', 'armed': False, 'capture_worker': False}


def wait_stopped():
    until = time.monotonic() + 8
    while time.monotonic() < until:
        try:
            # The lock survives socket removal if capture cleanup is still in progress.
            with state.locked(Path.cwd()):
                return stopped_state()
        except RuntimeError:
            time.sleep(.05)
    raise RuntimeError('Shutdown still in progress; check the foreground terminal')


def local(command):
    try:
        data = local_status(command)
    except (FileNotFoundError, ConnectionRefusedError):
        if command == 'stop':
            data = wait_stopped()
        elif command == 'status':
            try:
                with state.locked(Path.cwd()):
                    data = stopped_state()
            except RuntimeError:
                data = {'mode': 'busy_without_control_socket', 'capture_worker': None}
        else:
            raise RuntimeError('Sentry is not running') from None
    if not data.get('command_error'):
        if command == 'stop' and data['mode'] != 'stopped':
            data = wait_stopped()
        elif command in ('away', 'home', 'pause'):
            until = time.monotonic() + 6
            while data.get('stopping_capture') and time.monotonic() < until:
                time.sleep(.05)
                data = local_status('status')
    print(json.dumps(data))
    return int(bool(data.get('command_error') or data.get('stopping_capture') and command in ('home', 'pause')))


def test_bot(path, config):
    from sentry import probe
    binding = telegram_bot.validate_binding(state.read(path/'bot.json'))
    with tempfile.TemporaryDirectory(dir=path) as tmp:
        clip = Path(tmp)/'test.mp4'
        subprocess.run([config['ffmpeg'], '-v', 'error', '-nostdin', '-f', 'lavfi',
                        '-i', 'testsrc2=size=320x180:rate=15', '-f', 'lavfi',
                        '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                        '-movflags', '+faststart', str(clip)], check=True, timeout=30)
        probe(config, clip)
        telegram_bot.send(binding, 'Sentry notification test (no camera)')
        telegram_bot.send(binding, 'Sentry test pattern and tone (no camera)', clip)
    print('Bot API confirmed text and MP4 in the paired private chat')


def message(exc):
    if isinstance(exc, FileNotFoundError):
        return 'Required file, socket or selected volume is missing'
    if isinstance(exc, FileExistsError):
        return 'Target already exists; choose a new archive directory name'
    if isinstance(exc, subprocess.SubprocessError):
        return 'A local tool failed; check the command prerequisites in INSTALL.md'
    if isinstance(exc, OSError) and exc.errno is not None:
        return exc.strerror or type(exc).__name__
    if isinstance(exc, (ValueError, OSError, RuntimeError)):
        return str(exc)
    return type(exc).__name__


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for command in (*LOCAL, 'init', 'pair-bot', 'test-bot', 'authorize-mic', 'check-model'):
        commands.add_parser(command)
    commands.add_parser('run').add_argument('--test', action='store_true', help='Never arms, records, or removes media')
    disk = commands.add_parser('archive')
    disk.add_argument('mount', type=Path)
    disk.add_argument('--name', default='MacBook-Sentry')
    configure = commands.add_parser('configure')
    configure.add_argument('--retention-hours', type=float)
    configure.add_argument('--away-lease-seconds', type=int)
    configure.add_argument('--person-confidence', type=float)
    configure.add_argument('--camera', help='AVFoundation video device name or index')
    configure.add_argument('--detection', choices=('on', 'off'))
    phone = commands.add_parser('phone-setup')
    phone.add_argument('host')
    phone.add_argument('owner')
    phone.add_argument('--rotate', action='store_true')
    phone.add_argument('--listen-port', type=int, default=18765)
    commands.add_parser('shortcuts').add_argument('--diagnostic', action='store_true')
    args = parser.parse_args(argv)
    try:
        try:
            path = state.directory(create=args.command == 'init')
        except FileNotFoundError:
            if args.command not in ('status', 'stop'):
                raise
            print(json.dumps(stopped_state()))
            return 0
        os.chdir(path)  # Relative Unix socket avoids macOS's short sun_path limit.
        if args.command in LOCAL:
            return local(args.command)
        with state.locked(path) as lock:
            if args.command == 'init':
                init(path)
                return 0
            config = load(path)
            config_file = path/'config.json'
            if args.command == 'configure':
                for key in ('retention_hours', 'away_lease_seconds', 'person_confidence'):
                    value = getattr(args, key)
                    if value is not None:
                        lo, hi = {'retention_hours': (1, 720), 'away_lease_seconds': (15, 86400),
                                  'person_confidence': (.01, 1)}[key]
                        if not lo <= value <= hi:
                            raise ValueError(f'{key} must be {lo}–{hi}')
                        config[key] = value
                if args.detection:
                    config['detection'] = args.detection == 'on'
                if args.camera is not None:
                    if not args.camera or ':' in args.camera or not args.camera.isprintable():
                        raise ValueError('Select a video device name or index without a colon')
                    config['camera_input'] = args.camera + ':none'
                state.write(config_file, config)
                print('Settings saved; service remains stopped')
            elif args.command == 'archive':
                config.update(archive.provision(args.mount, args.name))
                state.write(config_file, config)
                print('New owned archive created on the selected SSD')
            elif args.command == 'run':
                serve(path, config, lock, args.test)
            elif args.command == 'pair-bot':
                file = path/'bot.json'
                try:
                    old = telegram_bot.validate_binding(state.read(file))
                except FileNotFoundError:
                    old = None
                new = telegram_bot.pair(telegram_bot.get_token())
                if old and old['chat_id'] == new['chat_id'] and old['token'].split(':')[0] == new['token'].split(':')[0]:
                    new['generation'] = old['generation']
                state.write(file, new)
                print('Paired one private chat; restart Sentry unarmed')
            elif args.command == 'test-bot':
                test_bot(path, config)
            elif args.command == 'authorize-mic':
                subprocess.run([config['microphone_capture'], '--authorize'], check=True, timeout=90)
            elif args.command == 'check-model':
                from PIL import Image
                from person_detector import PersonModel
                print('Offline Nano loaded; detections on generated blank frame:',
                      len(PersonModel(config).detect(Image.new('RGB', (384, 384)))))
            elif args.command == 'phone-setup':
                import secrets
                if (not HOST.fullmatch(args.host) or not OWNER.fullmatch(args.owner)
                        or not 1024 <= args.listen_port <= 65535):
                    raise ValueError('Provide the Mac MagicDNS .ts.net name, exact Serve login, and a private origin port')
                if args.rotate or not config.get('phone_secret'):
                    config['phone_secret'] = secrets.token_hex(32)
                config.update(phone_host=args.host, phone_owner=args.owner, listen_port=args.listen_port)
                state.write(config_file, config)
                print('Phone identity saved; no network routes changed')
            elif args.command == 'shortcuts':
                if not config.get('phone_host'):
                    raise RuntimeError('Run phone-setup first')
                print('Private signed pair:', apple_shortcuts.sign_pair(path, config, diagnostic=args.diagnostic))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, ImportError, KeyError) as exc:
        print('Sentry:', message(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
