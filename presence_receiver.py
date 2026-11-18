"""Loopback-only POST origin behind Tailscale Serve."""
from hmac import compare_digest, digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
from typing import cast

from controller import event_time

PREFIX = '/sentry-presence'
HEADER = 'X-Sentry-Signal'


def diagnostic_secret(secret):
    return digest(secret.encode(), b'sentry-diagnostic-only-v1', 'sha256').hex()


class Receiver(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, port, owner, secret, controller):
        self.owner, self.controller = owner, controller
        self.secrets = (secret, diagnostic_secret(secret)) if controller.test else (secret,)
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(('127.0.0.1', port), Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            return self.shutdown_request(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # No request metadata or tracebacks in logs.


class Handler(BaseHTTPRequestHandler):
    @property
    def receiver(self):
        return cast(Receiver, self.server)

    def setup(self):
        self.request.settimeout(3)
        super().setup()

    def log_message(self, format, *args):
        pass

    def reply(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Serve removes client-supplied Tailscale identity headers. Loopback is a
        # trust boundary, not authentication; the install secret is also needed.
        headers = self.headers.get_all(HEADER, [])
        if (self.headers.get_all('Tailscale-User-Login') != [self.receiver.owner] or len(headers) != 1
                or not any(compare_digest(headers[0].encode(), secret.encode()) for secret in self.receiver.secrets)):
            return self.reply(403, {'error': 'forbidden'})
        name = {f'{prefix}/{signal}': signal for prefix in ('', PREFIX)
                for signal in ('home', 'away')}.get(self.path)
        if name is None:
            return self.reply(404, {'error': 'not_found'})
        lengths = self.headers.get_all('Content-Length', [])
        if (self.headers.get_all('Transfer-Encoding') or len(lengths) != 1
                or not re.fullmatch(r'[1-9][0-9]{0,2}', lengths[0]) or int(lengths[0]) > 128
                or len(self.headers.get_all('Content-Type', [])) != 1
                or self.headers.get_content_type() != 'application/json'):
            return self.reply(400, {'error': 'expected_small_json_body'})
        try:
            body = self.rfile.read(int(lengths[0]))
            fields = json.loads(body, object_pairs_hook=lambda pairs: pairs)
            if len(body) != int(lengths[0]) or not body.lstrip().startswith(b'{') or len(fields) != 1:
                raise ValueError
            key, value = fields[0]
            if key != 'event_at':
                raise ValueError
            at = event_time(value)
        except (ValueError, TypeError, OverflowError):
            return self.reply(400, {'error': 'invalid_event_at'})
        error = self.receiver.controller.receive(name, at)
        state = self.receiver.controller.status()
        return self.reply(409 if error else 200, {'accepted': error is None, 'event': name,
                                                'error': error, **{k: state[k] for k in
                                                ('signal', 'mode', 'armed', 'last_accepted')}})
