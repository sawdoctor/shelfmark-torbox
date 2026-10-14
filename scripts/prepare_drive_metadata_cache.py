#!/usr/bin/env python3
"""Save live rollback material and stage one Drive metadata-cache change.

Default mode only prepares rollback material. Explicit --activate installs the
staged metadata setting and renews only the two authorized audiobook services.
Neither mode writes database records, remote objects or audiobook payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
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


OVERRIDE = Path('/etc/systemd/system') / (UNIT + '.d') / 'zz-shelfmark-metadata-cache.conf'
LEGACY = '/mnt/legacy-gdrive-audiobooks'
RECOVERY_ROOT = Path('/home/sawdoctor/torbox-recovery-20261011')


def command(*args: str, timeout: int = 45) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Arguments, stderr and unit contents can contain private information.
        raise RuntimeError(f'{args[0]} operation failed (exit {result.returncode}); private details withheld')
    return result.stdout


def service() -> tuple[list[Path], list[str]]:
    props = dict(line.split('=', 1) for line in command(
        'systemctl', 'show', UNIT, '-p', 'MainPID', '-p', 'FragmentPath',
        '-p', 'DropInPaths', '-p', 'ActiveState').splitlines() if '=' in line)
    if props.get('ActiveState') != 'active' or int(props.get('MainPID', '0')) <= 0:
        raise RuntimeError('Drive mount service is not active')
    proc = Path('/proc') / props['MainPID']
    args = (proc / 'cmdline').read_bytes().decode().rstrip('\0').split('\0')
    if f'/{UNIT}' not in (proc / 'cgroup').read_text():
        raise RuntimeError('Drive process has an unexpected controlling service')
    files = [Path(props['FragmentPath']), *map(Path, shlex.split(props.get('DropInPaths', '')))]
    return files, args


def inventory() -> tuple[dict, list[dict], set[str]]:
    ids = command('docker', 'ps', '-q').split()
    containers = json.loads(command('docker', 'inspect', *ids)) if ids else []
    selected = next((c for c in containers if c['Name'] == '/' + CONTAINER), None)
    if selected is None:
        raise RuntimeError('The existing audiobook container is not running')
    consumers = []
    for c in containers:
        for m in c['Mounts']:
            source = Path(m['Source'])
            if source == MOUNT or source in MOUNT.parents:
                consumers.append({'name': c['Name'].lstrip('/'), 'source': str(source),
                                  'destination': m['Destination'], 'propagation': m.get('Propagation', '')})
    return selected, consumers, {c['Id'] for c in containers if c['Name'] != '/' + CONTAINER}


def identity(container: dict) -> dict:
    return {k: container[k] for k in ('Id', 'Image', 'Config', 'HostConfig', 'Mounts')}


def database_snapshot(database: Path, destination: Path) -> dict:
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as current:
        with sqlite3.connect(destination) as backup:
            current.backup(backup)
    with sqlite3.connect(destination.as_uri() + '?mode=ro', uri=True) as db:
        result = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            table = '"' + name.replace('"', '""') + '"'
            columns = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
            if {'id', 'path', 'libraryFiles', 'isMissing'} <= columns:
                result['books'] = sorted(db.execute('SELECT id, path FROM ' + table).fetchall())
            if {'id', 'currentTime', 'isFinished', 'userId', 'mediaItemId'} <= columns:
                result['progress'] = sorted(db.execute(
                    'SELECT id, userId, mediaItemId, currentTime, isFinished FROM ' + table).fetchall())
        if 'books' not in result or 'progress' not in result:
            raise RuntimeError('Current database schema could not be safely identified')
        return result


def announce(*values: object) -> None:
    # A disconnected phone must not interrupt rollback merely by closing stdout.
    try:
        print(*values, flush=True)
    except (BrokenPipeError, OSError):
        pass


def activate(stage_name: str) -> None:
    if os.geteuid() != 0:
        raise RuntimeError('Activation requires sudo on this server')
    os.umask(0o077)
    stage = Path(stage_name)
    recovery = RECOVERY_ROOT
    if stage.is_symlink() or stage.parent != recovery or not stage.name.startswith('drive-metadata-cache-'):
        raise RuntimeError('Expected an existing staged recovery directory')
    if stage.resolve() != stage or not stage.is_dir() or stage.stat().st_mode & 0o077:
        raise RuntimeError('Recovery directory is not private or has changed')
    plan = json.loads((stage / 'plan.json').read_text())
    if (plan.get('phase') != 'prepared-only' or plan.get('unit') != UNIT or
            plan.get('change') != {'dir_cache_time': {'before': '5m', 'after': '1h'}}):
        raise RuntimeError('Stage is not the reviewed, unactivated single-setting plan')
    files, args = service()
    if {str(f) for f in files} != set(plan['original_unit_hashes']):
        raise RuntimeError('Service unit files changed since preparation')
    for f in files:
        if hashlib.sha256(f.read_bytes()).hexdigest() != plan['original_unit_hashes'][str(f)]:
            raise RuntimeError('Service configuration changed since preparation')
    original = starts('\n'.join(f.read_text() for f in files))
    if (len(original) != 1 or shlex.split(original[0]) != args or len(args) < 4 or
            Path(args[0]).name != 'rclone' or args[1] != 'mount' or str(MOUNT) not in args):
        raise RuntimeError('Active mount command changed since preparation')
    proposed = replacement(args)
    override = '[Service]\nExecStart=\nExecStart=' + ' '.join(map(quote, proposed)) + '\n'
    if (stage / 'proposed-override.conf').read_text() != override:
        raise RuntimeError('Staged override differs from the reviewed single-setting change')
    selected, consumers, other_ids = inventory()
    previous = json.loads((stage / 'container-before.json').read_text())
    expected_consumers = [{'name': CONTAINER, 'source': str(MOUNT),
                           'destination': LEGACY, 'propagation': 'rprivate'}]
    if consumers != expected_consumers or consumers != plan['consumers']:
        raise RuntimeError('Another container consumes this mount; refusing expanded disruption')
    if (identity(selected) != identity(previous) or selected['Id'] != plan['container_id'] or
            selected['Image'] != plan['image_id']):
        raise RuntimeError('Audiobook container image, mounts or configuration changed since preparation')
    env = dict(entry.split('=', 1) for entry in selected['Config'].get('Env', []) if '=' in entry)
    database = host_path(selected['Mounts'], Path(env.get('CONFIG_PATH', '/config')) / 'absdatabase.sqlite')
    if str(database) != plan['database'] or not database.is_file():
        raise RuntimeError('Current database location changed since preparation')
    if OVERRIDE.exists() or OVERRIDE.is_symlink() or (stage / 'activation-state.json').exists():
        raise RuntimeError('An activation artifact already exists; refusing to overwrite it')
    if OVERRIDE.parent.is_symlink():
        raise RuntimeError('Unexpected service override directory')
    installed = False
    stop_requested = False
    mount_restart_requested = False
    owner = stage.stat()

    def save(name: str, value: object) -> None:
        p = stage / name
        p.write_text(json.dumps(value, indent=2) + '\n')
        p.chmod(0o600)
        os.chown(p, owner.st_uid, owner.st_gid)

    def note(phase: str) -> None:
        save('activation-state.json', {'phase': phase, 'unit': UNIT, 'container': CONTAINER})
        announce(phase.replace('-', ' '))

    def interrupted(signum: int, frame: object) -> None:
        raise InterruptedError('Activation interrupted')

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        # Backup current progress, rather than depending only on the older staged copy.
        database_snapshot(database, stage / 'absdatabase-at-activation.sqlite')
        OVERRIDE.parent.mkdir(mode=0o755, exist_ok=True)
        with OVERRIDE.open('x') as f:
            installed = True
            f.write(override)
        command('systemctl', 'daemon-reload')
        effective = starts(command('systemctl', 'cat', UNIT))
        if len(effective) != 1 or shlex.split(effective[0]) != proposed:
            raise RuntimeError('Installed override is not the effective reviewed command')
        note('stopping-audiobook-container')
        stop_requested = True
        command('docker', 'stop', '--timeout', '30', CONTAINER, timeout=45)
        stopped = json.loads(command('docker', 'inspect', CONTAINER))[0]
        if stopped['State']['Running']:
            raise RuntimeError('Audiobook container did not stop')
        baseline = database_snapshot(database, stage / 'absdatabase-after-stop.sqlite')
        save('records-after-stop.json', baseline)
        note('restarting-only-drive-audiobook-mount')
        mount_restart_requested = True
        command('systemctl', 'restart', UNIT, timeout=55)
        _, running = service()
        if running != proposed:
            raise RuntimeError('Restarted mount does not have the reviewed arguments')
        mounted = command('findmnt', '-rn', '-T', str(MOUNT), '-o', 'TARGET,FSTYPE').split()
        if mounted != [str(MOUNT), 'fuse.rclone']:
            raise RuntimeError('Drive FUSE mount is not mounted at its original path')
        note('starting-same-audiobook-container')
        command('docker', 'start', CONTAINER)
        current, consumers, running_other_ids = inventory()
        if identity(current) != identity(selected) or consumers != expected_consumers:
            raise RuntimeError('Container identity or mount bindings changed unexpectedly')
        if not other_ids <= running_other_ids:
            raise RuntimeError('A previously running unrelated container is no longer running')
        # Device identity proves the container bound the current FUSE superblock.
        host_dev = int(command('python3', '-c',
            'import os; print(os.stat("/mnt/gdrive-audiobooks").st_dev)', timeout=15).strip())
        probe = r"""
