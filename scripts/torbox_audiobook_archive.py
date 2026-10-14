#!/usr/bin/env python3
"""Protect active TorBox audiobook sources and keep a verified backup vault.

Default invocation is read-only. --apply enables AirLock and backup writes.
Audio travels directly between rclone remotes, never through a local payload
file. This job never changes playback symlinks or Audiobookshelf's database.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shelfmark.torbox_airlock import (
    AUDIO_SUFFIXES,
    EDIT_ROUTES,
    AirLockAPI,
    AirLockError,
    file_paths,
    ready,
)


@dataclass
class Settings:
    library: Path
    mount: Path
    vault_mount: Path
    vault_remote: str
    source_remote: str
    state_dir: Path
    shelfmark_config: Path
    api_key_file: str = ""
    rclone_config: str = ""
    max_backups: int = 2

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            library=Path(
                os.environ.get(
                    "TORBOX_AUDIOBOOK_LIBRARY", "/mnt/usenet_scratch/audiobookshelf-test/audiobooks"
                )
            ),
            mount=Path(os.environ.get("TORBOX_MOUNT_PATH", "/mnt/torbox-audiobooks")),
            vault_mount=Path(os.environ.get("TORBOX_VAULT_MOUNT", "/mnt/gdrive-torbox-archive")),
            vault_remote=os.environ.get("TORBOX_VAULT_REMOTE", "gdrive:Shelfmark-TorBox-Archive"),
            source_remote=os.environ.get("TORBOX_SOURCE_REMOTE", ""),
            state_dir=Path(
                os.environ.get(
                    "TORBOX_ARCHIVE_STATE_DIR", str(Path.home() / ".local/state/torbox-audiobooks")
                )
            ),
            shelfmark_config=Path(
                os.environ.get(
                    "SHELFMARK_CONFIG_DIR", "/home/sawdoctor/docker/shelfmark-torbox/config"
                )
            ),
            api_key_file=os.environ.get("TORBOX_API_KEY_FILE", ""),
            rclone_config=os.environ.get("RCLONE_CONFIG", ""),
            max_backups=int(os.environ.get("TORBOX_ARCHIVE_MAX_BACKUPS", "2")),
        )

    def token(self) -> str:
        token = os.environ.get("TORBOX_API_KEY", "").strip()
        if not token and self.api_key_file:
            token = Path(self.api_key_file).read_text().strip()
        if not token:
            path = self.shelfmark_config / "plugins/prowlarr_clients.json"
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("Shelfmark download-client configuration is not a JSON object")
            token = str(data.get("TORBOX_API_KEY") or "").strip()
        if not token:
            raise AirLockError(
                "TorBox API key is missing from the existing Shelfmark configuration"
            )
        return token


def remote_path(remote: str, relative: str) -> str:
    name, separator, prefix = remote.partition(":")
    if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("A named rclone remote is required; local payload paths are forbidden")
    if ".." in Path(prefix).parts or ".." in Path(relative).parts:
        raise ValueError("Unsafe remote path")
    return name + ":" + "/".join(part for part in (prefix.strip("/"), relative) if part)


class RcloneError(RuntimeError):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


class Rclone:
    def __init__(self, settings: Settings) -> None:
        self.flags = ["--retries", "1", "--low-level-retries", "2", "--timeout", "60s"]
        if settings.rclone_config:
            self.flags += ["--config", settings.rclone_config]

    def command(self, *args: str) -> list[str]:
        return ["rclone", *args, *self.flags]

    def run(self, *args: str) -> str:
        result = subprocess.run(
            self.command(*args), capture_output=True, text=True, timeout=3600, check=False
        )
        if result.returncode:
            # rclone output may include configuration details; keep it out of logs.
            raise RcloneError(
                f"rclone {args[0]} failed (exit {result.returncode})", result.returncode
            )
        return result.stdout

    def stat(self, remote: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.run("lsjson", remote, "--stat", "--hash"))
        except RcloneError as error:
            if error.code in {3, 4}:  # rclone: directory/file not found
                return None
            raise
        if not isinstance(data, dict) or data.get("IsDir") is not False:
            raise RcloneError("Expected a single remote audio file")
        return data

    def digest(self, remote: str) -> tuple[str, int]:
        """Bounded-memory SHA256 verification with a bounded subprocess lifetime."""
        digest = hashlib.sha256()
        length = 0
        deadline = time.monotonic() + 3600
        with subprocess.Popen(
            self.command("cat", remote), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as process:
            if process.stdout is None:
                raise RcloneError("rclone did not expose a verification stream")
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while True:
                        if time.monotonic() > deadline:
                            raise RcloneError("Backup verification timed out")
                        if not selector.select(timeout=1):
                            continue
                        chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                        if not chunk:
                            break
                        length += len(chunk)
                        digest.update(chunk)
                if process.wait(timeout=30):
                    raise RcloneError("Backup verification stream failed")
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()
        return digest.hexdigest(), length


def read_state(settings: Settings) -> dict[str, Any]:
    path = settings.state_dir / "archive-state.json"
    if not path.exists():
        return {"version": 1, "origins": {}, "backups": {}}
    data = json.loads(path.read_text())
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not all(isinstance(data.get(key), dict) for key in ("origins", "backups"))
    ):
        raise ValueError("Invalid archive state; preserve it and review before proceeding")
    return data


def write_state(settings: Settings, state: dict[str, Any]) -> None:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=settings.state_dir, delete=False) as stream:
        json.dump(state, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(settings.state_dir / "archive-state.json")


def lexical_absolute(path: Path) -> Path:
    """Normalize '..' without resolving links or traversing a remote FUSE mount."""
    return Path(os.path.abspath(path))  # noqa: PTH100 -- resolve() would access remote storage


def scan_links(settings: Settings) -> tuple[list[dict[str, str]], list[str]]:
    """Read link text, not audio content; do not follow symlink directories."""
    if not settings.library.is_dir():
        raise ValueError("Audiobook library is unavailable; refusing to create a substitute")
    links = []
    vault_links = []
    mount = lexical_absolute(settings.mount)
    vault = lexical_absolute(settings.vault_mount)
    for root, _directories, files in os.walk(settings.library, followlinks=False):
        for name in sorted(files):
            link = Path(root) / name
            if link.suffix.lower() not in AUDIO_SUFFIXES or not link.is_symlink():
                continue
            target = lexical_absolute(link.parent / link.readlink())
            relative = link.relative_to(settings.library).as_posix()
            if target.is_relative_to(mount):
                links.append(
                    {"library_path": relative, "torbox_path": target.relative_to(mount).as_posix()}
                )
            elif target.is_relative_to(vault):
                vault_links.append(relative)
    return sorted(links, key=lambda entry: entry["library_path"]), sorted(vault_links)


def source_remote(settings: Settings) -> str:
    if settings.source_remote:
        remote_path(settings.source_remote, "")
        return settings.source_remote
    result = subprocess.run(
        ["findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", str(settings.mount)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    source = result.stdout.strip()
    if result.returncode or not source:
        raise ValueError(
            "TorBox mount source is unknown; set TORBOX_SOURCE_REMOTE from its existing mount unit"
        )
    remote_path(source, "")
    return source


def vault_identity(data: dict[str, Any]) -> dict[str, Any]:
    """Detect replacement or modification of a previously verified vault file."""
    return {key: data.get(key) for key in ("ID", "Size", "ModTime", "Hashes")}


def backup(
    link: dict[str, str],
    kind: str,
    item: dict[str, Any],
    info: dict[str, Any],
    settings: Settings,
    state: dict[str, Any],
    rclone: Rclone,
    source: str,
) -> str:
    relative = link["library_path"]
    src = remote_path(source, link["torbox_path"])
    dst = remote_path(settings.vault_remote, relative)
    if src == dst:
        raise ValueError("Backup source and destination must differ")
    identity = {
        "kind": kind,
        "item_id": item["id"],
        "item_hash": item.get("hash"),
        "file_id": info.get("id"),
        "file_hash": info.get("hash"),
        "file_md5": info.get("md5"),
        "size": info.get("size"),
        "source": src,
        "destination": dst,
    }
    previous = state["backups"].get(relative)
    destination = rclone.stat(dst)
    if (
        previous
        and previous.get("identity") == identity
        and destination
        and previous.get("vault_identity") == vault_identity(destination)
        and destination.get("Size") == previous.get("size")
        and previous.get("sha256")
    ):
        return "unchanged"
    origin = rclone.stat(src)
    if origin is None or int(origin.get("Size", 0)) <= 0:
        raise RcloneError("TorBox source file is unavailable or empty")
    size = int(origin["Size"])
    if info.get("size") is None or int(info["size"]) != size:
        raise RcloneError("TorBox API and WebDAV file sizes differ; leaving both copies untouched")
    if destination is None:
        # Neither copyto nor verification stages audio on local storage.
        rclone.run("copyto", src, dst, "--immutable", "--ignore-existing")
    elif destination.get("Size") != size:
        raise RcloneError("Existing vault file differs; refusing to overwrite it")
    source_hash, source_size = rclone.digest(src)
    backup_hash, backup_size = rclone.digest(dst)
    if source_size != size or backup_size != size or source_hash != backup_hash:
        raise RcloneError("Backup content verification failed; playback source preserved")
    destination = rclone.stat(dst)
    if not destination or destination.get("Size") != size:
        raise RcloneError("Vault file disappeared during verification; source preserved")
    state["backups"][relative] = {
        "identity": identity,
        "size": size,
        "sha256": source_hash,
        "vault_identity": vault_identity(destination),
    }
    return "verified"


def maintain(
    settings: Settings, api: AirLockAPI, *, apply: bool, protect_only: bool = False
) -> dict[str, Any]:
    links, vault_links = scan_links(settings)
    state = read_state(settings)
    report: dict[str, Any] = {
        "mode": "apply" if apply else "plan",
        "protected": [],
        "already_protected": [],
        "pending_protection": [],
        "unmatched_links": [],
        "backups": [],
        "errors": [],
        "vault_links_need_review": vault_links,
    }
    index: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {}
    for kind in EDIT_ROUTES:
        try:
            items = api.items(kind)
        except AirLockError as error:
            report["errors"].append(f"{kind}: {error}")
            continue
        for item in items:
            if not ready(item):
                continue
            for path, info in file_paths(item):
                index.setdefault(path, []).append((kind, item, info))
    matched = []
    selected = {}
    for link in links:
        candidates = index.get(link["torbox_path"], [])
        if len(candidates) != 1:
            report["unmatched_links"].append(link["library_path"])
            continue
        kind, item, info = candidates[0]
        matched.append((link, kind, item, info))
        selected[(kind, item["id"])] = item
        if apply:
            state["origins"][link["library_path"]] = {
                **link,
                "kind": kind,
                "item_id": item["id"],
                "file_id": info.get("id"),
            }
    if apply:
        write_state(settings, state)
    for (kind, item_id), item in selected.items():
        label = f"{kind}/{item_id}"
        if item.get("airlocked") is True:
            report["already_protected"].append(label)
        elif not apply:
            report["pending_protection"].append(label)
        else:
            try:
                api.protect(kind, item)
            except AirLockError as error:
                report["pending_protection"].append(label)
                report["errors"].append(f"{label}: {error}")
            else:
                report["protected"].append(label)
    if not apply or protect_only or not matched:
        return report
    rclone = Rclone(settings)
    try:
        source = source_remote(settings)
        remote_path(settings.vault_remote, "")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        report["errors"].append(f"Backup configuration: {error}")
        return report
    # Rotate attempts so a failing book cannot starve other backups indefinitely.
    cursor = state.get("last_backup_path", "")
    matched.sort(key=lambda entry: (entry[0]["library_path"] <= cursor, entry[0]["library_path"]))
    attempts = 0
    for link, kind, item, info in matched:
        if attempts >= settings.max_backups:
            break
        relative = link["library_path"]
        try:
            result = backup(link, kind, item, info, settings, state, rclone, source)
        except (RcloneError, ValueError, OSError, subprocess.SubprocessError) as error:
            report["errors"].append(f"Backup {relative}: {error}")
            attempts += 1
        else:
            report["backups"].append({"library_path": relative, "status": result})
            attempts += result != "unchanged"
        state["last_backup_path"] = relative
        write_state(settings, state)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Enable AirLock and write verified backups"
    )
    parser.add_argument("--protect-only", action="store_true", help="Skip vault transfers")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if settings.max_backups < 1:
            raise ValueError("TORBOX_ARCHIVE_MAX_BACKUPS must be positive")
        api = AirLockAPI(settings.token())
        if args.apply:
            settings.state_dir.mkdir(parents=True, exist_ok=True)
            with (settings.state_dir / "archive.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    print(json.dumps({"status": "already_running"}))
                    return 0
                report = maintain(settings, api, apply=True, protect_only=args.protect_only)
        else:
            report = maintain(settings, api, apply=False, protect_only=args.protect_only)
        print(json.dumps(report, indent=2))
        return (
            1
            if report["errors"] or report["pending_protection"] or report["unmatched_links"]
            else 0
        )
    except (AirLockError, ValueError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
