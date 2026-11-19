"""Create paused Antigravity Automations and bind their native events to ConsoleX."""
import json
from pathlib import Path
import re
import time
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEDULE_ID = re.compile(r'consolex-agy-[0-9a-f]{32}')
PROMPT_PREFIX = 'ConsoleX Antigravity Automation: '
COMMAND = re.compile(r'/consolex-schedule\s+(\S+)\s+--\s+(CRON_TZ=\S+ [^\n]+)')
NATIVE_CALL = ['agentapi', 'new-conversation', '--']
FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))


def _home(env):
    return Path(env.get('CONSOLEX_ANTIGRAVITY_HOME') or Path.home() / '.gemini').resolve()


def _binding_dir(core, env):
    return Path(core._state_dir(env)).parent / 'automations'


def _binding_path(core, env, schedule_id):
    return _binding_dir(core, env) / (schedule_id + '.json')


def _definition_path(env, schedule_id):
    return _home(env) / 'config' / 'sidecars' / schedule_id / 'sidecar.json'


def _settings(core, env):
    path = _home(env) / 'config' / 'config.json'
    config = core._read_json(str(path), None)
    if not isinstance(config, dict) or not isinstance(config.get('sidecars', {}), dict):
        raise ValueError('Antigravity App settings could not be read')
    return config.get('sidecars', {})


def parse_cron(expression):
    words = expression.split()
    if len(words) != 6 or not re.fullmatch(r'CRON_TZ=[A-Za-z0-9_+/-]{1,64}', words[0]):
        raise ValueError('choose a time zone and a recurring five-field schedule in ConsoleX')
    zone = words[0][8:]
    try:
        ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError('the schedule time zone is not available on this computer') from exc
    for field, (low, high) in zip(words[1:], FIELD_RANGES):
        for part in field.split(','):
            match = re.fullmatch(r'(\*|\d{1,2}(?:-\d{1,2})?)(?:/(\d{1,2}))?', part)
            if not match:
                raise ValueError('invalid cron field')
            base, step = match.groups()
            if base != '*':
                start, _, end = base.partition('-')
                if not (low <= int(start) <= int(end or start) <= high):
                    raise ValueError('cron value outside its range')
            if step is not None and not 1 <= int(step) <= high - low + 1:
                raise ValueError('invalid cron step')
    return zone, ' '.join(words[1:])


def _prompt(schedule_id):
    return (PROMPT_PREFIX + schedule_id + '\nWait for the ConsoleX hook to confirm the task and supply its method. '
            'If no task is confirmed, stop without doing work or submitting a delivery.')


def _read_definition(core, env, schedule_id, binding):
    path = _definition_path(env, schedule_id)
    if path.resolve() != path:
        raise ValueError('unsafe Antigravity automation path')
    document = core._read_json(str(path), None)
    if not isinstance(document, dict):
        raise ValueError('Antigravity automation definition could not be read')
    args = document.get('args')
    permissions = document.get('agent_permissions') or {}
    if (document.get('builtin') != 'schedule' or not isinstance(args, list) or len(args) != 5
            or args[1:] != NATIVE_CALL + [_prompt(schedule_id)]
            or permissions.get('workspace_uris') != [Path(binding['workspace']).as_uri()]):
        raise ValueError('the Antigravity automation no longer matches its ConsoleX task')
    zone, cron = parse_cron(args[0])
    return zone, cron


def _report(core, env, binding, item, http):
    api = binding['api_base']
    token, _ = core.connection_for(env, api)
    if not token or not core._codex_can_use_api(env, api):
        return {}
    status, body = http('PUT', api + '/api/operator/external/schedules', token,
                        {'runtime': 'antigravity', 'schedules': [item]}, timeout=core.QUICK_TIMEOUT)
    return body if status == 200 and body.get('succ') else {}


