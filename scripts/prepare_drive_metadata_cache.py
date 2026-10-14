#!/usr/bin/env python3
"""Save live rollback material and stage one Drive metadata-cache change.

Preparation only: no installed configuration, remote objects, audio files,
database records, mounts or running services are changed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone

UNIT = 'rclone-gdrive-audiobooks.service'
MOUNT = Path('/mnt/gdrive-audiobooks')
CONTAINER = 'audiobookshelf-test'


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(f'{args[0]} read failed (exit {result.returncode}); nothing activated')
    return result.stdout


def starts(text: str) -> list[str]:
    # Only accept ordinary literal ExecStart commands; refuse shell wrappers,
    # systemd specifiers and variable expansion rather than reconstructing them.
    text = re.sub(r'\\\r?\n\s*', ' ', text)
    section = ''
    commands: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('['):
            section = line
        elif section == '[Service]' and line.startswith('ExecStart='):
            value = line[len('ExecStart='):].strip()
            if value:
                commands.append(value)
            else:
                commands.clear()
    return commands


def replacement(args: list[str]) -> list[str]:
    changed = args.copy()
    hits = 0
    for i, arg in enumerate(args):
        if arg == '--dir-cache-time':
            if i + 1 >= len(args) or args[i + 1] != '5m':
                raise RuntimeError('Directory cache no longer matches the inspected 5m setting')
            changed[i + 1] = '1h'
            hits += 1
        elif arg.startswith('--dir-cache-time='):
            if arg != '--dir-cache-time=5m':
                raise RuntimeError('Directory cache no longer matches the inspected 5m setting')
            changed[i] = '--dir-cache-time=1h'
            hits += 1
    if hits != 1:
        raise RuntimeError('Expected exactly one explicit directory-cache argument')
    return changed


def quote(arg: str) -> str:
    if any(ord(c) < 32 for c in arg) or '%' in arg or '$' in arg:
        raise RuntimeError('Nonliteral systemd argument; refusing an uncertain reconstruction')
    return '"' + arg.replace('\\', '\\\\').replace('"', '\\"') + '"'


def host_path(mounts: list[dict], path: Path) -> Path:
    matches = [m for m in mounts if path == Path(m['Destination']) or Path(m['Destination']) in path.parents]
    if not matches:
        raise RuntimeError('Audiobookshelf database is not on an identified host-backed mount')
    m = max(matches, key=lambda item: len(item['Destination']))
    return Path(m['Source']) / path.relative_to(m['Destination'])


def main() -> None:
    if os.geteuid() == 0:
        raise RuntimeError('Run preparation as sawdoctor, without sudo')
    props = dict(line.split('=', 1) for line in run(
        'systemctl', 'show', UNIT, '-p', 'MainPID', '-p', 'FragmentPath',
        '-p', 'DropInPaths', '-p', 'ActiveState').splitlines() if '=' in line)
    if props.get('ActiveState') != 'active':
        raise RuntimeError('The inspected Drive service is not active')
    pid = int(props.get('MainPID', '0'))
    proc = Path('/proc') / str(pid)
    args = (proc / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
    if len(args) < 4 or Path(args[0]).name != 'rclone' or args[1] != 'mount' or str(MOUNT) not in args:
        raise RuntimeError('Service MainPID is not the expected direct rclone mount')
    if '--daemon' in args or any(a.startswith('--daemon=') for a in args):
        raise RuntimeError('Daemonized mount requires a different activation plan')
    if f'/{UNIT}' not in (proc / 'cgroup').read_text():
        raise RuntimeError('Drive process is outside the inspected service')
    files = [Path(props['FragmentPath']), *map(Path, shlex.split(props.get('DropInPaths', '')))]
    originals = [(file, file.read_bytes()) for file in files]
    commands = starts('\n'.join(content.decode() for _, content in originals))
    if len(commands) != 1 or '$' in commands[0] or '%' in commands[0] or shlex.split(commands[0]) != args:
        raise RuntimeError('Effective ExecStart differs from the running literal command; nothing activated')
    proposed = replacement(args)
    override = '[Service]\nExecStart=\nExecStart=' + ' '.join(map(quote, proposed)) + '\n'

    ids = run('docker', 'ps', '-q').split()
    containers = json.loads(run('docker', 'inspect', *ids)) if ids else []
    consumers = []
    selected = None
    for container in containers:
        if container['Name'] == '/' + CONTAINER:
            selected = container
        for mount in container['Mounts']:
            source = Path(mount['Source'])
            if source == MOUNT or source in MOUNT.parents:
                consumers.append({'name': container['Name'].lstrip('/'), 'source': str(source),
                                  'destination': mount['Destination'], 'propagation': mount.get('Propagation', '')})
    if selected is None or consumers != [{'name': CONTAINER, 'source': str(MOUNT),
                                         'destination': '/mnt/legacy-gdrive-audiobooks', 'propagation': 'rprivate'}]:
        raise RuntimeError('Drive mount consumers changed; refusing the previous activation scope')
    env = dict(entry.split('=', 1) for entry in selected['Config'].get('Env', []) if '=' in entry)
    database = host_path(selected['Mounts'], Path(env.get('CONFIG_PATH', '/config')) / 'absdatabase.sqlite')
    if not database.is_file():
        raise RuntimeError('Existing Audiobookshelf database could not be identified')
    recovery = Path.home() / 'torbox-recovery-20261011'
    if not recovery.is_dir():
        raise RuntimeError('Existing recovery directory not found')
    os.umask(0o077)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    stage = Path(tempfile.mkdtemp(prefix=f'drive-metadata-cache-{stamp}-', dir=recovery))
    stage.chmod(0o700)
    original_dir = stage / 'original-units'
    original_dir.mkdir(mode=0o700)
    hashes = {}
    for i, (file, content) in enumerate(originals):
        (original_dir / f'{i}-{file.name}').write_bytes(content)
        hashes[str(file)] = hashlib.sha256(content).hexdigest()
    (stage / 'effective-unit-before.txt').write_text(run('systemctl', 'cat', UNIT))
    (stage / 'container-before.json').write_text(json.dumps(selected, indent=2) + '\n')
    (stage / 'proposed-override.conf').write_text(override)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as current:
        with sqlite3.connect(stage / 'absdatabase-before.sqlite') as backup:
            current.backup(backup)
    plan = {'phase': 'prepared-only', 'unit': UNIT, 'main_pid': pid,
            'original_unit_hashes': hashes, 'container_id': selected['Id'],
            'image_id': selected['Image'], 'consumers': consumers,
            'change': {'dir_cache_time': {'before': '5m', 'after': '1h'}},
            'poll_interval': 'unchanged', 'audio_cache': 'unchanged',
            'credentials': 'unchanged', 'database': str(database),
            'approval_required': 'Drive mount restart and audiobookshelf-test stop/start to renew its private bind; no other service restarts',
            'rollback': 'restore prior service configuration and mount, retaining current ABS database; database backup is precautionary, not an automatic restore'}
    (stage / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    print('Preparation complete. No live configuration, database records, remote objects or services changed.')
    print('Proposed sole setting change: directory metadata cache 5m -> 1h; polling and audio cache unchanged.')
    print('Private rollback copies, current database backup and proposed override:', stage)
    print('Activation scope: rclone-gdrive-audiobooks.service and audiobookshelf-test only.')
    print('Activation would briefly interrupt Audiobookshelf playback, including TorBox playback through that server.')
    print('Activation has NOT been performed and requires explicit approval.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Do not echo command arguments or unit content that could contain secrets.
        if isinstance(error, RuntimeError):
            print('Preparation stopped:', error)
        else:
            print('Preparation stopped:', type(error).__name__, '(private details withheld). Nothing activated.')
        raise SystemExit(1)
