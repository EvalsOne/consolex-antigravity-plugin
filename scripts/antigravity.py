#!/usr/bin/env python3
"""Antigravity's native hook contract, adapted to the shared ConsoleX client.

Only explicit USER_INPUT transcript steps are human turns. MODEL/GENERIC steps
are tool returns, never final replies; private `thinking` is never collected.
The transcript format was inspected on Antigravity App 2.33.0. Unknown formats
fail visibly instead of inventing prompts, tool outputs, or successful checks.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

_automation_spec = importlib.util.spec_from_file_location('consolex_antigravity_automation',
                                                         Path(__file__).with_name('automation.py'))
automation = importlib.util.module_from_spec(_automation_spec)
_automation_spec.loader.exec_module(automation)

_shared = Path(__file__).with_name('runtime.py')
if not _shared.is_file():  # Source checkout only; the builder includes its own copy.
    _shared = Path(__file__).resolve().parents[2] / 'coding_agents/runtime.py'
_spec = importlib.util.spec_from_file_location('consolex_antigravity_runtime', _shared)
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)
core = runtime.core
EVENTS = {'SessionStart', 'PreInvocation', 'PreToolUse', 'PostToolUse', 'Stop'}
SESSION = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


def environment(env):
    # Hooks and a plugin's long-lived MCP server must use the same data directory.
    # PLUGIN_DATA is not guaranteed to be supplied to hooks.
    return runtime.environment({**env, 'CONSOLEX_DATA': env.get('CONSOLEX_DATA') or core.consolex_home(env)},
                               'antigravity')


def read_transcript(payload, env):
    """Read only this conversation's host-owned transcript; bounded and UTF-8."""
    session = payload.get('conversationId')
    if not isinstance(session, str) or not SESSION.fullmatch(session):
        raise ValueError('invalid conversation identity')
    value = payload.get('transcriptPath')
    if not isinstance(value, str) or not value:
        raise ValueError('the host did not supply a transcript')
    path = Path(value).expanduser().resolve()
    home = Path(env.get('CONSOLEX_ANTIGRAVITY_HOME') or Path.home() / '.gemini').resolve()
    expected = {home / carrier / 'brain' / session / '.system_generated/logs' / filename
                for carrier in ('antigravity', 'antigravity-cli', 'antigravity-ide')
                for filename in ('transcript.jsonl', 'transcript_full.jsonl')}
    if path not in expected:
        raise ValueError('transcript does not belong to this conversation')
    with path.open('rb') as handle:
        size = handle.seek(0, 2)
        handle.seek(max(0, size - core.TRANSCRIPT_TAIL_BYTES))
        if size > core.TRANSCRIPT_TAIL_BYTES:
            handle.readline()  # A partial first JSON line cannot be interpreted.
        raw = handle.read(core.TRANSCRIPT_TAIL_BYTES)
    rows = {}
    for line in raw.decode('utf-8').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or type(row.get('step_index')) is not int:
            raise ValueError('unsupported transcript record')
        # A later record can update an in-progress step in place.
        rows[row['step_index']] = row
    return [rows[key] for key in sorted(rows)]


def _user_request(text):
    # App 2.33.0 wraps the owner's text before appending host metadata.
    match = re.match(r'^<USER_REQUEST>\r?\n(.*?)\r?\n</USER_REQUEST>(?:\r?\n|$)', text, re.S)
    return match[1] if match else text


def _carrier_path(env, session):
    return Path(core._state_dir(env)).parent / 'carrier' / (session + '.json')


def _context(output):
    return (output or {}).get('reason') or runtime._context_text(output) or (output or {}).get('systemMessage') or ''


def _injection(text):
    # Antigravity deprecated ephemeralMessage (it is not delivered to the model);
    # systemMessage requires the nested object shape per hooks.md.
    return {'injectSteps': [{'systemMessage': {'systemMessage': text}}]} if text else {}


