"""Dedicated, single-recipient Bot API client; polling is used only to pair."""
from getpass import GetPassWarning, getpass
from hmac import compare_digest
from http.client import HTTPException, HTTPSConnection
import json
import re
import secrets
import time
import warnings

TOKEN = re.compile(r'[1-9][0-9]{4,19}:[A-Za-z0-9_-]{30,100}')
MAX_VIDEO = 49_000_000
MAX_RESPONSE = 4 * 1024 * 1024


class BotError(RuntimeError):
    pass


def positive(value):
    return type(value) is int and value > 0


def get_token():
    with warnings.catch_warnings():
        warnings.simplefilter('error', GetPassWarning)
        try:
            token = getpass('BotFather token (hidden): ')
        except (GetPassWarning, EOFError):
            raise BotError('Use a local Terminal with non-echoing input') from None
    if not TOKEN.fullmatch(token):
        raise BotError('Invalid BotFather token format')
    return token


def validate_binding(data):
    if (not isinstance(data, dict) or not isinstance(data.get('token'), str)
            or not TOKEN.fullmatch(data['token']) or not positive(data.get('chat_id'))
            or not isinstance(data.get('generation'), str)
            or not re.fullmatch(r'[a-f0-9]{32}', data['generation'])):
        raise BotError('Invalid bot binding; pair again')
    return data


def api(token, method, fields, *, video=None):
    if not TOKEN.fullmatch(token):
        raise BotError('Invalid BotFather token format')
    path = f'/bot{token}/{method}'
    conn = HTTPSConnection('api.telegram.org', timeout=20)
    try:
        if video is None:
            conn.request('POST', path, json.dumps(fields).encode(), {'Content-Type': 'application/json'})
        else:
            boundary = secrets.token_hex(24)
            prefix = b''.join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                              for k, v in fields.items())
            prefix += (f'--{boundary}\r\nContent-Disposition: form-data; name="video"; filename="clip.mp4"\r\n'
                       'Content-Type: video/mp4\r\n\r\n').encode()
            suffix = f'\r\n--{boundary}--\r\n'.encode()
            with video.open('rb') as stream:
                payload = stream.read(MAX_VIDEO + 1)
            if not 0 < len(payload) <= MAX_VIDEO:
                raise BotError('Video exceeds the upload limit or is empty')
            conn.request('POST', path, (prefix, payload, suffix), {
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Content-Length': str(len(prefix) + len(payload) + len(suffix))})
        response = conn.getresponse()
        if response.status != 200:
            raise BotError(f'{method} refused (HTTP {response.status})')
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise BotError(f'{method} response too large')
        data = json.loads(raw)
    except (OSError, HTTPException, ValueError, RecursionError):
        # Paths include the token; never surface transport errors or response bodies.
        raise BotError(f'{method} transport or response error') from None
    finally:
        conn.close()
    if not isinstance(data, dict) or data.get('ok') is not True or 'result' not in data:
        raise BotError(f'{method} malformed or unsuccessful response')
    return data['result']


def pair(token, *, tell=print, timeout=180):
    bot = api(token, 'getMe', {})
    if (not isinstance(bot, dict) or bot.get('is_bot') is not True or not positive(bot.get('id'))
            or str(bot['id']) != token.split(':')[0] or not isinstance(bot.get('username'), str)
            or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', bot['username'])):
        raise BotError('getMe did not identify the requested bot')
    webhook = api(token, 'getWebhookInfo', {})
    if not isinstance(webhook, dict) or webhook.get('url') != '':
        raise BotError('Use a dedicated bot without a webhook or another poller')
    command = '/start ' + secrets.token_urlsafe(24)
    started = int(time.time())
    tell(f"In the private chat with @{bot['username']}, send exactly: {command}", flush=True)
    deadline = time.monotonic() + timeout
    offset = 0
    while time.monotonic() < deadline:
        updates = api(token, 'getUpdates', {'offset': offset, 'limit': 25, 'timeout': 8,
                                            'allowed_updates': ['message']})
        if not isinstance(updates, list):
            raise BotError('getUpdates did not return an update list')
        for update in updates:
            if not isinstance(update, dict) or not positive(update.get('update_id')):
                raise BotError('Malformed update identifier')
            offset = max(offset, update['update_id'] + 1)
            msg = update.get('message')
            if not isinstance(msg, dict) or not isinstance(msg.get('text'), str):
                continue
            sender, chat, when = msg.get('from'), msg.get('chat'), msg.get('date')
            if (isinstance(chat, dict) and chat.get('type') == 'private' and positive(chat.get('id'))
                    and isinstance(sender, dict) and positive(sender.get('id')) and sender['id'] == chat['id']
                    and sender.get('is_bot') is False and 'sender_chat' not in msg and 'forward_origin' not in msg
                    and type(when) is int and started <= when <= time.time() + 10
                    and time.monotonic() < deadline and compare_digest(msg['text'].encode(), command.encode())):
                return {'token': token, 'chat_id': chat['id'], 'generation': secrets.token_hex(16)}
    raise BotError('Pairing expired; start again for a new code')


def send(binding, text, video=None):
    binding = validate_binding(binding)
    chat_id = binding['chat_id']
    fields = {'chat_id': chat_id}
    if video is None:
        method = 'sendMessage'
        fields['text'] = text
    else:
        method = 'sendVideo'
        fields.update(caption=text, supports_streaming='true')
    result = api(binding['token'], method, fields, video=video)
    chat = result.get('chat') if isinstance(result, dict) else None
    if (not isinstance(chat, dict) or chat.get('type') != 'private' or not positive(chat.get('id'))
            or chat['id'] != chat_id or not positive(result.get('message_id'))
            or result.get('message_thread_id') is not None or result.get('is_topic_message')):
        raise BotError(f'{method} recipient/message verification failed')
    if video is None:
        if result.get('text') != text:
            raise BotError('sendMessage text verification failed')
    else:
        item = result.get('video')
        if (result.get('caption') != text or not isinstance(item, dict)
                or not isinstance(item.get('file_id'), str) or not item['file_id']
                or not all(positive(item.get(k)) for k in ('width', 'height', 'duration'))
                or 'file_size' in item and not positive(item['file_size'])
                or 'mime_type' in item and item['mime_type'] != 'video/mp4'):
            raise BotError('sendVideo media verification failed')
    return {'message_id': result['message_id']}
