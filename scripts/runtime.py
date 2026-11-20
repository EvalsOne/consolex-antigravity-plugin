#!/usr/bin/env python3
"""Shared Codex, DSH and Antigravity events around the Claude Code ConsoleX client.

Hooks supply final text and tool evidence. The Codex Steps view also reads the
active task session’s rollout to place public model responses and tool requests.
`save` sends this session's journal, which is built from the same events.
The package builder puts this module and consolex_core.py beside each other.
"""
import contextlib
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import time
from urllib.parse import quote

_path = Path(__file__).with_name('consolex_core.py')
_spec = importlib.util.spec_from_file_location('consolex_core', _path)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

RUNTIMES = {'codex': 'Codex', 'deepseek_harness': 'DeepSeek Harness', 'antigravity': 'Antigravity'}
QUESTION_TOOLS = {'request_user_input', 'request_user_input_async', 'ask_user_question', 'ask_question'}
CONTROL_TOOLS = QUESTION_TOOLS | {'skill', 'Skill', 'update_plan'}
COMMAND_NAMES = '|'.join(name.split(':', 1)[1] for name in list(core.COMMANDS) + list(core.SCHEDULE_COMMANDS))
# `/consolex-tasks`: the command Skills in ~/.agents/skills, which DeepSeek Harness lists as slash commands too.
COMMAND = re.compile(r'^\s*(?:\$(?:consolex:)?consolex-|/consolex[-: ])(%s)\b\s*(.*)' % COMMAND_NAMES, re.S)
TASK = re.compile(r'^\s*(?:\$|/)(cx-[a-z0-9-]{1,70})(?![a-z0-9-])')
# A Skill picked from Codex's menu arrives as a link to its SKILL.md: `[$consolex-tasks](/Users/me/.agents/skills/…)`.
SKILL_LINK = re.compile(r'\[(\$[A-Za-z0-9][A-Za-z0-9:_-]*)\]\([^)\n]*\)')
# Only standalone, unambiguous requests end the whole task automatically. The agent can also
# acknowledge a more nuanced stop request with STOP_SIGNAL; questions and pauses stay open.
STOP_REQUEST = re.compile(
    r'^(?:(?:请|麻烦)\s*)?(?:停止|取消|结束)(?:一下|本次|这次|当前|这个)?(?:的)?'
    r'(?:任务|执行|运行|制作|生产|工作|节目制作)?(?:吧|了)?[。.!！]?$'
    r'|^(?:please\s+)?(?:stop|cancel|end)(?:\s+(?:this|the|current))?'
    r'(?:\s+(?:task|run|execution|work|production|it))?[.!]?$', re.IGNORECASE)
STOP_SIGNAL = '<!-- consolex:task-stopped -->'
CONTINUE_REQUEST = re.compile(r'^(?:继续|继续任务|继续执行|继续制作|resume|continue|continue the task)[。.!！]?$',
                              re.IGNORECASE)
STOP_GUIDANCE = ('If the owner explicitly cancels this entire ConsoleX task, stop work and append '
                 '<!-- consolex:task-stopped --> as the last line of your final acknowledgement, '
                 'without a ConsoleX delivery. Do not use it for a pause, question, or partial change.')
SPAWN_REFRESH = None  # Tests replace the detached refresher; None starts the real one.


def plain_prompt(prompt):
    """The prompt as typed: a picked Skill becomes `$name`, so it is a command and no local path is saved."""
    return SKILL_LINK.sub(r'\1', prompt)


def environment(env, runtime):
    if runtime not in RUNTIMES:
        raise ValueError('Unsupported runtime')
    env = {**env, 'CONSOLEX_RUNTIME': runtime}
    env['CONSOLEX_DATA'] = env.get('CONSOLEX_DATA') or (core.consolex_home(env) if runtime == 'antigravity'
                                                       else env.get('PLUGIN_DATA') or core.consolex_home(env))
    return env