def tool_call(payload):
    """Normalize direct eager tools and the lazy call_mcp_tool envelope alike."""
    call = payload.get('toolCall') or {}
    if not isinstance(call, dict):
        raise ValueError('invalid tool call')
    name, args = call.get('name') or '', call.get('args') or {}
    if not isinstance(name, str) or not isinstance(args, dict):
        raise ValueError('invalid tool arguments')
    lazy = name == 'call_mcp_tool' and isinstance(args, dict)
    if lazy:
        # Only this plugin's server can receive ConsoleX session credentials.
        if args.get('ServerName') != 'consolex_tools':
            return name, args, False
        name, args = args.get('ToolName') or '', args.get('Arguments') or {}
        if not isinstance(name, str) or not isinstance(args, dict):
            raise ValueError('invalid MCP envelope')
    elif name.startswith('mcp_consolex_tools_'):
        name = name[len('mcp_consolex_tools_'):]
    else:
        return name, args, False
    if name.startswith('consolex_'):
        name = 'mcp__consolex__' + name
    return name, args, lazy


def translate(output, event, payload):
    if not output:
        return {'decision': 'allow'} if event == 'PreToolUse' else {}
    if event == 'PreToolUse':
        hook = output.get('hookSpecificOutput') or {}
        if hook.get('permissionDecision') == 'deny' or output.get('decision') == 'block':
            return {'decision': 'deny', 'reason': hook.get('permissionDecisionReason') or _context(output)}
        result = {'decision': 'allow'}  # Preserve Antigravity's normal permission policy.
        if hook.get('updatedInput') is not None:
            _, _, lazy = tool_call(payload)
            if lazy:
                result['overwrite'] = {**payload['toolCall']['args'], 'Arguments': hook['updatedInput']}
            else:
                result['overwrite'] = hook['updatedInput']
        if _context(output):
            result['reason'] = _context(output)
        return result
    if event == 'Stop':
        return {'decision': 'continue', 'reason': _context(output)}
    return _injection(_context(output))


def _tool_events(rows, data, cursor, final=False):
    """Pair one native tool request with its adjacent GENERIC result.

    Never guess the result of multiple concurrent calls. Missing/truncated
    output is explicitly incomplete and cannot become successful evidence.
    """
    for pos, row in enumerate(rows):
        index = row['step_index']
        calls = row.get('tool_calls') or []
        if index <= cursor or row.get('source') != 'MODEL' or not calls:
            continue
        following = rows[pos + 1] if pos + 1 < len(rows) else {}
        paired = (len(calls) == 1 and following.get('step_index') == index + 1
                  and following.get('type') == 'GENERIC' and following.get('source') == 'MODEL'
                  and following.get('status') in ('DONE', 'ERROR'))
        if not paired and not final:
            break  # Revisit an unfinished call on the next hook.
        for ordinal, call in enumerate(calls):
            if not isinstance(call, dict) or not isinstance(call.get('name'), str):
                continue
            incomplete = bool(not paired or row.get('truncated_fields') or following.get('truncated_fields')
                              or following.get('media'))
            native = {'toolCall': call}
            name, args, _ = tool_call(native)
            yield {**data, 'hook_event_name': 'PostToolUse', 'tool_name': name,
                   'tool_input': args, 'tool_use_id': 'agy-%s-%s' % (index, ordinal),
                   'tool_response': following.get('content') or '' if paired else '',
                   'is_error': row.get('status') == 'ERROR' or following.get('status') == 'ERROR',
                   'source_incomplete': incomplete, '_step': index}


def _problem(event, message, state):
    if event == 'PostToolUse':
        return {}  # The host only accepts an empty object for this hook.
    text = 'ConsoleX: not checked. ' + message + '. Do not claim this task was started or checked.'
    if event == 'PreToolUse':
        return {'decision': 'deny', 'reason': text}
    if event == 'Stop':
        if state.get('error_reported'):
            return {}
        state['error_reported'] = True
        return {'decision': 'continue', 'reason': 'Report this once without doing more work: ' + text}
    return _injection(text)


