"""Offline acceptance: generated media, loopback only, fake Bot API and signer."""
from array import array
from contextlib import contextmanager, redirect_stdout
import fcntl
import hashlib
from http.client import HTTPConnection
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import apple_shortcuts as shortcuts
import archive
import controller
import macbook_sentry as cli
from live import Viewer
from person_detector import PersonGate
from presence_receiver import Receiver, diagnostic_secret
import sentry
import state
import telegram_bot as bot

ROOT = Path(__file__).resolve().parent
OWNER = 'fixture@example.invalid'
HOST = 'device.tailnet.ts.net'  # Reserved fixture string; never resolved.
SECRET = '7' * 64
TOKEN = '123456:' + 'x' * 35
NS = archive.NS


@contextmanager
def cwd(path):
    before = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


def until(condition, seconds=7):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError('Fixture did not reach expected state')


def post(port, route, headers=None, data=None):
    if headers is None:
        headers = {'Tailscale-User-Login': OWNER, 'X-Sentry-Signal': SECRET, 'Content-Type': 'application/json'}
    conn = HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        conn.request('POST', route, json.dumps(data if data is not None else {'event_at': controller.iso(time.time())}), headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


class Candidate(unittest.TestCase):
    def setUp(self):
        with_prefix = tempfile.TemporaryDirectory(prefix='sentry-test-')
        self.addCleanup(with_prefix.cleanup)
        self.base = Path(with_prefix.name).resolve()

    def test_detector_keeps_initial_frames_while_model_loads(self):
        frames = sentry.Frames()
        with patch.object(sentry, 'FRAME_BYTES', 1):
            frames.read(io.BytesIO(b'1234567'), 'a'*32)
        frames.model_ready()
        for expected in b'1234567':
            frame = frames.next(0)
            assert frame is not None
            self.assertEqual(frame[3], bytes([expected]))
        self.assertEqual(frames.queue.maxlen, sentry.LIVE_FRAMES)

    def test_closed_clip_resumes_without_resending_alert_or_touching_active_capture(self):
        config = {'retention_hours': 24}
        binding = {'generation': 'fixture'}
        active = ['a'*32]
        generation = binding['generation']
        (self.base/'events').mkdir()
        active_dir, done, old = (self.base/'events'/f'{time.time_ns()}-{i*32}' for i in 'abc')
        for path, session, gen in ((active_dir, active[0], generation), (done, 'b'*32, generation),
                                   (old, 'c'*32, 'old-bot')):
            path.mkdir()
            sentry.atomic(path/'event.json', {'capture': session, 'delivery_generation': gen,
                                             'time': sentry.timestamp(), 'clip_ready': False})
            sentry.atomic(path/'receipt.json', {'alert': {'message_id': 9}})

        def annotate(source, part):
            self.assertEqual(source.parent, done)
            part.write_bytes(b'boxed mp4 fixture')

        stop = threading.Event()
        with (patch('person_detector.PersonModel') as model, patch.object(sentry, 'probe'),
              patch.object(sentry, 'clip_event', return_value=time.time_ns()) as build,
              patch.object(bot, 'send', return_value={'message_id': 10}) as send):
            model.return_value.annotate.side_effect = annotate
            for _ in range(2):
                self.assertFalse(sentry.finish_closed_events(config, binding, self.base, lambda: active[0], stop))
            build.assert_called_once()
            self.assertTrue(build.call_args.kwargs['final'])
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], done/'telegram.mp4')
        self.assertEqual(json.loads((done/'receipt.json').read_text()),
                         {'alert': {'message_id': 9}, 'video': {'message_id': 10}})
        for untouched in (active_dir, old):
            event = sentry.read_event(untouched)
            assert event is not None
            self.assertFalse(event['clip_ready'])

    def test_private_viewer_cannot_read_history_and_closes_on_home(self):
        with patch.dict(os.environ, HOME=str(self.base)), cwd(self.base):
            state.directory(create=True)
            cfg = archive.provision_internal()
            session = 'a'*32
            capture = state.directory()/cfg['archive_name']/'recordings'/session
            capture.mkdir()
            name = '1900000000.ts'
            media = capture/name
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=15',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                            '-c:v', 'libx264', '-c:a', 'aac', '-f', 'mpegts', str(media)], check=True)
            (capture/'segments.csv').write_text(f'{name},0,2\n')
            lease: list[str | None] = [session]
            class Current:
                def viewing_session(self): return lease[0]
            with Viewer(0, OWNER, Current(), cfg) as server:
                self.assertEqual(server.server_address[0], '127.0.0.1')
                t = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02})
                t.start()
                root = '/sentry-live/'
                segment = f'{root}{session}/{name}'
                def fetch(path, owner=None):
                    conn = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                    try:
                        conn.request('GET', path, headers={'Tailscale-User-Login':owner} if owner else {})
                        reply = conn.getresponse()
                        return reply.status, reply.read(), reply.getheader('Cache-Control')
                    finally:
                        conn.close()
                try:
                    for path in (root,root+'index.m3u8',segment):
                        self.assertEqual(fetch(path)[0], 403)
                        self.assertEqual(fetch(path,'other@example.invalid')[0], 403)
                    status, playlist, cache = fetch(root+'index.m3u8',OWNER)
                    self.assertEqual((status,cache),(200,'private, no-store'))
                    self.assertIn(f'./{session}/{name}'.encode(),playlist)
                    status, data, _ = fetch(segment,OWNER)
                    self.assertEqual((status,data),(200,media.read_bytes()))
                    self.assertEqual(fetch(segment.replace(session, 'b'*32),OWNER)[0],404)
                    self.assertEqual(fetch(root+'../config.json',OWNER)[0],404)
                    self.assertEqual(fetch(root+'away',OWNER)[0],404)
                    out = subprocess.check_output(['ffprobe', '-v', 'error', '-analyzeduration', '1000000', '-probesize', '100000', '-headers',
                                  f'Tailscale-User-Login: {OWNER}\r\n', '-show_entries', 'stream=codec_type',
                                  '-of', 'json', f'http://127.0.0.1:{server.server_port}{root}index.m3u8'], timeout=12)
                    self.assertEqual({s['codec_type'] for s in json.loads(out)['streams']},{'audio','video'})
                    lease[0] = None
                    self.assertEqual(fetch(root+'index.m3u8',OWNER)[0],410)
                    self.assertEqual(fetch(segment,OWNER)[0],410)
                finally:
                    server.shutdown(); t.join()

    def test_public_signal_lane_requires_key_and_rejects_replay(self):
        test = controller.Controller({}, test=True)
        with Receiver(0, OWNER, SECRET, test, public_control=True) as receiver:
            thread = threading.Thread(target=receiver.serve_forever, kwargs={'poll_interval': .02})
            thread.start()
            try:
                headers = {'Content-Type': 'application/json'}
                for event in ('away','home'):
                    path = '/sentry-presence/'+event
                    data = {'event_at': controller.iso(time.time())}
                    self.assertEqual(post(receiver.server_port,path,headers,data)[0],403)
                    self.assertEqual(post(receiver.server_port,path,headers | {'X-Sentry-Signal':'wrong'},data)[0],403)
                    auth = headers | {'X-Sentry-Signal':diagnostic_secret(SECRET)}
                    self.assertEqual(post(receiver.server_port,path,auth,data)[0],200)
                    self.assertEqual(post(receiver.server_port,path,auth,data)[0],409)
                self.assertFalse(test.status()['capture_worker'])
            finally:
                receiver.shutdown(); thread.join(); test.close()

    def test_receiver_bounds_concurrent_connections_and_recovers(self):
        test = controller.Controller({}, test=True)
        sockets = []
        with Receiver(0, OWNER, SECRET, test) as receiver:
            self.assertEqual(receiver.server_address[0], '127.0.0.1')
            endpoint = ('127.0.0.1', receiver.server_port)
            thread = threading.Thread(target=receiver.serve_forever, kwargs={'poll_interval': .02})
            thread.start()
            try:
                with patch.object(receiver, 'process_request_thread', wraps=receiver.process_request_thread) as handle:
                    for _ in range(16):
                        sockets.append(socket.create_connection(endpoint, timeout=1))
                    until(lambda: handle.call_count == 16)
                    with socket.create_connection(endpoint, timeout=1) as excess:
                        try:
                            self.assertEqual(excess.recv(1), b'')
                        except ConnectionResetError:
                            pass
                    self.assertEqual(handle.call_count, 16)
                sockets.pop().close()
                def recovered():
                    try:
                        return post(receiver.server_port, '/home')[0] == 200
                    except (OSError, ConnectionError):
                        return False
                until(recovered, seconds=2)
                self.assertFalse(test.status()['capture_worker'])
            finally:
                for client in sockets:
                    client.close()
                receiver.shutdown()
                thread.join()
                test.close()

    def test_status_notices_follow_worker_state_and_deduplicate(self):
        from unittest.mock import Mock
        service = controller.Controller({}, test=True)
        sent = []
        try:
            with service.cv:
                service.notice = lambda text: not sent.append(text)
                service.presence = 'away'
                service.notify_presence()
                self.assertEqual(sent, [])
                service.worker = Mock()
                service.worker.poll.return_value = None
                service.notify_presence()
                service.notify_presence()
                service.presence = 'home'
                service.notify_presence()
                self.assertEqual(sent, ['Sentry activated'])
                service.worker = None
                service.notify_presence()
                service.notify_presence()
                self.assertEqual(sent, ['Sentry activated', 'Sentry disabled'])
        finally:
            service.worker = None
            service.close()

    def test_shortcut_serialization_and_replay_on_test_receiver(self):
        test = controller.Controller({}, test=True)
        with Receiver(0, OWNER, SECRET, test) as receiver:
            thread = threading.Thread(target=receiver.serve_forever, kwargs={'poll_interval': .02})
            thread.start()
            try:
                for event in ('away', 'home'):
                    generated = shortcuts.workflow(HOST, SECRET, event, diagnostic=True)
                    actions = plistlib.loads(plistlib.dumps(generated))['WFWorkflowActions']
                    date, request, result = actions
                    self.assertTrue(date['WFWorkflowActionIdentifier'].endswith('format.date'))
                    date_params = date['WFWorkflowActionParameters']
                    self.assertEqual(date_params['WFDate'], {
                        'WFSerializationType': 'WFTextTokenString',
                        'Value': {'string': '\ufffc', 'attachmentsByRange': {'{0, 1}': {'Type': 'CurrentDate'}}},
                    })
                    self.assertEqual(date_params['WFDateFormatStyle'], 'ISO 8601')
                    self.assertIs(date_params['WFISO8601IncludeTime'], True)
                    params = request['WFWorkflowActionParameters']
                    self.assertEqual((params['WFHTTPMethod'], params['WFHTTPBodyType']), ('POST', 'JSON'))
                    def unpack(key):
                        value = params[key]
                        self.assertEqual(value['WFSerializationType'], 'WFDictionaryFieldValue')
                        return {r['WFKey']['Value']['string']: r['WFValue']['Value']
                                for r in value['Value']['WFDictionaryFieldValueItems'] if r['WFItemType'] == 0}
                    headers = {key: value['string'] for key, value in unpack('WFHTTPHeaders').items()}
                    self.assertNotIn('Tailscale-User-Login', headers)
                    body = unpack('WFJSONValues')
                    self.assertEqual(body['event_at']['attachmentsByRange']['{0, 1}']['OutputUUID'], date_params['UUID'])
                    self.assertTrue(result['WFWorkflowActionIdentifier'].endswith('showresult'))
                    self.assertEqual(params['WFURL']['WFSerializationType'], 'WFTextTokenString')
                    route = urlsplit(params['WFURL']['Value']['string']).path
                    now = controller.iso(time.time())
                    self.assertEqual(post(receiver.server_port, route, headers, {'event_at': now})[0], 403)
                    headers['Tailscale-User-Login'] = OWNER
                    self.assertEqual(post(receiver.server_port, route, headers | {'X-Sentry-Signal': 'bad'})[0], 403)
                    self.assertEqual(post(receiver.server_port, route, headers, {'event_at': now, 'lat': 0})[0], 400)
                    code, reply = post(receiver.server_port, route, headers, {'event_at': now})
                    self.assertEqual((code, reply['event'], reply['mode'], reply['armed']), (200, event, 'test', False))
                    self.assertEqual(post(receiver.server_port, route, headers, {'event_at': now})[0], 409)
                self.assertFalse(test.arm())
                self.assertFalse(test.status()['capture_worker'])
                self.assertEqual(set(test.status()['last_accepted']), {'home', 'away'})
                self.assertEqual(post(receiver.server_port, '/sentry-presence/away', data={'event_at': controller.iso(test.last['home'] - .000001)})[0], 409)
                self.assertEqual(post(receiver.server_port, '/away', data={'event_at': controller.iso(time.time()+60)})[0], 409)
                self.assertEqual(post(receiver.server_port, '/home', data={'event_at': controller.iso(time.time()-600)})[0], 409)
            finally:
                receiver.shutdown()
                thread.join()
                test.close()
        restarted = controller.Controller({}, test=True)
        try:
            self.assertIsNone(restarted.status()['signal'])
            self.assertFalse(restarted.status()['last_accepted'])
        finally:
            restarted.close()

    def test_worker_home_lease_and_parent_cleanup(self):
        (self.base/'sentry.py').write_text('''
import json, pathlib, subprocess, sys, time
cfg = json.loads(sys.stdin.buffer.readline())
child = subprocess.Popen([sys.executable, '-c', "import signal,socket,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); print(s.getsockname()[1],flush=True); time.sleep(35)"], stdout=subprocess.PIPE)
pathlib.Path(cfg['port_file']).write_bytes(child.stdout.readline())
time.sleep(35)
''')
        port_file = self.base/'port'
        with (self.base/'lease.lock').open('a') as lock, patch.object(controller, 'ROOT', self.base):
            fcntl.flock(lock, fcntl.LOCK_EX)
            service = controller.Controller({'away_lease_seconds': 1, 'port_file': str(port_file)}, lock)
            try:
                self.assertEqual(service.receive('away', time.time()), 'not_armed')
                self.assertTrue(service.arm())
                self.assertEqual(service.receive('away', service.started-1), 'timestamp_outside_window')
                self.assertIsNone(service.receive('away', time.time()))
                launch = until(lambda: service.worker)
                port = int(until(lambda: port_file.read_text() if port_file.exists() else None))
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    pass
                lease = service.away_until
                self.assertIsNone(service.receive('away', time.time()))
                self.assertEqual(service.away_until, lease)
                self.assertIsNone(service.receive('home', time.time()))
                until(lambda: launch.poll() is not None and service.worker is None)
                def listener_closed():
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=1):
                            return False
                    except ConnectionResetError:
                        # Darwin can reset a connection that raced group shutdown.
                        return False
                    except ConnectionRefusedError:
                        return True
                until(listener_closed, seconds=2)
                self.assertIsNone(service.receive('away', time.time()))
                until(lambda: service.worker)
                until(lambda: service.status()['error'] == 'away_lease_expired' and service.worker is None)
                self.assertFalse(service.status()['armed'])
            finally:
                service.close()

    def test_missing_volume_worker_and_production_rejects_test_key(self):
        config = {'away_lease_seconds': 60, 'detection': False}
        with (self.base/'lock').open('a') as lock:
            service = controller.Controller(config, lock)
            with Receiver(0, OWNER, SECRET, service) as receiver:
                listener = threading.Thread(target=receiver.serve_forever, kwargs={'poll_interval': .02})
                listener.start()
                try:
                    self.assertTrue(service.arm())
                    headers = {'Tailscale-User-Login': OWNER, 'X-Sentry-Signal': diagnostic_secret(SECRET),
                               'Content-Type': 'application/json'}
                    self.assertEqual(post(receiver.server_port, '/away', headers)[0], 403)
                    self.assertFalse(service.status()['last_accepted'])
                    self.assertEqual(post(receiver.server_port, '/away')[0], 200)
                    # Real worker, but no archive binding: exits before opening any device.
                    until(lambda: service.status()['error'] == 'capture_worker_exited')
                    self.assertFalse(service.status()['capture_worker'])
                    self.assertFalse(service.status()['armed'])
                finally:
                    receiver.shutdown()
                    listener.join()
                    service.close()

    def test_owned_volume_provision_and_home_retention(self):
        mount = self.base/'volume'
        mount.mkdir()
        identity = '9'*32
        @contextmanager
        def volume(path):
            fd = os.open(path, archive.DIR)
            try:
                yield fd
            finally:
                os.close(fd)
        with patch.object(archive, 'open_volume', volume), patch.object(archive, 'disk_identity', return_value=identity):
            (mount/'occupied').mkdir()
            with self.assertRaises(FileExistsError):
                archive.provision(mount, 'occupied')
            self.assertEqual(list((mount/'occupied').iterdir()), [])
            config = archive.provision(mount, 'candidate') | {'retention_hours': 2, 'away_lease_seconds': 60}
            root = mount/'candidate'
            recording = root/'recordings'/('a'*32)
            recording.mkdir()
            older = int(time.time()) - 7200 - 30
            files = [recording/f'{older}.ts', recording/f'{older+3600}.ts']
            for item in files:
                item.write_bytes(b'fixture')  # Recent mtime, expired capture time.
            adjacent = mount/'unrelated.ts'
            adjacent.write_bytes(b'unrelated')
            alias = recording/f'{older-1}.ts'
            alias.symlink_to(adjacent)
            event = root/'events'/f'{older*NS}-{"b"*32}'
            event.mkdir()
            (event/'event.json').write_text(json.dumps({'capture_end_ns': (older+10)*NS}))
            (event/'clean.mp4').write_bytes(b'fixture')
            with self.assertRaises(PermissionError):
                archive.sweep(config | {'volume_uuid': 'wrong'})
            with self.assertRaises(PermissionError):
                archive.sweep(config | {'archive_owner': '9'*32})
            self.assertTrue(files[0].exists())
            service = controller.Controller(config)
            try:
                self.assertIsNone(service.receive('home', time.time()))
                until(lambda: not files[0].exists() and not event.exists())
                self.assertEqual(service.status()['signal'], 'home')
                self.assertFalse(service.worker)
                self.assertTrue(files[1].exists() and alias.is_symlink())
                self.assertEqual(adjacent.read_bytes(), b'unrelated')
            finally:
                service.close()
        with self.assertRaises((FileNotFoundError, ValueError)):
            archive.sweep(config)

    def test_external_hard_drive_identity(self):
        mount = Path('/Volumes/Test-Drive')
        identifier = 'deadbeef-dead-4bee-8fee-deadbeefdead'
        info = {'MountPoint': str(mount), 'VolumeUUID': identifier, 'Internal': False,
                'SolidState': False, 'BusProtocol': 'USB'}
        with patch.object(archive.subprocess, 'check_output', return_value=plistlib.dumps(info)):
            self.assertEqual(archive.disk_identity(mount), identifier)
        with patch.object(archive.subprocess, 'check_output',
                          return_value=plistlib.dumps(info | {'Internal': True})), self.assertRaises(OSError):
            archive.disk_identity(mount)

    def test_pair_challenge_and_outbound_validation(self):
        notice = []
        def pairing(token, method, fields):
            self.assertEqual(token, TOKEN)
            if method == 'getMe':
                return {'id': 123456, 'is_bot': True, 'username': 'fixture_bot'}
            if method == 'getWebhookInfo':
                return {'url': ''}
            self.assertEqual(method, 'getUpdates')
            text = notice[0].split('send exactly: ')[1]
            self.assertGreaterEqual(len(text.split()[1]), 32)
            msg = {'chat': {'id': 22, 'type': 'private'}, 'from': {'id': 22, 'is_bot': False},
                   'text': text, 'date': int(time.time())}
            return [{'update_id': 1, 'message': msg | {'from': {'id': 33, 'is_bot': False}}},
                    {'update_id': 2, 'message': msg | {'chat': {'id': 22, 'type': 'group'}}},
                    {'update_id': 3, 'message': msg | {'text': '/start wrong'}},
                    {'update_id': 4, 'message': msg | {'date': int(time.time())-120}},
                    {'update_id': 5, 'message': msg}]
        with patch.object(bot, 'api', side_effect=pairing):
            binding = bot.pair(TOKEN, tell=lambda text, **_: notice.append(text))
        self.assertEqual(binding['chat_id'], 22)
        outbound = {'message_id': 8, 'chat': {'id': 22, 'type': 'private'}, 'text': 'hello', 'caption': 'hello',
                    'video': {'file_id': 'fixture-file', 'duration': 2, 'width': 320, 'height': 180, 'file_size': 3,
                              'mime_type': 'video/mp4'}}
        with patch.object(bot, 'api', return_value=outbound) as endpoint:
            self.assertEqual(bot.send(binding, 'hello'), {'message_id': 8})
            endpoint.assert_called_once_with(TOKEN, 'sendMessage', {'chat_id': 22, 'text': 'hello'}, video=None)
        for wrong in (None, outbound | {'chat': {'id': 23, 'type': 'private'}}, outbound | {'chat': {'id': True, 'type': 'private'}},
                      outbound | {'video': {'file_id': ''}}, outbound | {'caption': 'wrong'}, outbound | {'message_thread_id': 8}):
            with patch.object(bot, 'api', return_value=wrong), self.assertRaises(bot.BotError):
                bot.send(binding, 'hello', Path('not-opened.mp4'))
        with patch.object(bot, 'api', return_value={'id': 98765, 'is_bot': True, 'username': 'other_bot'}):
            with self.assertRaises(bot.BotError):
                bot.pair(TOKEN)

    def test_https_response_and_multipart_contract(self):
        class Connection:
            status = 200
            raw = b'{"ok":true,"result":{"video":{"file_id":"fixture"}}}'
            def __init__(self, host, *, timeout):
                assert host == 'api.telegram.org' and timeout > 0
            def request(self, method, path, payload, headers):
                assert method == 'POST' and path == f'/bot{TOKEN}/sendVideo'
                assert 'multipart/form-data' in headers['Content-Type']
                assert int(headers['Content-Length']) == len(b''.join(payload))
                assert b'name="chat_id"\r\n\r\n22' in payload[0] and b'video/mp4' in payload[0]
                assert payload[1] == b'fixture bytes'
            def getresponse(self):
                return self
            def read(self, length):
                return self.raw[:length]
            def close(self):
                pass
        clip = self.base/'fixture.mp4'
        clip.write_bytes(b'fixture bytes')
        with patch.object(bot, 'HTTPSConnection', Connection):
            self.assertEqual(bot.api(TOKEN, 'sendVideo', {'chat_id': 22}, video=clip)['video']['file_id'], 'fixture')
            for raw in (b'["ok"]', b'{"ok":true}', b'not JSON', b'{"ok":false,"result":{}}'):
                Connection.raw = raw
                with self.assertRaises(bot.BotError):
                    bot.api(TOKEN, 'sendVideo', {'chat_id': 22}, video=clip)

    def test_cli_clean_home_setup_run_test_and_stop(self):
        env = os.environ | {'HOME': str(self.base), 'TMPDIR': str(self.base)}
        command = [sys.executable, '-B', str(ROOT/'macbook_sentry.py')]
        def call(*args, status=0):
            result = subprocess.run(command + list(args), env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, status, (args, result.stdout, result.stderr))
            return result.stdout
        self.assertEqual(json.loads(call('status'))['mode'], 'stopped')
        call('init')
        path = self.base/'.macbook-sentry'
        self.assertEqual((path.stat().st_mode & 0o777, (path/'config.json').stat().st_mode & 0o777), (0o700, 0o600))
        self.assertFalse((path/'control.sock').exists())
        config = state.read(path/'config.json')
        self.assertNotIn('volume', config)
        archive_path = path/'archive'
        self.assertEqual((archive_path/archive.MARKER).read_text().strip(), config['archive_owner'])
        self.assertEqual(config['retention_hours'], 24)
        call('configure', '--retention-hours', '12', '--away-lease-seconds', '30', '--person-confidence', '.55',
             '--detection', 'off', '--camera', 'USB Webcam')
        call('configure', '--retention-hours', '0', status=1)
        call('archive', str(self.base/'not-a-volume'), status=1)
        recording = archive_path/'recordings'/('a'*32)
        recording.mkdir()
        now = int(time.time())
        expired, kept = (recording/f'{now-hours*3600}.ts' for hours in (13, 11))
        for item in (expired, kept):
            item.write_bytes(b'fixture')
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        call('phone-setup', HOST, OWNER, '--listen-port', str(port))
        old = state.read(path/'config.json')['phone_secret']
        call('phone-setup', HOST, OWNER, '--listen-port', str(port), '--rotate')
        self.assertNotEqual(old, state.read(path/'config.json')['phone_secret'])
        for test in (True, False):
            with subprocess.Popen(command + ['run'] + (['--test'] if test else []), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as service:
                try:
                    until(lambda: (path/'control.sock').is_socket())
                    result = json.loads(call('status'))
                    self.assertEqual((result['mode'], result['armed'], result['signal']), ('test' if test else 'run', False, None))
                    call('run', status=1)
                    call('away', status=0 if test else 1)
                    call('arm', status=1 if test else 0)
                    call('home')
                    result = json.loads(call('status'))
                    self.assertEqual(result['signal'], 'home')
                    self.assertFalse(result['capture_worker'])
                    if test:
                        self.assertTrue(expired.exists())
                    else:
                        until(lambda: not expired.exists())
                    self.assertTrue(kept.exists())
                    self.assertFalse(json.loads(call('pause'))['capture_worker'])
                    self.assertEqual(json.loads(call('stop'))['mode'], 'stopped')
                    self.assertEqual(service.wait(timeout=6), 0)
                finally:
                    if service.poll() is None:
                        service.kill()
        self.assertEqual(json.loads(call('stop'))['mode'], 'stopped')
        self.assertFalse((path/'control.sock').exists())
        config = state.read(path/'config.json')
        external = config | {'volume': '/Volumes/SentryTestNotMounted', 'volume_uuid': '9'*32}
        with patch.dict(os.environ, HOME=str(self.base)), self.assertRaises(PermissionError):
            archive.sweep(config | {'archive_owner': '0'*32})
        with self.assertRaises(FileNotFoundError), archive.open_archive(external):
            self.fail('An unavailable external archive must not fall back to internal storage')
        state.write(path/'config.json', external)
        call('archive', '--internal', status=1)  # Never adopt the existing archive.
        self.assertEqual(state.read(path/'config.json'), external)
        call('archive', '--internal', '--name', 'recordings-new')
        config = state.read(path/'config.json')
        self.assertFalse({'volume', 'volume_uuid'}.intersection(config))
        self.assertEqual(config['retention_hours'], 12)
        self.assertTrue(kept.exists())  # Switching never moves or deletes footage.
        self.assertTrue((path/'recordings-new'/'recordings').is_dir())

    def test_cli_pair_rotation_sign_and_video_without_external_calls(self):
        with patch.dict(os.environ, HOME=str(self.base)), cwd(self.base), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['init']), 0)
            path = state.directory()
            first = {'token': TOKEN, 'chat_id': 22, 'generation': 'b'*32}
            rotated = first | {'token': '123456:' + 'y'*35, 'generation': 'c'*32}
            with patch.object(bot, 'get_token', return_value=TOKEN), patch.object(bot, 'pair', side_effect=[first, rotated]):
                self.assertEqual(cli.main(['pair-bot']), 0)
                self.assertEqual(cli.main(['pair-bot']), 0)
            target = path/'bot.json'
            self.assertEqual(state.read(target), rotated | {'generation': first['generation']})
            target.chmod(0o644)
            with self.assertRaises(PermissionError):
                state.read(target)
            target.chmod(0o600)
            link = path/'link'
            link.symlink_to(target)
            with self.assertRaises(OSError):
                state.read(link)
            self.assertEqual(cli.main(['phone-setup', HOST, OWNER]), 0)
            cfg = cli.load(path)
            first_key = cfg['phone_secret']
            self.assertEqual(cli.main(['phone-setup', HOST, OWNER, '--via-funnel']), 0)
            cfg = cli.load(path)
            self.assertNotEqual(cfg['phone_secret'], first_key)
            def fake_sign(args, **kw):
                self.assertEqual(args[:4], ['/usr/bin/shortcuts', 'sign', '--mode', 'anyone'])
                raw = Path(args[5]).read_bytes()
                value = plistlib.loads(raw)
                self.assertNotIn(cfg['phone_secret'].encode(),raw)
                self.assertNotIn(diagnostic_secret(cfg['phone_secret']).encode(),raw)
                self.assertEqual(value['WFWorkflowActions'][0]['WFWorkflowActionParameters']['WFTextActionText'],'PASTE_SETUP_KEY')
                self.assertIsNone(urlsplit(value['WFWorkflowActions'][2]['WFWorkflowActionParameters']['WFURL']['Value']['string']).port)
                self.assertGreaterEqual(len(value['WFWorkflowActions']), 2)
                Path(args[7]).write_bytes(b'test signer output')
            with patch.object(shortcuts.subprocess, 'run', side_effect=fake_sign):
                self.assertEqual(cli.main(['shortcuts']), 0)
                self.assertEqual(cli.main(['shortcuts', '--diagnostic']), 0)
            self.assertEqual(sorted(p.name for p in path.glob('phone-*/*.shortcut')),
                             ['Sentry Away Test.shortcut', 'Sentry Away.shortcut', 'Sentry Home Test.shortcut', 'Sentry Home.shortcut'])
            keys = [p for p in path.glob('phone-*/Sentry Setup Key.txt')]
            self.assertEqual({p.read_text().strip() for p in keys},{cfg['phone_secret'],diagnostic_secret(cfg['phone_secret'])})
            self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in keys))
            def delivery(token, method, fields, *, video):
                self.assertEqual(token, rotated['token'])
                self.assertEqual(fields['chat_id'], 22)
                result = {'chat': {'id': 22, 'type': 'private'}, 'message_id': 13}
                if method == 'sendVideo':
                    self.assertGreater(video.stat().st_size, 1000)
                    sentry.probe(cli.load(path), video)
                    result.update(caption=fields['caption'], video={'file_id': 'fixture', 'width': 320, 'height': 180,
                                                                  'duration': 2, 'mime_type': 'video/mp4'})
                else:
                    self.assertEqual(method, 'sendMessage')
                    self.assertIsNone(video)
                    result['text'] = fields['text']
                return result
            with patch.object(bot, 'api', side_effect=delivery) as api:
                self.assertEqual(cli.main(['test-bot']), 0)
                self.assertEqual(api.call_count, 2)
            with patch.object(cli.subprocess, 'run') as mock:
                self.assertEqual(cli.main(['authorize-mic']), 0)
                self.assertEqual(mock.call_args.args[0][-1], '--authorize')

    def test_generated_av_video_toolbox_and_fixed_late_clip(self):
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        assert ffmpeg and ffprobe, "FFmpeg and FFprobe required"
        config = {'ffmpeg': ffmpeg, 'ffprobe': ffprobe, 'camera_input': '0:none'}
        capture = 'b'*32
        with cwd(self.base):
            recording = Path('recordings')/capture
            recording.mkdir(parents=True)
            # Swap only the AVFoundation video input; keep the production PCM pipe and filter.
            for detect in (False, True):
                output = recording if detect else Path('clean-mode')
                output.mkdir(exist_ok=True)
                read_fd, write_fd = os.pipe()
                graph = sentry.camera_command(config | {'detection': detect}, output, 4, read_fd, audio_rate=44100)
                self.assertIn('h264_videotoolbox', graph)
                self.assertEqual(graph[graph.index('-allow_sw')+1], '0')
                self.assertEqual('pipe:1' in graph, detect)
                self.assertEqual('split=2' in graph[graph.index('-filter_complex')+1], detect)
                self.assertIn('[1:a]asetpts=N/SR/TB', graph[graph.index('-filter_complex')+1])
                head = graph.index('-thread_queue_size')
                pcm = graph.index('-thread_queue_size', head+1)
                graph[head:pcm] = ['-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30:duration=4']
                tone = subprocess.Popen([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                                         'sine=frequency=440:sample_rate=44100:duration=4', '-c:a', 'pcm_f32le',
                                         '-f', 'f32le', 'pipe:1'], stdout=write_fd)
                os.close(write_fd)
                try:
                    with subprocess.Popen(graph, stdout=subprocess.PIPE, pass_fds=(read_fd,)) as proc:
                        os.close(read_fd)
                        frames = sentry.Frames()
                        frames.read(proc.stdout, capture)
                        self.assertEqual(proc.wait(timeout=30), 0)
                    self.assertEqual(tone.wait(timeout=10), 0)
                finally:
                    if tone.poll() is None:
                        tone.kill()
                        tone.wait()
                self.assertEqual(bool(frames.next(0)), detect)
                self.assert_pcm(ffmpeg, next(output.glob('*.ts')), 4)
            saved = next(recording.glob('*.ts'))
            original = hashlib.sha256(saved.read_bytes()).digest()
            gate = PersonGate()
            self.assertFalse(gate.update(0, True))
            self.assertTrue(gate.update(.5, True))
            event = Path('events')/f'{time.time_ns()}-{"c"*32}'
            event.mkdir(parents=True)
            msg = {'capture': capture, 'pts': 2}
            self.assertFalse(sentry.clip_event(config, event, msg))
            end = sentry.clip_event(config, event, msg, final=True)
            self.assertLess(abs(end-time.time_ns()), 10*NS)
            with patch.object(sentry.time, 'time_ns', return_value=end+archive.DAY):
                self.assertFalse(sentry.clip_event(config, event, msg, final=True))
            self.assertTrue(sentry.clip_event(config, event, msg | {'pts': 5.5}, final=True))
            output = event/'clean.mp4'
            video = json.loads(subprocess.check_output([ffprobe, '-v', 'error', '-select_streams', 'v:0',
                '-show_entries', 'stream=start_time,nb_frames', '-of', 'json', str(output)]))['streams'][0]
            self.assertEqual((float(video['start_time']), int(video['nb_frames'])), (0., 52))
            self.assertEqual(hashlib.sha256(saved.read_bytes()).digest(), original)
            sentry.probe(config, output)
            self.assert_pcm(ffmpeg, output, 3.5)

    def assert_pcm(self, ffmpeg, clip, seconds):
        data = array('f', subprocess.check_output([ffmpeg, '-xerror', '-v', 'error', '-i', str(clip), '-map', '0:a:0',
                                                  '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1']))
        self.assertLess(abs(len(data)/48000 - seconds), .13)
        for index in range(4800, len(data)-4800, 2400):
            self.assertGreater(sum(v*v for v in data[index:index+2400])/2400, 1e-5)


if __name__ == '__main__':
    unittest.main()