def _context_text(output):
    return ((output or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')


def _with_stop_guidance(data, output):
    """Give the agent a structured way to report an explicit stop in ordinary language."""
    if not output:
        return core._context(data, STOP_GUIDANCE)
    result = dict(output)
    hook = dict(result.get('hookSpecificOutput') or {})
    hook['additionalContext'] = (_context_text(output) + '\n\n' + STOP_GUIDANCE).strip()
    result['hookSpecificOutput'] = hook
    return result


def _end_session_config(data, env):
    marker = core.load_marker(env, data.get('session_id'))
    if not marker:
        return None, None
    saved_api = marker.get('task_api_base')
    if saved_api and env.get('CONSOLEX_API_BASE') and (
            core.canonical_api_base(env['CONSOLEX_API_BASE']) != core.canonical_api_base(saved_api)):
        return marker, None
    _, config = core._session_config(env, data, allow_outdated=True)
    if (config and config.get('task_uuid') == marker.get('task_uuid') and
            (not saved_api or core.canonical_api_base(config.get('api_base')) == core.canonical_api_base(saved_api))):
        return marker, config
    if marker.get('task_uuid') and saved_api:
        return marker, {'task_uuid': marker['task_uuid'], 'api_base': saved_api}
    return marker, None


def _finish_stopped_task(data, env, http):
    """End only this chat's task; keep a retry marker if ConsoleX did not acknowledge it."""
    marker, config = _end_session_config(data, env)
    if not marker:
        return False
    marker['stop_sync_pending'] = True
    core.save_marker(env, marker)
    if config and core.session_event(config, env, data, marker, 'end', http=http):
        if marker.get('checkpoint_required'):
            # The owner decides a protected execution separately in ConsoleX. Preserve
            # its id and any uncertain effects, without reporting unrelated work as live.
            marker.pop('stop_sync_pending', None)
            marker['task_stopped'] = True
            marker['state'] = 'idle'
            core.save_marker(env, marker)
        else:
            core.clear_marker(env, data.get('session_id'))
        return True
    marker['state'] = 'idle'
    core.save_marker(env, marker)
    return False


def _record_interruption(data, env, http):
    """Close an interrupted attempt while retaining its marker for an explicit continuation."""
    marker, config = _end_session_config(data, env)
    if not marker:
        return False
    marker['host_interrupted'] = True
    marker['interrupt_sync_pending'] = True
    marker['state'] = 'idle'
    core.save_marker(env, marker)
    acknowledged = bool(config and core.session_event(config, env, data, marker, 'end', http=http))
    marker['state'] = 'idle'
    if acknowledged:
        marker.pop('interrupt_sync_pending', None)
    core.save_marker(env, marker)
    return acknowledged


def _codex_control(env, marker, http, *, acknowledge=False):
    saved_api = marker.get('task_api_base')
    if not saved_api:
        return None, {}
    data = {'session_id': marker['session_id']}
    _, config = _end_session_config(data, env)
    token = core._token(config, env) if config else None
    if not token:
        return None, {}
    path = 'sessions/%s/runs/%s/control' % (quote(marker['session_id'], safe=''), marker['run_control']['run_id'])
    return http('POST' if acknowledge else 'GET', core._task_url(config, env, path), token,
                timeout=core.SESSION_TIMEOUT)


def _stop_requested_from_consolex(data, env, marker, http):
    control = (marker or {}).get('run_control') or {}
    if not control.get('run_id') or data.get('turn_id') != control.get('turn_id'):
        return False
    if marker.get('remote_stop_run_id') == control['run_id']:
        return True
    status, body = _codex_control(env, marker, http)
    if status != 200 or not body.get('cancel_requested'):
        return False
    marker['remote_stop_run_id'] = control['run_id']
    core.save_marker(env, marker)
    return True


def _stop_at_hook(data, env, marker, http):
    message = 'The owner stopped this ConsoleX run. End this turn now. Do not run more tools or submit a delivery.'
    event = data['hook_event_name']
    if event == 'PreToolUse':
        return {'hookSpecificOutput': {'hookEventName': event, 'permissionDecision': 'deny',
                                       'permissionDecisionReason': message}}
    if event == 'PostToolUse':
        return {'decision': 'block', 'reason': message}
    # Codex has reached its final-response hook. Prevent other Stop hooks from
    # continuing the turn and acknowledge only this run to ConsoleX.
    status, body = _codex_control(env, marker, http, acknowledge=True)
    if status == 200 and body.get('cancelled'):
        if marker.get('checkpoint_required'):
            marker.update(task_stopped=True, state='idle')
            core.save_marker(env, marker)
        else:
            core.clear_marker(env, data['session_id'])
    return {'continue': False, 'stopReason': message}


def _codex_app_server_rpc(path, method, params):
    """One request to the host's private Unix App Server socket (WebSocket JSON-RPC)."""
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(2)
        connection.connect(os.path.realpath(path))
        key = base64.b64encode(os.urandom(16)).decode('ascii')
        connection.sendall((
            'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n'
            'Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n' % key).encode('ascii'))
        with connection.makefile('rb') as incoming:
            status = incoming.readline(4096)
            headers = {}
            for _ in range(32):
                line = incoming.readline(4096)
                if line == b'\r\n':
                    break
                name, value = line.split(b':', 1)
                headers[name.lower()] = value.strip()
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            if status.split()[1:2] != [b'101'] or headers.get(b'sec-websocket-accept') != accept:
                raise OSError('Codex App Server did not accept the connection')

            def send(payload, opcode=1):
                mask = os.urandom(4)
                length = len(payload)
                size = bytes([128 + length]) if length < 126 else b'\xfe' + struct.pack('!H', length)
                encoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
                connection.sendall(bytes([128 + opcode]) + size + mask + encoded)

            def request(name, arguments=None, request_id=None):
                wire = {'method': name}
                if arguments is not None:
                    wire['params'] = arguments
                if request_id is not None:
                    wire['id'] = request_id
                send(json.dumps(wire).encode())
                if request_id is None:
                    return None
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    flags, size = incoming.read(2)
                    if size == 126:
                        size, = struct.unpack('!H', incoming.read(2))
                    elif size == 127:
                        size, = struct.unpack('!Q', incoming.read(8))
                    if size > 1_048_576 or not flags & 128:
                        raise OSError('Unsupported Codex App Server frame')
                    raw = incoming.read(size)
                    opcode = flags & 15
                    if opcode == 9:
                        send(raw, opcode=10)
                    if opcode == 8:
                        break
                    if opcode == 1:
                        response = json.loads(raw)
                        if response.get('id') == request_id:
                            return response.get('result')
                return None

            if request('initialize', {'clientInfo': {'name': 'consolex', 'version': '1.9.11'}}, 1) is None:
                return None
            request('initialized')
            return request(method, params, 2)


def watch_codex_turn(env, session_id, run_id, turn_id, socket_path, http=core.http_json, rpc=_codex_app_server_rpc):
    """Interrupt only the registered turn, on a host that exposes this live thread."""
    try:
        thread = rpc(socket_path, 'thread/read', {'threadId': session_id, 'includeTurns': False})
        if ((thread or {}).get('thread') or {}).get('status', {}).get('type') != 'active':
            return
        binding = {'run_id': run_id, 'turn_id': turn_id}
        interrupted = False
        while True:
            with session_lock(env, session_id):
                marker = core.load_marker(env, session_id)
            if not marker or marker.get('run_control') != binding:
                return
            status, body = _codex_control(env, marker, http)
            if status in (401, 403, 404) or status == 200 and not body.get('running'):
                return
            if status == 200 and body.get('cancel_requested'):
                if not interrupted:
                    interrupted = rpc(socket_path, 'turn/interrupt', {'threadId': session_id, 'turnId': turn_id}) == {}
                    if not interrupted:
                        return  # The host refused this turn. Hooks can still stop it.
                status, body = _codex_control(env, marker, http, acknowledge=True)
                if status == 200 and body.get('cancelled'):
                    return
            time.sleep(4)
    except (OSError, ValueError, struct.error):
        return


def _native_text(text, runtime):
    if runtime not in RUNTIMES:
        raise ValueError('Unsupported runtime')
    return core.native_text(text, runtime)  # shared with the Codex MCP server's consolex_command tool


def adapt(output, runtime):
    if not output:
        return None
    output = dict(output)
    for key in ('reason', 'systemMessage'):
        if isinstance(output.get(key), str):
            output[key] = _native_text(output[key], runtime)
    if output.get('hookSpecificOutput'):
        output['hookSpecificOutput'] = dict(output['hookSpecificOutput'])
        output['hookSpecificOutput']['additionalContext'] = _native_text(_context_text(output), runtime)
    return output


def _answer(response):
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return False
    if not isinstance(response, dict) or response.get('isError') or response.get('is_error'):
        return False
    answers = response.get('answers')
    if isinstance(answers, dict):
        return any(isinstance(value, dict) and value.get('answers') for value in answers.values())
    if isinstance(answers, list):
        return any(isinstance(value, dict) and (value.get('selected') or value.get('custom')) for value in answers)
    # MCP/text wrappers still carry structured answers as text blocks.
    return any(_answer(part.get('text', '')) for part in response.get('content', []) if isinstance(part, dict))


def _evidence(marker, data):
    notes = marker.setdefault('tool_notes', {'dropped': 0, 'images': 0, 'clipped': 0})
    name = str(data.get('tool_name') or '')
    if name in CONTROL_TOOLS or name.startswith('mcp__codex_app__'):
        return
    inputs = core.redact(core._tool_input(name, data.get('tool_input')))
    response = data.get('tool_response')
    if isinstance(response, dict) and isinstance(response.get('content'), list):
        text, images = core._result_text(response['content'], None)
    else:
        text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        images = 0
    inputs, clipped_input = core._clip(inputs, core.MAX_TOOL_INPUT_CHARS)
    redacted = core.redact(text)
    text, clipped = core._clip(redacted, core.MAX_TOOL_OUTPUT_CHARS)
    notes['images'] += images
    notes['clipped'] += int(clipped or clipped_input)
    records = marker.setdefault('tools', [])
    call_id = str(data.get('tool_use_id') or '')
    # Tool completion callbacks may be redelivered by a carrier.
    if call_id and any(item.get('_id') == call_id for item in records):
        return
    failed = bool(data.get('is_error') or isinstance(response, dict) and
                  (response.get('is_error') or response.get('isError')))
    incomplete = bool(data.get('source_incomplete'))
    record = {'name': name[:200], 'input': inputs, 'output': text, 'clipped': clipped or incomplete,
              'is_error': failed, '_id': call_id}
    if incomplete:
        record['source_incomplete'] = True
    # Retain a bounded, redacted complete output locally only when opted in.
    if (marker.get('recover_evidence') is True and clipped and not failed and not images
            and not clipped_input and not incomplete and len(redacted) <= 60000
            and sum(len(item.get('_recovery_output', '')) for item in records) + len(redacted) <= 120000):
        record['_recovery_output'] = redacted
    records.append(record)
    while len(records) > core.MAX_TOOL_RECORDS or sum(len(i['input']) + len(i['output']) for i in records) > core.MAX_TOOL_TOTAL_CHARS:
        records.pop(0)
        notes['dropped'] += 1


def _activate_from_tool(data, env, http):
    name = data.get('tool_name')
    args = data.get('tool_input') or {}
    candidate = args.get('name') or args.get('skill') if isinstance(args, dict) else None
    if name in ('skill', 'Skill') and candidate:
        folder = core.find_skill_dir(str(candidate).split(':')[-1], env, data.get('cwd') or os.getcwd())
        if folder:
            return core.activate(data, env, folder, http=http)
    # Reading/editing SKILL.md is also how maintenance chats inspect a task. It is
    # not an invocation. Codex task activation comes from the owner's leading
    # $cx-… / picked Skill or $consolex-start, never arbitrary shell/file input.
    return None


def _start_context_path(env, token):
    if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
        return None
    return os.path.join(os.path.dirname(core._state_dir(env)), 'start-calls', token + '.json')


def prepare_start_call(data, env):
    """Stamp an explicit start with the calling chat, never an agent-supplied session id.

    Codex's MCP process has no chat id. Its trusted PreToolUse hook does, and
    updatedInput delivers this one-use receipt to the MCP call even when a
    scheduler supplied the task as tool output rather than a user prompt.
    """
    args = dict(data.get('tool_input') or {})
    args.pop('_activation_context', None)
    if (not data.get('session_id') or not os.path.isdir(data.get('cwd') or '')
            or not isinstance(args.get('task'), str) or not args['task'].strip()
            or set(args) - {'task', 'request', 'unattended', 'schedule_id'}
            or 'unattended' in args and not isinstance(args['unattended'], bool)
            or 'request' in args and not isinstance(args['request'], str)
            or 'schedule_id' in args and not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', str(args['schedule_id']))):
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                'permissionDecisionReason': 'ConsoleX task not started: give task, and use this chat in an existing working folder.'}}
    token = os.urandom(16).hex()
    path = _start_context_path(env, token)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    for old in Path(path).parent.glob('*.json'):
        try:
            if time.time() - old.stat().st_mtime > 120:
                old.unlink()
        except OSError:
            pass
    core._write_private(path, {'expires_at': time.time() + 120, 'arguments': args,
        'data': {key: data[key] for key in ('session_id', 'turn_id', 'prompt_id', 'cwd', 'model') if key in data}})
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow',
                                   'updatedInput': {**args, '_activation_context': token}}}


