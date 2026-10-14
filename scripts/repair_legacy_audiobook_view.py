#!/usr/bin/env python3
"""Replace the recovery view's author-directory links with metadata directories.

No audio bytes are copied, no remote objects or database records are changed,
and no container is restarted. --apply saves a SQLite online backup and retains
every replaced link for rollback before changing the local recovery view.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

VIEW = Path('/gdrive-audiobooks')
LEGACY = Path('/mnt/legacy-gdrive-audiobooks')
AUDIO = {'.m4b', '.m4a', '.mp3', '.flac', '.ogg', '.opus', '.aac', '.wav', '.wma', '.aiff', '.alac'}


def command(*args: str, timeout: int = 30) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed (exit {result.returncode})')
    return result.stdout


def inspect(container: str) -> dict:
    return json.loads(command('docker', 'inspect', container))[0]


def host_path(mounts: list[dict], path: Path) -> Path:
    matches = [m for m in mounts if path == Path(m['Destination']) or Path(m['Destination']) in path.parents]
    if not matches:
        raise RuntimeError(f'No host-backed mount for {path}')
    mount = max(matches, key=lambda m: len(m['Destination']))
    return Path(mount['Source']) / path.relative_to(mount['Destination'])


def build_author(source: Path, candidate: Path, container_source: Path) -> dict[str, int]:
    """Make only directories and file symlinks; never open source file content."""
    inventory = {}
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(source, followlinks=False, onerror=walk_error):
        directory = Path(directory)
        rel = directory.relative_to(source)
        destination = candidate / rel
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in dirs:
            if (directory / name).is_symlink():
                raise RuntimeError('Unexpected directory symlink in the original Drive tree')
        for name in files:
            original = directory / name
            stat = original.stat()
            if not original.is_file():
                raise RuntimeError('Unexpected non-file in the original Drive tree')
            (destination / name).symlink_to(container_source / rel / name)
            inventory[(rel / name).as_posix()] = stat.st_size
    return inventory


def recorded_audio(database: sqlite3.Connection, authors: set[str]) -> list[tuple[str, int | None]]:
    candidates = []
    for (name,) in database.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        quoted = '"' + name.replace('"', '""') + '"'
        columns = {row[1] for row in database.execute(f'PRAGMA table_info({quoted})')}
        if {'id', 'path', 'libraryFiles', 'isMissing'} <= columns:
            candidates.append(quoted)
    if len(candidates) != 1:
        raise RuntimeError('Cannot identify the existing Audiobookshelf item table safely')
    expected = []
    for item_path, library_files in database.execute(f'SELECT path, libraryFiles FROM {candidates[0]}'):
        path = Path(item_path)
        if VIEW not in path.parents:
            continue
        relative = path.relative_to(VIEW)
        if not relative.parts or relative.parts[0] not in authors:
            continue
        for entry in json.loads(library_files or '[]'):
            metadata = entry.get('metadata', {})
            file_path = Path(metadata.get('path', ''))
            if file_path.suffix.lower() not in AUDIO:
                continue
            if VIEW not in file_path.parents or file_path.relative_to(VIEW).parts[0] not in authors:
                raise RuntimeError('An existing audio path does not match the intended view')
            expected.append((file_path.as_posix(), metadata.get('size')))
    if not expected:
        raise RuntimeError('No existing affected audio records found; refusing an uncertain repair')
    return expected


def verify_scanner(container: str, expected: set[str]) -> int:
    # The installed scanner uses this same bundled filesystem walker. This reads
    # directory entries only: it neither starts a library scan nor updates items.
    javascript = '''const r=require('./server/libs/recursiveReaddirAsync');
    r.list(VIEW_PATH, {mode:r.LIST,recursive:true,stats:false,
      ignoreFolders:true,extensions:true,deep:true,realPath:true,normalizePath:false})
    .then(files=>{if(files.error)process.exit(2);
      console.log(JSON.stringify(files.filter(f=>!f.error).map(f=>f.fullname)));})
    .catch(()=>process.exit(3));'''.replace('VIEW_PATH', json.dumps(str(VIEW) + '/'))
    actual = set(json.loads(command('docker', 'exec', container, 'node', '-e', javascript, timeout=120)))
    missing = expected - actual
    if missing:
        raise RuntimeError(f'Scanner still cannot enumerate {len(missing)} original audio paths')
    return len(expected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--container', default='audiobookshelf-test')
    args = parser.parse_args()
    data = inspect(args.container)
    mounts = data['Mounts']
    view = host_path(mounts, VIEW)
    legacy = host_path(mounts, LEGACY)
    filesystem = command('findmnt', '-n', '-T', str(view), '-o', 'FSTYPE').strip()
    if filesystem.startswith('fuse') or view == legacy or view in legacy.parents or legacy in view.parents:
        raise RuntimeError('Recovery view is not safely separate from the remote audio mount')
    plans = []
    for entry in sorted(view.iterdir()):
        if not entry.is_symlink():
            continue  # Retain the existing TorBox overrides and real directories.
        target = entry.readlink()
        if target != LEGACY / entry.name:
            raise RuntimeError(f'Unexpected directory link: {entry.name}')
        source = legacy / entry.name
        if not source.is_dir():
            raise RuntimeError(f'Original Drive author is unavailable: {entry.name}')
        plans.append((entry.name, target))
    if not plans:
        print('No author directory links need conversion. No changes made.')
        return
    print(f'Found {len(plans)} author directory links; original Drive directories are readable.', flush=True)
    if not args.apply:
        print('Read-only inspection complete. Use --apply for the backed-up metadata repair.')
        return
    os.umask(0o077)
    lock_path = view.parent / '.legacy-view-repair.lock'
    with lock_path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        rollback = Path(tempfile.mkdtemp(prefix=f'legacy-view-repair-{stamp}-', dir=view.parent))
        rollback.chmod(0o700)
        prepared = rollback / 'prepared'
        originals = rollback / 'original-author-links'
        prepared.mkdir(mode=0o700)
        originals.mkdir(mode=0o700)
        environment = dict(e.split('=', 1) for e in data['Config'].get('Env', []) if '=' in e)
        db_path = host_path(mounts, Path(environment.get('CONFIG_PATH', '/config')) / 'absdatabase.sqlite')
        with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as source_db:
            with sqlite3.connect(rollback / 'absdatabase-before.sqlite') as backup_db:
                source_db.backup(backup_db)
        with sqlite3.connect((rollback / 'absdatabase-before.sqlite').as_uri() + '?mode=ro', uri=True) as backup_db:
            expected = recorded_audio(backup_db, {name for name, _ in plans})
        inventory = {}
        for name, target in plans:
            for relative, size in build_author(legacy / name, prepared / name, target).items():
                inventory[str(VIEW / name / relative)] = size
        for path, size in expected:
            if path not in inventory or (size is not None and int(size) != inventory[path]):
                raise RuntimeError('An original recorded audio file is absent or has changed size; nothing activated')
        manifest = {'container_id': data['Id'], 'view': str(view), 'legacy_source': str(legacy),
                    'original_links': {name: str(target) for name, target in plans},
                    'verified_recorded_audio_paths': [p for p, _ in expected], 'phase': 'prepared'}
        manifest_path = rollback / 'manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        if inspect(args.container)['Id'] != data['Id']:
            raise RuntimeError('Container changed during preparation; nothing activated')
        for name, target in plans:
            if not (view / name).is_symlink() or (view / name).readlink() != target:
                raise RuntimeError('Recovery view changed during preparation; nothing activated')
        applied = []
        try:
            for name, _target in plans:
                os.rename(view / name, originals / name)
                try:
                    os.rename(prepared / name, view / name)
                except BaseException:
                    os.rename(originals / name, view / name)
                    raise
                applied.append(name)
            count = verify_scanner(args.container, {p for p, _ in expected})
        except BaseException:
            for name in reversed(applied):
                os.rename(view / name, prepared / name)
                os.rename(originals / name, view / name)
            manifest['phase'] = 'rolled-back'
            manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
            raise
        manifest['phase'] = 'activated'
        manifest_path.write_text(json.dumps(manifest, indent=2) + '\n')
        print(f'Converted {len(applied)} authors. Scanner enumeration includes all {count} recorded audio paths.')
        print(f'Rollback links, manifest and current database backup: {rollback}')
        print('No audio copied, database records changed, remote files removed, or containers restarted.')
        print('The filesystem repair is active. Run a NORMAL Family-Audiobooks library scan to clear missing flags.')


if __name__ == '__main__':
    def interrupted(_signal, _frame):
        raise InterruptedError('Interrupted; original links are retained for rollback')
    for stop_signal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(stop_signal, interrupted)
    try:
        main()
    except Exception as error:
        print(f'Repair stopped safely: {error}', file=sys.stderr)
        sys.exit(1)