const fs = require('fs/promises');
const timer = setTimeout(() => process.exit(124), 45000);
(async () => {
  const mount = await fs.stat('/mnt/legacy-gdrive-audiobooks');
  const started = Date.now();
  const book = await fs.stat('/gdrive-audiobooks/Charles Dickens/A Tale of Two Cities/TaleOfTwoCitiesPart1_librivox.m4b');
  if (!book.isFile() || book.size <= 0) throw new Error('Book metadata unavailable');
  console.log(JSON.stringify({device: mount.dev, size: book.size, lookupSeconds: (Date.now()-started)/1000}));
  clearTimeout(timer);
})().catch(() => process.exit(1));
"""
        checked = json.loads(command('docker', 'exec', CONTAINER, 'node', '-e', probe, timeout=50))
        if checked['device'] != host_dev:
            raise RuntimeError('Audiobook container is still attached to a different Drive mount')
        after = database_snapshot(database, stage / 'absdatabase-after-start.sqlite')
        if not set(map(tuple, baseline['books'])) <= set(map(tuple, after['books'])):
            raise RuntimeError('Existing book IDs or paths changed during startup; database retained for investigation')
        if not {row[0] for row in baseline['progress']} <= {row[0] for row in after['progress']}:
            raise RuntimeError('Existing progress records missing after startup; database retained for investigation')
        save('activation-result.json', {'dir_cache_time': '1h', 'container_identity_preserved': True,
             'mount_device_verified': True, 'book_records_retained': len(baseline['books']),
             'progress_records_retained': len(baseline['progress']),
             'progress_values_unchanged': baseline['progress'] == after['progress'],
             'metadata_probe': checked, 'playback_improvement': 'not yet measured'})
        note('activation-complete')
        announce('Directory metadata cache: 1h. Polling, audio cache, credentials and audiobook paths unchanged.')
        announce('Current Drive mount bound inside the same Audiobookshelf container; existing book/progress records retained.')
        announce('One metadata lookup after restart: %.2fs. This is not a playback measurement.' % checked['lookupSeconds'])
        if baseline['progress'] != after['progress']:
            announce('Progress values changed during startup; current values retained, not overwritten.')
        announce('Rollback material:', stage)
        announce('No audio copied, remote objects changed or unrelated services restarted.')
    except BaseException:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        try:
            if installed:
                note('restoring-previous-audiobook-service-setting')
                OVERRIDE.rename(stage / 'failed-installed-override.conf')
                command('systemctl', 'daemon-reload')
            if mount_restart_requested:
                command('docker', 'stop', '--timeout', '30', CONTAINER, timeout=45)
                command('systemctl', 'restart', UNIT, timeout=55)
                _, restored = service()
                if restored != args:
                    raise RuntimeError('Original mount arguments could not be verified')
            if stop_requested:
                command('docker', 'start', CONTAINER)
            if stop_requested:
                restored_container, restored_consumers, _ = inventory()
                if identity(restored_container) != identity(selected) or restored_consumers != expected_consumers:
                    raise RuntimeError('Restored container could not be verified')
            note('activation-failed-previous-setting-restored')
            announce('Current database retained. No database backup was restored. Rollback material:', stage)
        except BaseException:
            # Make a final bounded attempt to make this existing container available.
            try:
                command('docker', 'start', CONTAINER)
            except BaseException:
                pass
            announce('Automatic rollback could not be fully verified. Recovery material:', stage)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activate', metavar='STAGED_DIRECTORY')
    options = parser.parse_args()
    try:
        if options.activate:
            activate(options.activate)
        else:
            main()
    except BaseException as error:
        if isinstance(error, RuntimeError):
            print('Stopped:', error)
        else:
            print('Stopped:', type(error).__name__, '(private details withheld).')
        raise SystemExit(1)