def prepare(core, env, data, prompt, http):
    match = COMMAND.fullmatch(prompt.strip())
    if not match or not core._TASK_LINK.fullmatch(match[1]):
        return 'Copy the schedule command for this task from ConsoleX. It includes the task UUID, time zone and recurrence.'
    task_uuid = match[1].lower()
    zone, cron = parse_cron(match[2])
    api = core.default_api_base(env)
    token, _ = core.connection_for(env, api)
    if not token:
        return core._not_connected_text(api)
    status, body = core._list_tasks(env, api, token, task_uuid, http)
    task = next((task for task in body.get('tasks', []) if task.get('uuid') == task_uuid), None)
    if status != 200 or not task:
        return 'ConsoleX could not find this task on this computer’s connected server. No automation was created.'
    config = {'task_uuid': task_uuid, 'api_base': api}
    gate, note = core._schedule_gate(env, config, http)
    if gate == 'block' or (gate == 'warn' and note.startswith('ConsoleX could not say')):
        return note
    name = ('ConsoleX · ' + ' '.join(task['name'].split()))[:140]
    schedule_id = 'consolex-agy-' + uuid5(NAMESPACE_URL, api + '/tasks/' + task_uuid).hex
    path = _definition_path(env, schedule_id)
    if path.resolve() != path:
        raise ValueError('unsafe Antigravity automation path')
    existing = _settings(core, env).get(schedule_id) or {}
    if existing.get('enabled') is True:
        return 'Pause “%s” in Antigravity → Automations before changing its recurrence in ConsoleX.' % name
    binding_path = _binding_path(core, env, schedule_id)
    workspace = Path(data['cwd']).resolve()
    if not workspace.is_dir():
        raise ValueError('open the task project in Antigravity before setting its schedule')
    binding = {**config, 'schedule_id': schedule_id, 'workspace': str(workspace)}
    previous = core._read_json(str(binding_path), None)
    if path.exists():
        if previous != binding:
            return 'Use the original Antigravity project to change this automation, or remove it there first.'
        _read_definition(core, env, schedule_id, binding)
    document = {
        'display_name': name, 'description': 'Runs a saved ConsoleX task and checks its delivery.',
        'builtin': 'schedule', 'args': [f'CRON_TZ={zone} {cron}', *NATIVE_CALL, _prompt(schedule_id)],
        'agent_permissions': {'workspace_uris': [workspace.as_uri()], 'access_grants': []},
    }
    core._write_private_unique(str(binding_path), binding)
    core._write_private_unique(str(path), document)
    item = {'schedule_id': schedule_id, 'task_uuid': task_uuid, 'timezone': zone, 'cron': cron, 'enabled': False}
    if not _report(core, env, binding, item, http):
        return '“%s” is saved in Antigravity as paused. ConsoleX could not confirm it. Run the setup command again before enabling it.' % name
    return ('“%s” is ready in Antigravity → Automations, under Paused. Enable it there and review the project permissions. '
            'The schedule is %s (%s). Keep the App open and the computer awake.%s') % (name, cron, zone, ' ' + note if note else '')


def _event_matches(env, schedule_id, session, prompt):
    directory = _home(env) / 'antigravity' / 'sidecar_data' / schedule_id / 'events'
    if directory.resolve() != directory:
        return False
    try:
        recent = sorted((path for path in directory.glob('*.json') if not path.is_symlink()), reverse=True)[:32]
        for path in recent:
            if path.stat().st_size > 32_768:
                continue
            record = json.loads(path.read_text(encoding='utf-8'))
            age = time.time() - int(record['timestampMs']) / 1000
            expected = {'conversationId': session, 'prompt': prompt}
            if -5 <= age <= 120 and not record.get('error') and record['payload'].get('newConversation') == expected:
                return True
    except (OSError, KeyError, ValueError, TypeError, AttributeError):
        return False
    return False


