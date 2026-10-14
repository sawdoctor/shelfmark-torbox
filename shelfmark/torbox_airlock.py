"""Small, dependency-free AirLock API used by Shelfmark and the host archive job.

Only enables protection. Never renames, untags, unlocks, deletes or re-downloads
an item. Kept independent of the application so the host job needs no venv.
"""

from __future__ import annotations

import json
import time
from pathlib import PurePosixPath
from typing import Any
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.torbox.app/v1/api"
EDIT_ROUTES = {
    "torrents": ("edittorrent", "torrent_id"),
    "usenet": ("editusenetdownload", "usenet_download_id"),
    "webdl": ("editwebdownload", "webdl_id"),
}
AUDIO_SUFFIXES = frozenset(
    {".m4b", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".wma", ".aac", ".wav", ".opus"}
)


class AirLockError(RuntimeError):
    """Protection could not be confirmed; preserve the source and retry later."""


def ready(item: dict[str, Any]) -> bool:
    """Only cached, complete, present items can support audiobook playback."""
    return (
        item.get("download_present") is True
        and item.get("download_finished") is True
        and str(item.get("download_state", "")).lower()
        in {"cached", "completed", "downloaded", "finished", "seeding"}
    )


def file_paths(item: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Exact plausible WebDAV paths, with no fuzzy title or basename matching."""
    result = []
    name = str(item.get("name") or "")
    for info in item.get("files") or []:
        if not isinstance(info, dict):
            continue
        raw = str(info.get("name") or "").lstrip("/")
        path = PurePosixPath(raw)
        if not raw or ".." in path.parts or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        candidates = {path.as_posix()}
        if name and "/" not in name and name not in {".", ".."}:
            if len(path.parts) == 1:
                candidates = {(PurePosixPath(name) / path).as_posix()}
            else:
                candidates.add((PurePosixPath(name) / PurePosixPath(*path.parts[1:])).as_posix())
        result.extend((candidate, info) for candidate in sorted(candidates))
    return result


def airlock_payload(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    """PUT replaces editable fields: preserve every one from a fresh read."""
    if kind not in EDIT_ROUTES:
        raise AirLockError("Unsupported TorBox download type")
    if not ready(item):
        raise AirLockError("TorBox item is not complete and present")
    if "airlocked" not in item:
        raise AirLockError("TorBox API does not expose AirLock for this item")
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise AirLockError("Missing item name; refusing to change metadata")
    for key in ("tags", "alternative_hashes"):
        if not isinstance(item.get(key), list) or any(
            not isinstance(value, str) for value in item[key]
        ):
            raise AirLockError(f"Missing or invalid {key}; refusing to change metadata")
    try:
        item_id = int(item["id"])
    except (KeyError, TypeError, ValueError) as error:
        raise AirLockError("Invalid TorBox item ID") from error
    return {
        EDIT_ROUTES[kind][1]: item_id,
        "name": name,
        "tags": list(item["tags"]),
        "alternative_hashes": list(item["alternative_hashes"]),
        "airlocked": True,
    }


class AirLockAPI:
    """Documented TorBox edit/list endpoints, with read-after-write verification."""

    def __init__(self, token: str) -> None:
        if not token.strip():
            raise AirLockError("TorBox API key is missing")
        self._token = token.strip()
        self._last_request = 0.0

    def request(
        self,
        kind: str,
        route: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        delay = 0.35 - (time.monotonic() - self._last_request)
        if delay > 0:
            time.sleep(delay)
        self._last_request = time.monotonic()
        url = f"{API_BASE}/{kind}/{route}"
        if params:
            url += "?" + urlencode(params)
        request = Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="PUT" if payload is not None else "GET",
        )
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
        except URLError as error:
            # Do not log request objects, headers, tokens or raw response bodies.
            raise AirLockError("TorBox API request failed; retry on the next run") from error
        except OSError as error:
            raise AirLockError("TorBox API connection failed; retry on the next run") from error
        except ValueError as error:
            raise AirLockError("TorBox returned an invalid API response") from error
        if not isinstance(result, dict) or result.get("success") is not True:
            code = result.get("error") if isinstance(result, dict) else "INVALID_RESPONSE"
            raise AirLockError(f"TorBox rejected the request ({code})")
        return result.get("data")

    def item(self, kind: str, item_id: int) -> dict[str, Any]:
        data = self.request(kind, "mylist", params={"id": item_id, "bypass_cache": "true"})
        if isinstance(data, list):
            data = next(
                (entry for entry in data if isinstance(entry, dict) and entry.get("id") == item_id),
                None,
            )
        if not isinstance(data, dict) or data.get("id") != item_id:
            raise AirLockError("TorBox item was not found")
        return data

    def items(self, kind: str) -> list[dict[str, Any]]:
        result = []
        seen = set()
        while True:
            page = self.request(
                kind,
                "mylist",
                params={"bypass_cache": "true", "offset": len(result), "limit": 100},
            )
            if not isinstance(page, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("id"), int) for item in page
            ):
                raise AirLockError("Unexpected TorBox list response")
            for item in page:
                if item.get("id") in seen:
                    raise AirLockError(
                        "TorBox pagination repeated an item; refusing partial inventory"
                    )
                seen.add(item.get("id"))
            result.extend(page)
            if len(page) < 100:
                return result

    def protect(self, kind: str, item: dict[str, Any]) -> dict[str, Any]:
        current = self.item(kind, int(item["id"]))
        if not ready(current):
            raise AirLockError("TorBox item is not complete and present")
        if current.get("airlocked") is True:
            return current
        payload = airlock_payload(kind, current)
        self.request(kind, EDIT_ROUTES[kind][0], payload=payload)
        confirmed = self.item(kind, int(current["id"]))
        if confirmed.get("airlocked") is not True or not ready(confirmed):
            raise AirLockError("AirLock could not be confirmed; source has been preserved")
        if any(
            confirmed.get(key) != payload[key] for key in ("name", "tags", "alternative_hashes")
        ):
            raise AirLockError("Metadata changed during AirLock; review this item before retrying")
        return confirmed