def start_task_tool(env, arguments, http=core.http_json, runtime='codex'):
    """Explicit hook-bound start. Missing/untrusted hooks and stale methods fail closed."""
    env = environment(env, runtime)
    args = dict(arguments)
    path = _start_context_path(env, args.pop('_activation_context', None))
    context = None
    if path:
        claimed = path + '.claimed'
        owned = False
        try:
            os.rename(path, claimed)  # only one call can consume this receipt
            owned = True
            context = core._read_json(claimed, None)
        except OSError:
            pass
        finally:
            if owned:
                try:
                    os.remove(claimed)
                except OSError:
                    pass
    if (not isinstance(context, dict) or context.get('expires_at', 0) < time.time()
            or context.get('arguments') != args):
        return json.dumps({'status': 'not_started', 'error': 'No valid activation context from this chat. '
            'Restart the agent and review/trust the ConsoleX hooks. Do not execute or publish this task.'}), True
    data = {**context['data'], 'hook_event_name': 'ConsoleXStartTask', '_consolex_start': True,
            'unattended': args.get('unattended', False), 'schedule_id': args.get('schedule_id')}
    data['prompt_id'] = str(data.get('turn_id') or data.get('prompt_id') or 'turn')
    argument = args['task'].strip()
    folder = core.find_skill_dir(argument, env, data['cwd'])
    if folder:
        argument = core.load_config(folder).get('task_uuid') or argument
    if args.get('request'):
        argument += ' -- ' + args['request']
    with session_lock(env, data['session_id']):
        previous = core.load_marker(env, data['session_id'])
        if previous:
            previous['activation_ready'] = False
            previous['explicit_start_pending'] = True
            core.save_marker(env, previous)
        output, task = core.start_task(data, env, argument, http=http)
        marker = core.load_marker(env, data['session_id']) or {}
        instructions = _native_text(_context_text(output), runtime)
        if not task or marker.get('task_uuid') != task['uuid'] or not marker.get('activation_ready'):
            return json.dumps({'status': 'not_started', 'instructions': instructions,
                'error': 'ConsoleX could not activate this task with its latest method. Do not execute or publish it.'},
                ensure_ascii=False), True
        _journal_prompt(env, data, 'ConsoleX task: ' + argument, command=False, synthetic=False)
        task_reference = os.path.basename(marker['skill_dir'])
        return json.dumps({'status': 'started', 'task_uuid': task['uuid'], 'task_name': task['name'],
            'session_id': data['session_id'], 'skill': task_reference, 'task_reference': task_reference,
            'task_skill_installed': os.path.isfile(os.path.join(marker['skill_dir'], 'SKILL.md')),
            'method_version': (marker.get('revision') or {}).get('version'),
            'unattended': marker.get('unattended', False), 'schedule_id': marker.get('schedule_id'),
            'instructions': instructions}, ensure_ascii=False), False