def handle(payload, env, event, http=core.http_json):
    if event not in EVENTS or not isinstance(payload, dict):
        raise ValueError('unsupported hook event')
    env = environment(env)
    session = payload.get('conversationId')
    if not isinstance(session, str) or not SESSION.fullmatch(session):
        return _problem(event, 'Antigravity supplied no valid conversation identity', {})
    with runtime.session_lock(env, session):
        path = _carrier_path(env, session)
        state = core._read_json(str(path), {})
        try:
            result = _handle(payload, env, event, state, http)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            # Normal tools continue to obey host policy when ConsoleX is unavailable.
            try:
                name, _, _ = tool_call(payload)
            except (ValueError, TypeError):
                name = 'mcp__consolex__invalid'
            if event == 'PreToolUse' and not name.startswith('mcp__consolex__'):
                result = {'decision': 'allow'}
            else:
                result = _problem(event, 'Antigravity context unavailable (%s)' % type(exc).__name__, state)
        if event == 'Stop':
            result.setdefault('decision', 'stop')
        core._write_private(str(path), state)
        return result


def _handle(payload, env, event, state, http):
    session = payload['conversationId']
    folders = payload.get('workspacePaths') or []
    cwd = next((folder for folder in folders if isinstance(folder, str) and os.path.isdir(folder)), None)
    if not cwd:
        raise ValueError('no existing workspace supplied by the host')
    # A new session may not have created its transcript before SessionStart.
    data = {'session_id': session, 'turn_id': state.get('turn', 'session'), 'cwd': cwd,
            'model': payload.get('modelName'), 'hook_event_name': event}
    if event == 'SessionStart':
        try:
            automation.reconcile(core, env, http)
        except (OSError, ValueError):
            pass  # Task setup and scheduled starts verify their own automation state.
        return translate(runtime.handle(data, env, 'antigravity', http=http), event, payload)
    rows = read_transcript(payload, env)
    user = next((row for row in reversed(rows) if row.get('type') == 'USER_INPUT'
                 and row.get('source') == 'USER_EXPLICIT' and row.get('status') == 'DONE'), None)
    turn = 'user-%s' % user['step_index'] if user else state.get('turn', 'session')
    data = {'session_id': session, 'turn_id': turn, 'prompt_id': turn, 'cwd': cwd,
            'model': payload.get('modelName'), 'hook_event_name': event}
    if event == 'PreInvocation':
        if not user:
            return _problem(event, 'No explicit user turn was found in the transcript', state)
        if state.get('user_step') == user['step_index']:
            return {}  # Model calls and Stop continuations are not new user turns.
        state.update(user_step=user['step_index'], turn=turn, tool_cursor=user['step_index'])
        state.pop('error_reported', None)
        prompt = _user_request(user.get('content') or '')
        raw = payload.get('lastUserInput')
        if isinstance(raw, str):
            raw = _user_request(raw)
            if runtime.COMMAND.match(raw) or runtime.TASK.match(raw):
                prompt = raw  # A host can expand a selected Skill in the transcript.
        command = runtime.COMMAND.match(prompt)
        if command and command[1] == 'schedule':
            try:
                note = automation.prepare(core, env, data, prompt, http)
            except (OSError, ValueError) as exc:
                note = 'Could not prepare the Antigravity automation: %s.' % exc
            return _injection('Relay this setup result to the user. Do not run the task or enable the automation. '
                              + core.native_text(note, 'antigravity'))
        if prompt.startswith(automation.PROMPT_PREFIX):
            try:
                output, error = automation.start(core, env, data, prompt, user['step_index'] == 0, http)
            except (OSError, ValueError, KeyError, TypeError):
                output, error = None, 'The Antigravity automation settings could not be verified.'
            if error:
                return {'terminationBehavior': 'terminate', 'injectSteps': [
                    {'errorMessage': {'userMessage': 'ConsoleX automation did not start. ' + error}}]}
            state['automation_turn'] = turn
            return _injection(core.native_text(_context(output), 'antigravity'))
        output = runtime.handle({**data, 'hook_event_name': 'UserPromptSubmit',
                                 'prompt': prompt}, env, 'antigravity', http=http)
        return translate(output, event, payload)
    if user and state.get('user_step') != user['step_index']:
        # Missing prompt hooks cannot silently associate new work with an old task.
        name, _, _ = tool_call(payload)
        if event == 'PreToolUse' and not name.startswith('mcp__consolex__'):
            return {'decision': 'allow'}
        return _problem(event, 'The current user turn was not observed by the ConsoleX prompt hook', state)
    if event == 'PreToolUse':
        name, args, _ = tool_call(payload)
        if name == 'mcp__consolex__' + core.START_TASK_TOOL:
            if state.get('automation_turn') == turn:
                return {'decision': 'deny', 'reason': 'The native Automation already started its saved ConsoleX task.'}
            if args.get('unattended') or args.get('schedule_id') is not None:
                return {'decision': 'deny', 'reason': 'Scheduled starts require a verified Antigravity Automation event.'}
        # A question pauses this session; the next explicit user turn releases it.
        output = runtime.handle({**data, 'tool_name': name, 'tool_input': args}, env, 'antigravity', http=http)
        return translate(output, event, payload)
    cursor = state.get('tool_cursor', user['step_index'] if user else -1)
    for tool in _tool_events(rows, data, cursor, final=event == 'Stop' and payload.get('fullyIdle') is True):
        runtime.handle(tool, env, 'antigravity', http=http)
        state['tool_cursor'] = tool['_step']
    if event == 'PostToolUse':
        return {}  # This event's output schema is {}, context is injected before the next invocation.
    if payload.get('fullyIdle') is not True:
        return {}  # Background work still running; do not submit an early delivery.
    if payload.get('terminationReason') not in ('model_stop', 'NO_TOOL_CALL') or payload.get('error'):
        core.on_session_end(data, env, http=http)
        return {}
    final = next((row for row in reversed(rows) if row.get('type') == 'PLANNER_RESPONSE'
                  and row.get('source') == 'MODEL' and row.get('status') == 'DONE'
                  and not row.get('tool_calls') and row.get('content')
                  and user and row['step_index'] > user['step_index']), None)
    if not final:
        return {}
    if any(row['step_index'] > final['step_index'] and row.get('tool_calls') for row in rows):
        return {}  # An earlier draft is not the final reply after tool execution.
    if final.get('truncated_fields'):
        return _problem(event, 'The final reply was truncated in the host transcript', state)
    if state.get('stop_step') == final['step_index']:
        return {}
    output = runtime.handle({**data, 'hook_event_name': 'Stop',
                             'last_assistant_message': final['content']}, env, 'antigravity', http=http)
    if not output:
        state['stop_step'] = final['step_index']
        return {}
    marker = core.load_marker(env, session)
    if output.get('decision') != 'block':
        # Antigravity cannot display Claude/Codex's systemMessage. One bounded
        # reporting continuation puts its receipt in the chat instead.
        if marker:
            marker['review_gate'] = True
            core.save_marker(env, marker)
        output = {'reason': 'Report the following ConsoleX receipt once. Do not modify files, call work tools, '
                           'or submit another delivery in this reporting turn.\n' + _context(output)}
    # Uncertain submissions retain runtime.py's attempt key and can retry.
    if (marker or {}).get('delivery_event', {}).get('completed') or output.get('decision') == 'block':
        state['stop_step'] = final['step_index']
    return translate(output, event, payload)


def main():
    event = sys.argv[1] if len(sys.argv) == 2 else ''
    try:
        output = handle(json.load(sys.stdin), dict(os.environ), event)
    except Exception as exc:
        output = _problem(event, 'The plugin could not read the host event (%s)' % type(exc).__name__, {})
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()