def start(core, env, data, prompt, first_turn, http):
    schedule_id = prompt.removeprefix(PROMPT_PREFIX).split('\n', 1)[0]
    if not first_turn or not SCHEDULE_ID.fullmatch(schedule_id) or prompt != _prompt(schedule_id):
        return None, 'No native Antigravity Automation was confirmed for this conversation.'
    binding = core._read_json(str(_binding_path(core, env, schedule_id)), None)
    if not isinstance(binding, dict) or binding.get('schedule_id') != schedule_id:
        return None, 'The ConsoleX automation binding is missing.'
    try:
        if Path(data['cwd']).resolve() != Path(binding['workspace']):
            raise ValueError('the activation is outside the registered project')
        zone, cron = _read_definition(core, env, schedule_id, binding)
        if (_settings(core, env).get(schedule_id) or {}).get('enabled') is not True:
            raise ValueError('the native automation is paused')
    except (ValueError, OSError) as exc:
        return None, str(exc)
    # The App starts the conversation and records agentapi's event in separate operations.
    deadline = time.monotonic() + 3
    while not _event_matches(env, schedule_id, data['session_id'], prompt):
        if time.monotonic() >= deadline:
            return None, 'No native Antigravity event matched this conversation.'
        time.sleep(.1)
    api = binding['api_base']
    if (not core._codex_can_use_api(env, api) or not core.connection_for(env, api)[0]
            or (env.get('CONSOLEX_API_BASE') and core.canonical_api_base(env['CONSOLEX_API_BASE']) != api)):
        return None, 'The ConsoleX server saved with this automation is not connected.'
    item = {'schedule_id': schedule_id, 'task_uuid': binding['task_uuid'], 'timezone': zone, 'cron': cron, 'enabled': True}
    result = _report(core, env, binding, item, http)
    if result.get('stored') != 1:
        return None, 'ConsoleX did not confirm the automation.'
    if result.get('conflicts'):
        return None, 'This task already runs on another schedule. Pause the other schedule before running here.'
    output, task = core.start_task({**data, 'hook_event_name': 'ConsoleXStartTask', '_consolex_start': True,
                                   'unattended': True, 'schedule_id': schedule_id},
                                  {**env, 'CONSOLEX_API_BASE': api}, binding['task_uuid'], http=http)
    if not task:
        return None, (output or {}).get('hookSpecificOutput', {}).get('additionalContext') or 'Task activation failed.'
    return output, ''


def reconcile(core, env, http):
    """Refresh owned schedules without replacing any other runtime's schedule list."""
    directory = _binding_dir(core, env)
    if not directory.is_dir():
        return
    settings = _settings(core, env)
    state_path = directory.parent / 'automation-reports.json'
    previous = core._read_json(str(state_path), {})
    now = time.time()
    pending = {}
    for path in directory.glob('*.json'):
        schedule_id = path.stem
        if not SCHEDULE_ID.fullmatch(schedule_id):
            continue
        binding = core._read_json(str(path), {})
        if not isinstance(binding, dict) or binding.get('schedule_id') != schedule_id:
            continue
        definition = _definition_path(env, schedule_id)
        if definition.exists():
            try:
                zone, cron = _read_definition(core, env, schedule_id, binding)
            except (OSError, ValueError):
                continue
            item = {'schedule_id': schedule_id, 'task_uuid': binding['task_uuid'], 'timezone': zone, 'cron': cron,
                    'enabled': (settings.get(schedule_id) or {}).get('enabled') is True}
        else:
            item = {'schedule_id': schedule_id, 'deleted': True}
        last = previous.get(schedule_id) or {}
        if last.get('item') != item or now - last.get('at', 0) >= 3600:
            pending.setdefault(binding['api_base'], []).append(item)
    for api, items in pending.items():
        token, _ = core.connection_for(env, api)
        if not token or not core._codex_can_use_api(env, api):
            continue
        batch = items[:100]
        status, body = http('PUT', api + '/api/operator/external/schedules', token,
                            {'runtime': 'antigravity', 'schedules': batch}, timeout=core.QUICK_TIMEOUT)
        if status == 200 and body.get('succ'):
            previous.update({item['schedule_id']: {'item': item, 'at': now} for item in batch})
    if pending:
        core._write_private_unique(str(state_path), previous)