# ---------------------------------------------------------------- session journal (for save)
#
# `save` sends the owner's messages, the final reply and the tool records of the whole session. Claude Code
# keeps a transcript for that; here they come only from the runtime's own events, so every session keeps a
# small journal beside its marker. It stays on this computer (0600, keys masked, capped like the upload) and
# is removed after a week without activity; nothing is sent unless the owner runs the save command.

def _journal_path(env, session_id):
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', str(session_id or ''))[:128]
    return os.path.join(os.path.dirname(core._state_dir(env)), 'journal', safe + '.json') if safe else None


def load_journal(env, session_id):
    path = _journal_path(env, session_id)
    journal = core._read_json(path, None) if path else None
    if isinstance(journal, dict) and journal.get('session_id') == str(session_id):
        return journal
    return {'session_id': str(session_id or ''), 'started_at': time.time(), 'messages': [], 'reply': '', 'model': None}


def save_journal(env, journal):
    path = _journal_path(env, journal.get('session_id'))
    if not path:
        return
    if not os.path.exists(path):
        _sweep(env, os.path.dirname(path))  # A new session: drop what week-old sessions left behind.
    core._write_private(path, journal)


def _sweep(env, journal_dir):
    for folder in (core._state_dir(env), journal_dir):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            full = os.path.join(folder, name)
            try:
                if time.time() - os.path.getmtime(full) > core.MARKER_TTL_SECONDS:
                    os.remove(full)
            except OSError:
                pass


def _journal_prompt(env, data, prompt, *, command, synthetic):
    """An owner's prompt; ConsoleX commands and plugin continuations are not the owner's request."""
    if not data.get('session_id') or synthetic:
        return
    journal = load_journal(env, data['session_id'])
    if command:
        # The reply to a ConsoleX command only relays its result; it is not the session's result.
        journal['command_turn'] = data['prompt_id']
    else:
        journal.pop('command_turn', None)
        _note_skills(journal, _SKILL_MENTION.findall(prompt or ''))  # `$name` loads a Skill in Codex
        if prompt:
            messages = journal.setdefault('messages', [])
            messages.append(core._clip(core.redact(prompt), core.MAX_SAVED_MESSAGE_CHARS)[0])
            del messages[:-core.MAX_SAVED_MESSAGES]
    if data.get('model') and not journal.get('model'):
        journal['model'] = str(data['model'])[:128]  # Until a reply names the model that wrote it.
    save_journal(env, journal)


def _journal_reply(env, data, *, report):
    """The latest reply that answers the owner, not one that relays a command or reports a ConsoleX check."""
    if not data.get('session_id'):
        return
    journal = load_journal(env, data['session_id'])
    reply = data.get('last_assistant_message')
    if not report and data['prompt_id'] != journal.get('command_turn') and isinstance(reply, str) and reply.strip():
        journal['reply'] = core._clip(core.redact(reply.strip()), core.MAX_SAVED_REPLY_CHARS)[0]
        if data.get('model'):
            journal['model'] = str(data['model'])[:128]
    save_journal(env, journal)


# A Skill the agent read: Codex reads a Skill's SKILL.md with a shell command; DSH loads one with its Skill tool.
_SKILL_FILE = re.compile(r'(/[^\s"\'`]*?/skills/[^/\s"\'`]+)/SKILL\.md')
SKILL_TOOLS = ('Skill', 'skill', 'load_skill', 'skills_load')
_SKILL_MENTION = re.compile(r'(?:^|\s)\$([A-Za-z0-9][A-Za-z0-9._-]{1,127})\b')
MAX_JOURNAL_SKILLS = 30


def _note_skills(journal, values):
    """Remember Skills this session used (folders or names), latest last, for /consolex:save."""
    skills = journal.setdefault('skills', [])
    for value in values:
        value = str(value or '').strip()
        if not value or value.startswith(('consolex', 'cx-')) or os.path.basename(value).startswith(('consolex', 'cx-')):
            continue
        if value in skills:
            skills.remove(value)
        skills.append(value)
    del skills[:-MAX_JOURNAL_SKILLS]


def _skills_in_tool(data):
    name = str(data.get('tool_name') or '')
    tool_input = data.get('tool_input')
    if name in SKILL_TOOLS and isinstance(tool_input, dict):
        return [tool_input.get('skill') or tool_input.get('name') or '']
    text = tool_input if isinstance(tool_input, str) else json.dumps(tool_input, ensure_ascii=False)
    return _SKILL_FILE.findall(text or '')


def _journal_tool(env, data):
    if not data.get('session_id'):
        return
    journal = load_journal(env, data['session_id'])
    # Which Skills it read is kept apart from the tool records (capped at 80), so an early read is never lost.
    _note_skills(journal, _skills_in_tool(data))
    if not _evidence_off(env):
        _evidence(journal, data)
    save_journal(env, journal)


def _evidence_off(env):
    return str(env.get('CONSOLEX_TOOL_EVIDENCE', '')).lower() in ('off', '0', 'false', 'no')


def _records(holder):
    return [{k: v for k, v in item.items() if not k.startswith('_')} for item in holder.get('tools', [])]


