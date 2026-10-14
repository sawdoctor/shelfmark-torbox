#!/usr/bin/env python3
"""Upgrade only the two inspected Shelfmark services, retaining server customizations.

Defaults to preparation. --apply is authorized only for this server's two named
services. No mount services, bridges, Plex or arr services are operated on.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

OLD_PATCH = "6b38603b8b2fb57964050c9d1a4385eb20d8120843709bee1714cd00c4bede5f"
NEW_PATCH = "ae0c9ac4b380bd64d3140a998594c650df188bf6bb805736a83cb0e6f6d6bf22"
PATCH_URL = "https://raw.githubusercontent.com/sawdoctor/shelfmark-ebooks/7b47e02d415936850461238e13ec4f9d4e2cce5b/patches/v1.4.0/base_handler.py"
PATCH_PATH = Path("/home/sawdoctor/docker/shelfmark-ebooks/patches/base_handler.py")
TARGETS = {
    "shelfmark-ebooks": "ghcr.io/calibrain/shelfmark:v1.4.0",
    "shelfmark-torbox": "ghcr.io/sawdoctor/shelfmark-torbox:0.1.0-torbox.1",
}
ROOT = Path("/home/sawdoctor/torbox-recovery-20261011")
STAGE: Path | None = None


def note(message: str) -> None:
    if STAGE is not None:
        with (STAGE / "progress.log").open("a") as out:
            out.write(message + "\n")
    try:
        print(message, flush=True)
    except BrokenPipeError:
        sys.stdout = open(os.devnull, "w")


def run(args: list[str], *, cwd: Path | None = None, timeout: int = 30) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if STAGE is not None:
        with (STAGE / "commands.log").open("a") as out:
            # Private file: Compose errors can include resolved configuration.
            out.write(json.dumps(args) + "\n" + result.stdout + result.stderr + "\n")
    if result.returncode:
        raise RuntimeError("Command failed; private details retained in commands.log")
    return result.stdout


def inspect(name: str) -> dict:
    return json.loads(run(["docker", "inspect", name]))[0]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n")


def state(phase: str, **details: object) -> None:
    assert STAGE is not None
    save_json(STAGE / "update-state.json", {"phase": phase, **details})


def atomic_write(path: Path, data: bytes) -> None:
    before = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=".shelfmark-update-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, before.st_mode & 0o777)
        if before.st_uid != os.geteuid() or before.st_gid != os.getegid():
            os.chown(temporary, before.st_uid, before.st_gid)
        os.replace(temporary, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def replace_image(text: str, service: str, image: str) -> str:
    """Surgical edit; retain comments, variables and all other Compose settings."""
    lines = text.splitlines(keepends=True)
    headers = [i for i, line in enumerate(lines) if re.match(r"^services:\s*(?:#.*)?$", line.rstrip())]
    if len(headers) != 1:
        raise ValueError("Expected one ordinary top-level services section")
    start = headers[0] + 1
    service_headers = []
    for i in range(start, len(lines)):
        if lines[i].strip() and not lines[i].lstrip().startswith("#") and not lines[i][0].isspace():
            break
        if re.match(r"^\s+" + re.escape(service) + r":\s*(?:#.*)?$", lines[i].rstrip()):
            service_headers.append(i)
    if len(service_headers) != 1:
        raise ValueError("Expected one ordinary selected service definition")
    start = service_headers[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].lstrip()
        if stripped.strip() and not stripped.startswith("#") and len(lines[i]) - len(stripped) <= indent:
            end = i
            break
    matches = [i for i in range(start + 1, end)
               if re.match(r"^\s+image:", lines[i])
               and len(lines[i]) - len(lines[i].lstrip()) == indent + 2]
    if len(matches) > 1:
        raise ValueError("Ambiguous image setting")
    line = " " * (indent + 2) + "image: " + image + "\n"
    if matches:
        lines[matches[0]] = line
    else:
        lines.insert(start + 1, line)
    return "".join(lines)


def compose(plan: dict, *args: str, files: list[Path] | None = None, timeout: int = 30) -> str:
    command = ["docker", "compose", "--project-directory", str(plan["cwd"]), "-p", plan["project"]]
    for file in files or [plan["file"]]:
        command.extend(["-f", str(file)])
    return run(command + list(args), cwd=plan["cwd"], timeout=timeout)


def model(plan: dict, files: list[Path] | None = None) -> dict:
    return json.loads(compose(plan, "config", "--format", "json", files=files))


def mounts(container: dict) -> list[dict]:
    fields = ("Type", "Source", "Destination", "RW", "Propagation")
    return sorted([{k: m.get(k) for k in fields} for m in container["Mounts"]], key=lambda m: m["Destination"])


def environment(container: dict) -> dict:
    return dict(entry.split("=", 1) for entry in container["Config"].get("Env", []) if "=" in entry)


def configured_environment(plan: dict, container: dict) -> None:
    live = environment(container)
    expected = plan["model"]["services"][plan["name"]].get("environment", {})
    if any(live.get(key) != (str(value) if value is not None else None) for key, value in expected.items()):
        raise ValueError("Compose environment differs from running settings; no restart performed")


def configured_mounts(plan: dict, container: dict) -> None:
    expected = {}
    for volume in plan["model"]["services"][plan["name"]].get("volumes", []):
        source = volume.get("source", "")
        if volume["type"] == "volume":
            source = plan["model"].get("volumes", {}).get(source, {}).get("name", source)
        expected[volume["target"]] = (volume["type"], source, not volume.get("read_only", False))
    actual = {m["Destination"]: (m["Type"], m.get("Name") if m["Type"] == "volume" else m["Source"], m["RW"])
              for m in container["Mounts"]}
    if expected != actual:
        raise ValueError("Compose mounts differ from running mounts; no restart performed")


def configured_connections(plan: dict, container: dict) -> None:
    selected = plan["model"]["services"][plan["name"]]
    expected_ports = {}
    for port in selected.get("ports", []):
        key = str(port["target"]) + "/" + port.get("protocol", "tcp")
        expected_ports.setdefault(key, []).append({"HostIp": port.get("host_ip", ""), "HostPort": str(port.get("published", ""))})
    if expected_ports != (container["HostConfig"].get("PortBindings") or {}):
        raise ValueError("Compose ports differ from running bindings; no restart performed")
    if selected.get("network_mode"):
        raise ValueError("Nonstandard network_mode needs separate preservation handling")
    expected_networks = {plan["model"]["networks"][key]["name"] for key in selected.get("networks", {})}
    if expected_networks != set(container["NetworkSettings"]["Networks"]):
        raise ValueError("Compose networks differ from live connections; no restart performed")
    if selected.get("post_start") or selected.get("pre_stop"):
        raise ValueError("Compose lifecycle hooks require inspection before this scoped updater")


def backup_config(plan: dict) -> None:
    """Only application settings/databases/code; never book or download payloads."""
    source = next(Path(m["Source"]) for m in plan["before"]["Mounts"] if m["Destination"] == "/config")
    destination = plan["dir"] / "config-before"
    settings_suffixes = {".json", ".yaml", ".yml", ".toml", ".ini", ".conf", ".cfg", ".py", ".cjs", ".db", ".sqlite", ".sqlite3"}
    for file in source.rglob("*"):
        if file.is_symlink() or not file.is_file():
            continue
        if file.suffix.lower() not in settings_suffixes and not file.name.endswith(("-wal", "-shm", "-journal")):
            continue
        target = destination / file.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file, target)
    # The service is stopped; DB sidecars and databases are copied together.
    # Backups are never automatically restored over newer application state.


def runtime_matches(plan: dict, current: dict, expected_image: str) -> None:
    before = plan["before"]
    if not current["State"]["Running"] or current["Image"] != expected_image:
        raise ValueError("Selected service is not running the expected image")
    if mounts(current) != mounts(before):
        raise ValueError("Selected service mount settings changed")
    if current["HostConfig"].get("PortBindings") != before["HostConfig"].get("PortBindings"):
        raise ValueError("Selected service port bindings changed")
    if set(current["NetworkSettings"]["Networks"]) != set(before["NetworkSettings"]["Networks"]):
        raise ValueError("Selected service networks changed")
    configured_environment(plan, current)


def start(plan: dict) -> None:
    compose(plan, "up", "-d", "--no-deps", "--no-build", "--pull", "never",
            "--force-recreate", "--wait", "--wait-timeout", "180", plan["name"], timeout=240)


def prepare() -> tuple[list[dict], bytes]:
    assert STAGE is not None
    patch = PATCH_PATH.read_bytes()
    if digest(patch) not in {OLD_PATCH, NEW_PATCH}:
        raise ValueError("Live handler differs from the inspected original or prepared upgrade")
    note("Downloading checksum-verified v1.4.0 handler; services remain running.")
    with urllib.request.urlopen(PATCH_URL, timeout=30) as response:
        new_patch = response.read(100_000)
    if digest(new_patch) != NEW_PATCH:
        raise ValueError("Adapted handler checksum failed")
    compile(new_patch, str(PATCH_PATH), "exec")
    (STAGE / "handler-proposed.py").write_bytes(new_patch)
    plans = []
    for name, image in TARGETS.items():
        before = inspect(name)
        if not before["State"]["Running"]:
            raise ValueError("Selected Shelfmark service is not running; inspect before upgrading")
        labels = before["Config"].get("Labels", {})
        files = labels.get("com.docker.compose.project.config_files", "").split(",")
        if len(files) != 1 or not files[0]:
            raise ValueError("Expected one live Compose file for each selected service")
        file = Path(files[0])
        if file != Path("/home/sawdoctor/docker") / name / "compose.yml" or file.is_symlink():
            raise ValueError("Unexpected Compose location")
        if labels.get("com.docker.compose.service") != name or labels.get("com.docker.compose.project") != name:
            raise ValueError("Unexpected Compose service or project")
        folder = STAGE / name
        folder.mkdir()
        plan = {"name": name, "image": image, "project": name,
                "file": file, "cwd": Path(labels.get("com.docker.compose.project.working_dir") or file.parent),
                "before": before, "dir": folder, "original": file.read_bytes()}
        if plan["cwd"] != file.parent:
            raise ValueError("Unexpected Compose working directory")
        shutil.copy2(file, folder / "compose-before.yml")
        env = file.parent / ".env"
        if env.exists():
            shutil.copy2(env, folder / "environment-before")
        save_json(folder / "container-before.json", before)
        plan["model"] = model(plan)
        configured_environment(plan, before)
        configured_mounts(plan, before)
        configured_connections(plan, before)
        selected = plan["model"]["services"][name]
        for i, entry in enumerate(selected.get("env_file", [])):
            env_file = Path(entry if isinstance(entry, str) else entry["path"])
            if env_file.exists():
                shutil.copy2(env_file, folder / ("environment-file-" + str(i)))
        if selected.get("secrets") or selected.get("configs"):
            raise ValueError("Additional runtime file sources require explicit backup handling")
        proposed = replace_image(plan["original"].decode(), name, image).encode()
        proposed_file = file.parent / (".shelfmark-update-" + STAGE.name + ".yml")
        try:
            proposed_file.write_bytes(proposed)
            proposed_model = model(plan, [proposed_file])
        finally:
            proposed_file.unlink(missing_ok=True)
        expected = copy.deepcopy(plan["model"])
        expected["services"][name]["image"] = image
        if proposed_model != expected:
            raise ValueError("Proposed Compose change affects more than the selected image")
        plan["proposed"] = proposed
        (folder / "compose-proposed.yml").write_bytes(proposed)
        if name == "shelfmark-ebooks":
            shutil.copy2(PATCH_PATH, folder / "handler-before.py")
            for mount in before["Mounts"]:
                if mount["Destination"] != "/custom-scripts":
                    continue
                source = Path(mount["Source"])
                for script in source.rglob("*"):
                    if script.is_symlink() or not script.is_file() or script.suffix.lower() not in {".py", ".sh", ".json", ".conf"}:
                        continue
                    destination = folder / "custom-scripts-before" / script.relative_to(source)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(script, destination)
        plans.append(plan)
    return plans, new_patch


def activate(plans: list[dict], new_patch: bytes) -> None:
    assert STAGE is not None
    for plan in plans:
        note("Downloading " + plan["image"] + "; no server-side build.")
        run(["docker", "pull", plan["image"]], timeout=900)
        image = json.loads(run(["docker", "image", "inspect", plan["image"]]))[0]
        plan["image_id"] = image["Id"]
        if plan["name"] == "shelfmark-torbox" and image["Config"].get("Labels", {}).get("org.opencontainers.image.revision") != "bde6ba61fcbc733ab49cea6ef3ee493856204896":
            raise ValueError("TorBox image revision differs from the published release")
        save_json(plan["dir"] / "target-image.json", image)
    completed = []
    for plan in plans:
        name = plan["name"]
        runtime_matches(plan, inspect(name), plan["before"]["Image"])
        if plan["file"].read_bytes() != plan["original"] or model(plan) != plan["model"]:
            raise ValueError("Configuration changed during preparation; no restart performed")
        if name == "shelfmark-ebooks" and digest(PATCH_PATH.read_bytes()) not in {OLD_PATCH, NEW_PATCH}:
            raise ValueError("Handler changed during preparation")
        if plan["before"]["Image"] == plan["image_id"] and plan["original"] == plan["proposed"] and (name != "shelfmark-ebooks" or digest(PATCH_PATH.read_bytes()) == NEW_PATCH):
            if inspect(name)["State"].get("Health", {}).get("Status") == "healthy":
                completed.append(name)
                note(name + " already uses the verified upgrade; no restart needed.")
                continue
        state("updating", service=name, completed=completed)
        changed = False
        stopped = False
        try:
            note("Updating only " + name + "; saving stopped application configuration first.")
            run(["docker", "stop", "--time", "30", name], timeout=60)
            stopped = True
            backup_config(plan)
            atomic_write(plan["file"], plan["proposed"])
            changed = True
            if name == "shelfmark-ebooks":
                atomic_write(PATCH_PATH, new_patch)
            start(plan)
            current = inspect(name)
            runtime_matches(plan, current, plan["image_id"])
            if name == "shelfmark-ebooks":
                inside = run(["docker", "exec", name, "python", "-c",
                              "import hashlib; print(hashlib.sha256(open('/app/shelfmark/download/clients/base_handler.py','rb').read()).hexdigest())"])
                if inside.strip() != NEW_PATCH:
                    raise ValueError("Container is not using the adapted handler")
            save_json(plan["dir"] / "container-after.json", current)
            completed.append(name)
            state("service-complete", service=name, completed=completed)
            note(name + " is healthy; existing mounts, ports, networks and configured environment retained.")
        except Exception:
            note("Update failed for " + name + "; restoring its previous Compose and handler.")
            recovered = False
            try:
                if changed:
                    run(["docker", "stop", "--time", "30", name], timeout=60)
                    atomic_write(plan["file"], plan["original"])
                    if name == "shelfmark-ebooks":
                        atomic_write(PATCH_PATH, (plan["dir"] / "handler-before.py").read_bytes())
                    # Restore only the previous image/settings, never stale application databases.
                    rollback_file = plan["file"].parent / (".shelfmark-rollback-" + STAGE.name + ".yml")
                    try:
                        rollback_file.write_text(replace_image(plan["original"].decode(), name, plan["before"]["Image"]))
                        compose(plan, "up", "-d", "--no-deps", "--no-build", "--pull", "never",
                                "--force-recreate", "--wait", "--wait-timeout", "180", name,
                                files=[rollback_file], timeout=240)
                    finally:
                        rollback_file.unlink(missing_ok=True)
                elif stopped:
                    run(["docker", "start", name], timeout=60)
                runtime_matches(plan, inspect(name), plan["before"]["Image"])
                recovered = True
            except Exception as recovery_error:
                (plan["dir"] / "rollback-error.txt").write_text(type(recovery_error).__name__ + "\n")
            state("update-failed", service=name, rollback_verified=recovered, completed=completed)
            note("Previous service settings restored; availability " + ("verified." if recovered else "NOT verified; recovery material retained."))
            raise
    state("update-complete", completed=completed, images=TARGETS)
    note("Both Shelfmark updates complete. No bridges, mount services, Audiobookshelf, Plex or arr containers were operated on.")
    note("No book files copied or deleted. Original configuration/database backups and old images retained.")


def main() -> int:
    global STAGE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Download images and update only the two selected services")
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    ROOT.mkdir(parents=True, exist_ok=True)
    lock = (ROOT / ".shelfmark-update.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        note("A Shelfmark update is already running. Check its progress rather than starting another.")
        return 1
    STAGE = Path(tempfile.mkdtemp(prefix="shelfmark-update-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-", dir=ROOT))
    note("Private rollback and status directory: " + str(STAGE))
    state("preparing")
    try:
        plans, patch = prepare()
        state("prepared", images=TARGETS)
        note("Preparation verified: only the two image references and the matched eBook handler will change.")
        if args.apply:
            activate(plans, patch)
        else:
            note("Preparation only; no services stopped or persistent configuration changed.")
        return 0
    except Exception as error:
        (STAGE / "error.txt").write_text(type(error).__name__ + ": " + str(error) + "\n")
        existing = json.loads((STAGE / "update-state.json").read_text())
        if existing["phase"] in {"preparing", "prepared"}:
            state("preflight-failed", error=type(error).__name__)
        note("Stopped: " + str(error) if isinstance(error, (ValueError, RuntimeError)) else "Stopped: " + type(error).__name__ + " (private details retained).")
        note("Recovery and progress: " + str(STAGE))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
