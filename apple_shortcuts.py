"""Generate native Shortcuts files, then sign locally with Apple's CLI."""
from pathlib import Path
import plistlib
import subprocess
import tempfile
import uuid

from presence_receiver import HEADER, PREFIX, diagnostic_secret


def text(value, attachment=None):
    data = {'string': value}
    if attachment:
        data['attachmentsByRange'] = {'{0, 1}': attachment}
    return {'WFSerializationType': 'WFTextTokenString', 'Value': data}


def reference(action, name):
    return {'Type': 'ActionOutput', 'OutputUUID': action, 'OutputName': name}


def dictionary(fields):
    return {'WFSerializationType': 'WFDictionaryFieldValue', 'Value': {'WFDictionaryFieldValueItems': [
        {'WFItemType': 0, 'WFKey': text(key), 'WFValue': value} for key, value in fields.items()]}}


def action(name, **parameters):
    return {'WFWorkflowActionIdentifier': 'is.workflow.actions.' + name, 'WFWorkflowActionParameters': parameters}


def workflow(host, secret, signal, *, diagnostic=False, public_control=False, template=False):
    if diagnostic:
        secret = diagnostic_secret(secret)
    date_id, http_id = (str(uuid.uuid4()).upper() for _ in range(2))
    actions = [
        action('format.date', UUID=date_id,
               WFDate=text('\ufffc', {'Type': 'CurrentDate'}),
               WFDateFormatStyle='ISO 8601', WFISO8601IncludeTime=True),
        action('downloadurl', UUID=http_id, WFURL=text(f'https://{host}:8443{PREFIX}/{signal}'),
               WFHTTPMethod='POST', WFHTTPBodyType='JSON', ShowHeaders=True,
               WFHTTPHeaders=dictionary({HEADER: text(secret), 'Content-Type': text('application/json')}),
               WFJSONValues=dictionary({'event_at': text('\ufffc', reference(date_id, 'Formatted Date'))})),
    ]
    if diagnostic:
        actions.append(action('showresult', Text=text('\ufffc', reference(http_id, 'Contents of URL'))))
    http = actions[1]['WFWorkflowActionParameters']
    if public_control:
        http['WFURL'] = text(f'https://{host}{PREFIX}/{signal}')
    prompts = []
    if template:
        key_id = str(uuid.uuid4()).upper()
        http['WFHTTPHeaders'] = dictionary({HEADER: text('\ufffc', reference(key_id, 'Text')),
                                            'Content-Type': text('application/json')})
        actions.insert(0, action('gettext', UUID=key_id, WFTextActionText='PASTE_SETUP_KEY'))
        prompts.append({'ActionIndex': 0, 'Category': 'Parameter', 'ParameterKey': 'WFTextActionText',
                        'Text': 'Sentry setup key', 'DefaultValue': 'PASTE_SETUP_KEY'})
    return {'WFWorkflowActions': actions, 'WFWorkflowClientVersion': '2605.0.5',
            'WFWorkflowMinimumClientVersion': 900, 'WFWorkflowMinimumClientVersionString': '900',
            'WFWorkflowTypes': [], 'WFWorkflowImportQuestions': prompts,
            'WFWorkflowIcon': {'WFWorkflowIconGlyphNumber': 59511, 'WFWorkflowIconStartColor': 4282601983}}


def sign_pair(state, config, *, diagnostic=False):
    with tempfile.TemporaryDirectory(prefix='shortcuts-', dir=state) as stage:
        output = Path(stage)/'signed'
        output.mkdir(mode=0o700)
        for value in ('away', 'home'):
            name = f'Sentry {value.title()}' + (' Test' if diagnostic else '')
            unsigned, signed = Path(stage)/f'{name}.shortcut', output/f'{name}.shortcut'
            unsigned.write_bytes(plistlib.dumps(workflow(config['phone_host'], '', value,
                                                        diagnostic=diagnostic, template=True,
                                                        public_control=config.get('phone_public', False)),
                                                fmt=plistlib.FMT_BINARY))
            subprocess.run(['/usr/bin/shortcuts', 'sign', '--mode', 'anyone', '--input', str(unsigned),
                            '--output', str(signed)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True, timeout=90)
            if signed.is_symlink() or not signed.is_file() or signed.stat().st_size == 0:
                raise OSError('Shortcuts signing did not produce a file')
            signed.chmod(0o600)
        key = config['phone_secret']
        key_file = output/'Sentry Setup Key.txt'
        with key_file.open('x') as file:
            key_file.chmod(0o600)
            file.write((diagnostic_secret(key) if diagnostic else key)+'\n')
        # Publish a fresh private directory, not a partial or overwritten pair.
        destination = state/('phone-' + uuid.uuid4().hex[:12])
        output.rename(destination)
    return destination