def _delivery_records(marker, runtime):
    records = _records(marker)
    if runtime == 'codex':
        for item, captured in zip(records, marker.get('tools', [])):
            if captured.get('_id'):
                item['capture_id'] = captured['_id']
    return records


def _save_inputs(env, data):
    """What `save` sends, from this session's journal (never from another runtime's log)."""
    journal = load_journal(env, data.get('session_id'))
    data['session_messages'] = list(journal.get('messages') or [])
    data['final_reply'] = journal.get('reply') or ''
    data['model'] = journal.get('model') or data.get('model')
    data['tools'] = _records(journal)
    data['tool_notes'] = journal.get('tool_notes', {})
    data['skills_read'] = list(journal.get('skills') or [])


NO_EXECUTION = ('This conversation has no stage-check execution for this ConsoleX task. Start the task here first (/cx-… '
                'or /consolex:start), then hand its stages in; another conversation\'s execution_id cannot be used.')


def _native_execution(env, data, need_own=False):
    """(execution id, marker, problem) for a DSH native ConsoleX tool call.

    The server binds an execution to its owner, task and computer, not to the conversation, so these tools (the plugin's
    own) do: an execution id is only ever this conversation's own, the one its session marker got when it started this
    task. A supplied id must be that one, for the task the call names; a conversation that did not start the task, a
    foreign id or another task is refused. Left out, the conversation's own is filled in. `need_own`: the call needs an
    execution (consolex_checkpoint); otherwise a conversation without stage checks calls without one, as before.
    """
    given = data.get('execution_id') if isinstance(data.get('execution_id'), str) and data.get('execution_id') else None
    marker = core.load_marker(env, data.get('session_id'))
    own = marker.get('execution_id') if marker and marker.get('checkpoint_required') else None
    if not own:
        if given or need_own:
            return None, marker, NO_EXECUTION
        return None, marker, ''
    config, _ = core._task_ref(env, data.get('task'), data.get('cwd'))
    same_task = bool(config) and config.get('task_uuid') == marker.get('task_uuid')
    if given and (given != own or not same_task) or need_own and not same_task:
        return None, marker, NO_EXECUTION + (' This conversation\'s execution for its own task is "%s".' % own if same_task else '')
    return own, marker, ''


def _native_stage_tool(data, env, http):
    """DSH's native consolex_checkpoint: a stage handed in, or where the stages stand (core.checkpoint_tool)."""
    execution_id, _, problem = _native_execution(env, data, need_own=True)
    if problem:
        return {'text': _native_text(problem, env['CONSOLEX_RUNTIME']), 'is_error': True}
    text, is_error = core.checkpoint_tool(env, data.get('task'), execution_id, data.get('operation'), data.get('stage_key'),
                                          data.get('text'), http=http, cwd=data.get('cwd'), session_id=data.get('session_id'))
    return {'text': core.command_text(text, env) if is_error else text, 'is_error': is_error}


def _native_task_tool(data, env, http):
    """DSH's native consolex_run_task_tool. In a conversation doing a task with stage checks the call carries the
    execution id and gets the same early look as a Claude Code or Codex hook gives (the server checks again)."""
    execution_id, marker, problem = _native_execution(env, data)
    if problem:
        return {'text': _native_text(problem, env['CONSOLEX_RUNTIME']), 'is_error': True}
    if marker and marker.get('checkpoint_required'):
        tool_input = {key: data[key] for key in ('task', 'tool', 'arguments') if key in data}
        if execution_id:
            tool_input['execution_id'] = execution_id
        guarded = core.checkpoint_hook({'hook_event_name': 'PreToolUse', 'session_id': data.get('session_id'),
                                        'cwd': data.get('cwd'), 'tool_name': core.RUN_TASK_TOOL,
                                        'tool_input': tool_input}, env, http=http)
        hook = (guarded or {}).get('hookSpecificOutput') or {}
        if hook.get('permissionDecision') == 'deny':
            return {'text': _native_text(str(hook.get('permissionDecisionReason') or 'ConsoleX stage check blocked this action.'),
                                         env['CONSOLEX_RUNTIME']), 'is_error': True}
    text, is_error = core.run_task_tool(env, data.get('task'), data.get('tool'), data.get('arguments'),
                                        data.get('session_id'), http=http, cwd=data.get('cwd'), execution_id=execution_id)
    return {'text': core.command_text(text, env) if is_error else text, 'is_error': is_error}


def task_tool_event(data, env, http=core.http_json):
    """DSH's native ConsoleX tools: {text, is_error}. The session id records the calls against this conversation,
    so a delivery's check sees them as calls ConsoleX ran itself."""
    if data.get('hook_event_name') == 'ConsoleXCheckpoint':
        return _native_stage_tool(data, env, http)
    if data.get('hook_event_name') == 'ConsoleXCommand':
        # The user asked in plain words for a ConsoleX command (add, remove, status …): the same result as typing it.
        text, is_error = core.command_for_agent(env, data.get('command'), data.get('arguments'), data.get('session_id'),
                                                data.get('cwd'), http=http)
        return {'text': text, 'is_error': is_error}
    if data.get('hook_event_name') == 'ConsoleXRenameSkill':
        name, problem = core.rename_task_skill(env, data.get('skill'), data.get('name'), data.get('cwd'))
        return {'text': problem or _native_text('Renamed. The task Skill is now /%s.' % name, env['CONSOLEX_RUNTIME']),
                'is_error': bool(problem)}
    if data.get('hook_event_name') == 'ConsoleXSave':
        # Step two of /consolex save: the session with the task's main skill, read from disk.
        text, is_error = core.save_session(env, data.get('save_id'), data.get('skill_path'), data.get('rules'),
                                           data.get('name'), http=http, result_id=data.get('result_id'), acceptance_mode=data.get('acceptance_mode'))
        return {'text': _native_text(text, env['CONSOLEX_RUNTIME']), 'is_error': is_error}
    if data.get('hook_event_name') == 'ConsoleXTaskTools':
        text, is_error = core.task_tools_reply(env, data.get('task'), http=http, cwd=data.get('cwd'))
        return {'text': text, 'is_error': is_error}
    return _native_task_tool(data, env, http)


def handle(data, env, runtime, http=core.http_json):
    env = environment(env, runtime)
    if data.get('hook_event_name') in ('ConsoleXTaskTools', 'ConsoleXRunTaskTool', 'ConsoleXRenameSkill', 'ConsoleXCommand',
                                       'ConsoleXSave', 'ConsoleXCheckpoint'):
        return task_tool_event(data, env, http=http)
    data = dict(data)
    data['prompt_id'] = str(data.get('turn_id') or data.get('prompt_id') or 'turn')
    # Never ask the Claude transcript parser to interpret another runtime's log.
    transcript = data.pop('transcript_path', None)
    if runtime == 'codex':
        data['_codex_transcript'] = transcript
    event = data.get('hook_event_name')
    if event == 'Stop':
        data['_stop_deadline'] = time.monotonic() + core.STOP_CHECK_BUDGET
    marker = core.load_marker(env, data.get('session_id'))
    resuming_stopped = False
    if runtime == 'codex' and marker and marker.get('stop_sync_pending'):
        # A previous end request may have reached the server despite a timeout. Retry before
        # accepting another task here, and never send a working heartbeat in between.
        if _finish_stopped_task(data, env, http):
            marker = core.load_marker(env, data.get('session_id'))
        elif event == 'UserPromptSubmit':
            return core._context(data, 'ConsoleX could not record that the previous task stopped. '
                                 'It will retry; do not start another ConsoleX task in this chat yet.')
        else:
            return None
    if runtime == 'codex' and marker and marker.get('task_stopped'):
        if event == 'UserPromptSubmit' and CONTINUE_REQUEST.fullmatch(plain_prompt(str(data.get('prompt') or '')).strip()):
            marker.pop('task_stopped', None)
            core.save_marker(env, marker)
            resuming_stopped = True
        elif event == 'UserPromptSubmit':
            return core._context(data, 'The task was stopped. Its stage-check execution is retained on Runs: '
                                 'review or cancel it there, or say continue to resume it in this chat. '
                                 'Do not restart the task implicitly.')
        else:
            return None
    if runtime == 'codex' and marker and marker.get('interrupt_sync_pending'):
        if _record_interruption(data, env, http):
            marker = core.load_marker(env, data.get('session_id'))
        elif event == 'UserPromptSubmit':
            return core._context(data, 'ConsoleX could not record the interrupted attempt. '
                                 'It will retry; do not start or continue a ConsoleX task in this chat yet.')
        else:
            return None
    if event == 'SessionStart':
        core.on_session_start(data, env, spawn=SPAWN_REFRESH)
        return None
    if event in ('SessionEnd', 'Interrupt'):
        core.SESSION_TIMEOUT = 2
        if marker:
            data['tools'] = _delivery_records(marker, runtime)
            data['tool_notes'] = marker.get('tool_notes', {})
        if runtime == 'codex' and event == 'Interrupt' and marker:
            control = marker.get('run_control') or {}
            if data.get('turn_id') and control.get('turn_id') and data['turn_id'] != control['turn_id']:
                return None
            if marker.get('stop_requested'):
                if _finish_stopped_task(data, env, http) and core.load_marker(env, data.get('session_id')):
                    return {'systemMessage': 'The task stopped. Review or cancel any stage-check execution on Runs.'}
            else:
                # An interrupt ends this attempt, but retains the task marker and stage execution
                # so an owner can continue in this chat. Late callbacks must not reopen it.
                _record_interruption(data, env, http)
        else:
            core.session_state(data, env, 'end' if event == 'SessionEnd' else 'idle', http=http)
        return None
    if event == 'UserPromptSubmit':
        prompt = plain_prompt(str(data.get('prompt') or '')).strip()
        # Stop-generated continuation is not a human answer, even if Codex
        # delivers it through UserPromptSubmit with a new turn id.
        tag = (marker or {}).get('continuation_tag')
        synthetic = bool(data.get('synthetic') or tag and tag in prompt)
        if marker:
            was_interrupted = bool(marker.get('host_interrupted'))
            if not synthetic:
                if runtime == 'codex':
                    marker.pop('host_interrupted', None)
                    marker['stop_requested'] = bool(STOP_REQUEST.fullmatch(prompt))
                marker.pop('awaiting_owner', None)
                marker['unattended'] = core.unattended(data, env, marker)
                marker.pop('review_gate', None)
                marker.pop('continuation_tag', None)
                marker.pop('continuation_chain', None)
                marker.pop('delivery_event', None)
                marker.pop('recovery_overrides', None)
                for item in marker.get('tools') or []:
                    item.pop('_recovery_output', None)
                # A new owner turn takes a fresh opt-in snapshot. If the task
                # was disabled or the server is unavailable, do not retain the
                # additional local evidence.
                _, config = core._session_config(env, data)
                core.refresh_recovery_preference(marker, config, env, http=http)
                marker['owner_answered_at'] = time.time()
                core.save_marker(env, marker)
            if (runtime == 'codex' and marker.get('run_control') and not synthetic
                    and (was_interrupted or resuming_stopped) and CONTINUE_REQUEST.fullmatch(prompt)):
                _, config = _end_session_config(data, env)
                if config:
                    core.session_event(config, env, data, marker, 'start', http=http)
                    core.save_marker(env, marker)
            elif not (runtime == 'codex' and was_interrupted and marker.get('stop_requested')):
                if runtime == 'codex' and (was_interrupted or resuming_stopped):
                    # Older servers reopen an ended run when `seen` carries activity. Send an
                    # explicit empty snapshot too, so continuation needs no server upgrade.
                    data['_force_activity_snapshot'] = True
                core.heartbeat(data, env, http=http)
        # A Codex app scheduled run: the app puts the automation's name and id above the saved prompt.
        schedule_id, body = core.codex_automation(prompt)
        if runtime == 'codex':
            # A Codex app scheduled run starts, or an automation changed since the last sync: ConsoleX hears this
            # computer's schedules (in the background).
            core.sync_schedules_if_changed(env, force=bool(schedule_id))
        match = COMMAND.match(body)
        if marker and not synthetic and not schedule_id and not match and body:
            core.note_user_message(env, data.get('session_id'), body)
        journal_path = _journal_path(env, data.get('session_id'))
        if journal_path and not os.path.exists(journal_path):
            # A session's first prompt: its start, where a runtime has no SessionStart event (refresh is hourly at most).
            core.on_session_start(data, env, spawn=SPAWN_REFRESH)
        _journal_prompt(env, data, prompt, command=bool(match), synthetic=synthetic)
        if match:
            if match[1] == 'check' and marker:
                data['last_assistant_message'] = marker.get('last_assistant_message', '')
                data['tools'] = _delivery_records(marker, runtime)
                data['tool_notes'] = marker.get('tool_notes', {})
            if match[1] == 'save':
                _save_inputs(env, data)
            if match[1] == 'start':
                output, task = core.start_task(data, env, match[2], http=http)
                if task:
                    # Starting a task asks for work: keep its request and delivery when saving this session.
                    _journal_prompt(env, data, prompt, command=False, synthetic=synthetic)
                    if runtime == 'deepseek_harness':
                        output['consolexTask'] = {'name': task['name']}
            else:
                handler = core._command_handler('consolex:' + match[1], env)
                output = handler(data, env, match[2], http=http) if handler else core._context(data, (
                    'ConsoleX: scheduled runs are set up in the Codex app or the Claude desktop app; %s has no scheduler. '
                    'Tell the user in one line.') % RUNTIMES[runtime])
                if match[1] == 'save' and data.get('consolex_save_id') and runtime == 'deepseek_harness':
                    # A DSH command never reaches the model: the plugin wakes the agent for step two.
                    output = {**output, 'consolexSave': {'id': data['consolex_save_id']}}
            return adapt(output, runtime)
        match = TASK.search(body)
        if match:
            folder = core.find_skill_dir(match[1], env, data.get('cwd') or os.getcwd())
            if folder:
                # New scheduled prompts always call the explicit start tool.
                # If the owner edited the prompt in the app it arrives as a
                # user message, so activating here as well starts the same
                # task twice and repeats its method in the model context.
                config = core.load_config(folder)
                task_uuid = config.get('task_uuid')
                explicit = ('先调用 ConsoleX 的 consolex_start_task 工具' in body or
                            'First call the ConsoleX consolex_start_task tool' in body)
                if (runtime == 'codex' and schedule_id and not marker and task_uuid and explicit and
                        core._explicit_schedule_task_uuid(body) == str(task_uuid).lower()):
                    return None
                output = core.activate(data, env, folder, http=http)
                if not synthetic and not schedule_id and body:
                    core.note_user_message(env, data.get('session_id'), body)
                return adapt(output, runtime)
        guarded = core.checkpoint_hook(data, env, http=http)
        if guarded:
            if (runtime == 'codex' and marker and not synthetic and guarded.get('decision') != 'block'
                    and guarded.get('hookSpecificOutput')):
                guarded = _with_stop_guidance(data, guarded)
            return adapt(guarded, runtime)
        if runtime == 'codex' and marker and not synthetic:
            return adapt(_with_stop_guidance(data, None), runtime)
        return None
    if runtime == 'codex' and marker and marker.get('host_interrupted'):
        return None  # A late tool/Stop callback from the interrupted turn is not new work.
    if (runtime == 'codex' and event in ('PreToolUse', 'PostToolUse', 'Stop')
            and _stop_requested_from_consolex(data, env, marker, http)):
        return _stop_at_hook(data, env, marker, http)
    if event == 'PreToolUse':
        if runtime in ('codex', 'antigravity') and data.get('tool_name') == 'mcp__consolex__' + core.START_TASK_TOOL:
            return prepare_start_call(data, env)
        guarded = core.checkpoint_hook(data, env, http=http)
        if guarded:
            return adapt(guarded, runtime)
    if event in ('PreToolUse', 'PostToolUse') and data.get('tool_name') in QUESTION_TOOLS:
        if marker:
            if event == 'PreToolUse':
                core.session_state(data, env, 'ask', http=http)
            elif not data.get('is_error') and _answer(data.get('tool_response')):
                answer_text = core._extract_answer_text(data)
                if answer_text:
                    core.note_user_message(env, data.get('session_id'), answer_text)
                    marker = core.load_marker(env, data.get('session_id')) or marker
                marker.pop('awaiting_owner', None)
                marker.pop('review_gate', None)
                marker.pop('continuation_chain', None)
                marker.pop('delivery_event', None)
                marker['owner_answered_at'] = time.time()
                core.save_marker(env, marker)
                core.heartbeat(data, env, http=http)
        return None
    if event == 'PostToolUse' and runtime == 'codex' and core.is_codex_schedule_tool(data.get('tool_name')):
        # The agent created, changed or deleted a Codex app scheduled task: ConsoleX hears it at once.
        return adapt(core.on_codex_schedule_changed(data, env, http=http), runtime)
    if event == 'PostToolUse':
        # A tool that names a skill folder may have edited it (a patch, a shell command): synced when the turn ends.
        core.note_method_edit(env, data.get('session_id'), core.skill_paths_in(data.get('tool_input') or {}, data.get('cwd')))
        output = _activate_from_tool(data, env, http)
        marker = core.load_marker(env, data.get('session_id'))
        if marker and not _evidence_off(env):
            _evidence(marker, data)
            core.save_marker(env, marker)
        _journal_tool(env, data)
        if marker:
            data['tools'] = _delivery_records(marker, runtime)
            data['tool_notes'] = marker.get('tool_notes', {})
            core.heartbeat(data, env, http=http)
        return adapt(output, runtime)
    if event != 'Stop':
        return None
    final_text = str(data.get('last_assistant_message') or '')
    signalled_stop = (core._without_fenced_code(final_text).rstrip().endswith(STOP_SIGNAL)
                      and not core.parse_delivery(final_text)) if runtime == 'codex' else False
    if runtime == 'codex' and marker and (marker.get('stop_requested') or signalled_stop):
        data['tools'] = _delivery_records(marker, runtime)
        data['tool_notes'] = marker.get('tool_notes', {})
        if not _finish_stopped_task(data, env, http):
            return {'systemMessage': 'ConsoleX could not record that this task stopped; it will retry on later activity.'}
        if core.load_marker(env, data.get('session_id')):
            return {'systemMessage': 'The task stopped. Review or cancel any stage-check execution on Runs.'}
        return None
    # The turn ends: skills it edited reach ConsoleX now, as one version each (the task's own skill is synced by the
    # check below for a task session).
    # The turn ConsoleX continued to report a check result: Codex's review gate, or DSH's report step.
    # Older clients set the review gate even when the plugin itself requested a
    # revision. Recover that pending continuation as well as new ones.
    pending = (marker or {}).get('continuation_chain')
    if marker and not pending and marker.get('review_gate'):
        pending_chains = [key for key, chain in (marker.get('chains') or {}).items() if chain.get('sent_back')]
        if len(pending_chains) == 1:
            pending = pending_chains[0]
    if pending and marker and (marker.get('chains') or {}).get(pending, {}).get('sent_back'):
        marker.pop('review_gate', None)
        marker['continuation_chain'] = pending
        data['prompt_id'] = pending
    else:
        pending = None
    # DSH marks every steered continuation as a report, including a revision;
    # the persisted, explicitly authorized pending chain is more specific.
    recovery_pending = core.pending_recovery(((marker or {}).get('chains') or {}).get(data['prompt_id']))
    report = not recovery_pending and bool(data.get('consolex_report') and not pending or marker and marker.get('review_gate'))
    core.sync_noted_methods(env, data.get('session_id'), data.get('cwd') or os.getcwd(), http=http,
                            skip_task=(marker or {}).get('task_uuid') if marker and not report else None)
    _journal_reply(env, data, report=report)
    if not marker:
        return None
    if report:
        core.end_turn(data, env, http=http)
        return None  # Keep the checked delivery as the reply for a later manual check.
    data['last_assistant_message'] = data.get('last_assistant_message') or ''
    marker['last_assistant_message'] = data['last_assistant_message']
    core.save_marker(env, marker)
    data['stop_hook_active'] = False
    data['tools'] = _delivery_records(marker, runtime)
    data['tool_notes'] = marker.get('tool_notes', {})
    # Duplicate Stop events reuse the existing idempotency key, including after
    # network uncertainty. A different delivery or a new human turn gets a new attempt.
    digest = hashlib.sha256((data['prompt_id'] + '\0' + data['last_assistant_message']).encode()).hexdigest()
    previous = marker.get('delivery_event') or {}
    if previous.get('digest') == digest and previous.get('completed') and not recovery_pending:
        return {'systemMessage': previous.get('message', 'ConsoleX: already checked.')}
    if previous.get('digest') == digest and not recovery_pending:
        marker['attempts'] = max(0, previous['attempt'] - 1)
    if pending and core.parse_delivery(data['last_assistant_message']):
        # Consume this authorization before checking the revision. A later
        # report-only block must not inherit the preceding revision's flag.
        marker['chains'][pending]['sent_back'] = False
        marker.pop('continuation_chain', None)
    marker['delivery_event'] = {'digest': digest, 'attempt': int(marker.get('attempts') or 0) + 1}
    if runtime == 'deepseek_harness':
        marker.pop('stage_held', None)  # set again by this Stop's stage checks only if they hold the run now
    core.save_marker(env, marker)
    output = core.handle(data, env, http=http)
    marker = core.load_marker(env, data.get('session_id')) or marker
    held = runtime == 'deepseek_harness' and _stage_hold(marker, data['prompt_id'])
    if held:
        # DSH steers one report step for every answer a Stop gives. While stage checks hold the run (paused for the owner,
        # or a single-stage plan waiting), a pending revision cannot go on: suspend it, so that report step's stop ends
        # the turn instead of being taken for the revision again (which would report the hold over and over).
        for key in {pending, data['prompt_id']} - {None}:
            if key in (marker.get('chains') or {}):
                marker['chains'][key]['sent_back'] = False
        marker.pop('continuation_chain', None)
    if output:
        if output.get('decision') == 'block':
            tag = '[ConsoleX continuation %s]' % digest[:16]
            chain = (marker.get('chains') or {}).get(data['prompt_id']) or {}
            if chain.get('sent_back'):
                marker.pop('review_gate', None)
                marker['continuation_chain'] = data['prompt_id']
            else:
                marker['review_gate'] = True
                marker.pop('continuation_chain', None)
            marker['continuation_tag'] = tag
            output['reason'] = tag + '\n' + output['reason']
        # Unreachable submissions are retryable with exactly the same attempt.
        chain = (marker.get('chains') or {}).get(data['prompt_id']) or {}
        # Whether the delivery was checked comes from the result line as the check made it. When a stage report was added
        # to the line (free text from the stages' own requirements), core kept the line it was added to.
        checked_line = marker['result_message'] if 'result_message' in marker else output.get('systemMessage')
        marker['delivery_event']['completed'] = bool(not core.pending_recovery(chain) and checked_line and
            'not checked' not in checked_line.lower() and '没有检查' not in checked_line)
        if pending and not held and not marker['delivery_event']['completed'] and output.get('decision') != 'block':
            marker['chains'][pending]['sent_back'] = True
            marker['continuation_chain'] = pending
        marker['delivery_event']['message'] = core._plain_text(output.get('systemMessage', ''))  # as the owner reads it
        marker.pop('result_message', None)
        core.save_marker(env, marker)
    return adapt(output, runtime)


def _stage_hold(marker, prompt_id):
    """True when this very Stop was answered by the stage checks holding the run, not by a delivery check: the server
    paused it (also a pause remembered while it is unreachable), or a single-stage plan reported its wait. Decided from
    this Stop's own outcome (`stage_held`, dropped before the Stop and set by core.checkpoint_hook), never from an earlier
    report of the same turn: DSH can take the owner's "continue" into that turn after the run went on."""
    return bool(marker and marker.get('checkpoint_required') and marker.get('stage_held') == prompt_id)


@contextlib.contextmanager
def session_lock(env, session_id):
    """Serialize parallel tool hooks and Stop without losing evidence or attempts."""
    path = core._marker_path(env, session_id)
    if not path:
        yield
        return
    with open(path + '.lock', 'a+b') as lock:
        os.utime(path + '.lock')  # In use: keep it out of the week-old sweep.
        if os.name == 'nt':
            import msvcrt
            lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def main():
    runtime = os.environ.get('CONSOLEX_RUNTIME', 'codex')
    env = environment(dict(os.environ), runtime)
    if runtime == 'codex' and len(sys.argv) == 6 and sys.argv[1] == '--watch-codex-turn':
        _, _, session_id, run_id, turn_id, path = sys.argv
        watch_codex_turn(env, session_id, int(run_id), turn_id, path)
        return
    try:
        data = json.load(sys.stdin)
        with session_lock(env, data.get('session_id')):
            output = handle(data, env, runtime)
    except Exception as exc:
        output = {'systemMessage': 'ConsoleX: check unavailable (%s); not checked.' % type(exc).__name__}
    if output:
        print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
