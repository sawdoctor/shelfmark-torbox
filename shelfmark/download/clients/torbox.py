"""Torbox debrid service client for Shelfmark."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn

import requests

from shelfmark.config.env import TMP_DIR
from shelfmark.core.config import config
from shelfmark.core.logger import setup_logger
from shelfmark.download.clients import (
    DownloadClient,
    DownloadState,
    DownloadStatus,
    register_client,
)
from shelfmark.download.clients._coercion import config_text
from shelfmark.download.http import download_url
from shelfmark.download.network import get_ssl_verify

if TYPE_CHECKING:
    from collections.abc import Callable
    from io import BytesIO
    from threading import Event

logger = setup_logger(__name__)

_API_BASE = "https://api.torbox.app/v1/api"
_API_TIMEOUT = 30
_STATUS_TIMEOUT = 15
_READY_STATES = frozenset({"cached", "completed"})
_WEBDL_READY_STATES = frozenset({"cached", "completed", "downloaded", "finished"})
_WEBDL_ERROR_STATES = frozenset({"error", "failed", "virus"})
_WEBDL_POLL_INTERVAL = 2
_BOOK_EXTENSIONS = (
    ".aac", ".azw", ".azw3", ".cbr", ".cbz", ".djvu", ".doc", ".docx",
    ".epub", ".fb2", ".flac", ".lit", ".m4a", ".m4b", ".mobi", ".mp3",
    ".ogg", ".opus", ".pdf", ".rtf", ".txt", ".wma",
)


def _raise_runtime_error(message: str) -> NoReturn:
    raise RuntimeError(message)


def _raise_type_error(message: str) -> NoReturn:
    raise TypeError(message)


@dataclass
class _DownloadState:
    """Internal mutable state for an in-progress Torbox download."""

    torrent_id: str
    name: str
    target_dir: Path
    phase: str = "uploading"
    error_message: str | None = None
    progress: float = 0.0
    download_thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@register_client("torrent")
class TorboxClient(DownloadClient):
    """Download torrent content through Torbox's CDN.

    API documentation: https://api.torbox.app/docs
    """

    protocol = "torrent"
    name = "torbox"

    _downloads: ClassVar[dict[str, _DownloadState]] = {}
    _downloads_lock = threading.Lock()

    def __init__(self) -> None:
        self._api_key = config_text(config.get("TORBOX_API_KEY", ""))

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    @staticmethod
    def is_configured() -> bool:
        client = config_text(config.get("PROWLARR_TORRENT_CLIENT", ""))
        api_key = config_text(config.get("TORBOX_API_KEY", ""))
        return client == "torbox" and bool(api_key)


    def test_connection(self) -> tuple[bool, str]:
        """Validate Torbox credentials and torrent API access."""
        if not self._api_key:
            return False, "Torbox API Key is required"

        try:
            user_url = f"{_API_BASE}/user/me"
            response = requests.get(
                user_url,
                headers=self._auth_headers(),
                timeout=_STATUS_TIMEOUT,
                verify=get_ssl_verify(user_url),
            )
            response.raise_for_status()
            data = self._response_data(response.json())
            if not isinstance(data, dict):
                raise TypeError("Unexpected Torbox user response")

            email = str(data.get("email") or "Unknown")

            torrents_url = f"{_API_BASE}/torrents/mylist"
            response = requests.get(
                torrents_url,
                headers=self._auth_headers(),
                params={"bypass_cache": "true"},
                timeout=_STATUS_TIMEOUT,
                verify=get_ssl_verify(torrents_url),
            )
            response.raise_for_status()
            self._response_data(response.json())

        except (requests.exceptions.RequestException, RuntimeError, TypeError, ValueError) as error:
            return False, f"Connection failed: {error}"

        return True, f"Connected to Torbox as '{email}'"
    def add_download(
        self,
        url: str,
        name: str,
        category: str | None = None,
        expected_hash: str | None = None,
        **kwargs: object,
    ) -> str:
        """Add a magnet to Torbox and return its torrent ID."""
        if not self._api_key:
            msg = "Torbox API key is not configured"
            raise RuntimeError(msg)

        magnet_link = url
        if not magnet_link.startswith("magnet:") and expected_hash:
            magnet_link = f"magnet:?xt=urn:btih:{expected_hash}"

        create_url = f"{_API_BASE}/torrents/createtorrent"
        try:
            response = requests.post(
                create_url,
                headers=self._auth_headers(),
                data={"magnet": magnet_link, "name": name},
                timeout=_API_TIMEOUT,
                verify=get_ssl_verify(create_url),
            )
            response.raise_for_status()
            data = self._response_data(response.json())
            torrent_id = str(data.get("torrent_id", data.get("id", "")))
            if not torrent_id:
                msg = "No torrent ID returned from Torbox"
                _raise_runtime_error(msg)

            mount_value = config_text(
                config.get("TORBOX_MOUNT_PATH", "/mnt/torbox-audiobooks")
            )
            target_dir = Path(mount_value or "/mnt/torbox-audiobooks")
            state = _DownloadState(torrent_id, name, target_dir, phase="waiting_torbox")
            with self._downloads_lock:
                self._downloads[torrent_id] = state
        except Exception:
            logger.exception("Failed to add magnet to Torbox")
            raise
        else:
            logger.info("Added torrent to Torbox: ID %s (%s)", torrent_id, name)
            return torrent_id

    def get_status(self, download_id: str) -> DownloadStatus:
        """Poll Torbox and start the local CDN download when ready."""
        state = self._ensure_state(download_id)
        with state.lock:
            if state.phase == "error":
                return DownloadStatus.error(state.error_message or "Torbox error")
            if state.phase == "complete":
                return DownloadStatus(100.0, DownloadState.COMPLETE, "Complete", True, str(state.target_dir))
            if state.phase == "downloading_http":
                return DownloadStatus(state.progress, DownloadState.DOWNLOADING, "Downloading files via HTTP...", False, None)

        try:
            list_url = f"{_API_BASE}/torrents/mylist"
            response = requests.get(
                list_url,
                headers=self._auth_headers(),
                params={"id": download_id, "bypass_cache": "true"},
                timeout=_STATUS_TIMEOUT,
                verify=get_ssl_verify(list_url),
            )
            response.raise_for_status()
            torrent = self._response_data(response.json())
            if isinstance(torrent, list):
                torrent = torrent[0] if torrent else {}
            if not isinstance(torrent, dict):
                msg = "Unexpected Torbox torrent response"
                _raise_type_error(msg)
            return self._handle_torrent(torrent, state)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as error:
            logger.warning(
                "Temporary Torbox API problem checking %s; will retry on next poll: %s",
                download_id,
                error,
            )
            return DownloadStatus(
                state.progress,
                DownloadState.DOWNLOADING,
                "TorBox API temporarily unavailable; retrying",
                False,
                None,
            )
        except Exception as error:
            logger.exception("Error checking Torbox status for %s", download_id)
            return DownloadStatus.error(str(error))

    def remove(self, download_id: str, *, delete_files: bool = False) -> bool:
        """Delete the torrent from Torbox and clean up local files."""
        try:
            url = f"{_API_BASE}/torrents/controltorrent"
            requests.post(
                url,
                headers=self._auth_headers(),
                json={"torrent_id": int(download_id), "operation": "delete"},
                timeout=_STATUS_TIMEOUT,
                verify=get_ssl_verify(url),
            ).raise_for_status()
        except (requests.exceptions.RequestException, ValueError) as error:
            logger.warning("Failed to delete Torbox torrent %s: %s", download_id, error)

        with self._downloads_lock:
            self._downloads.pop(download_id, None)

        # Never delete filesystem data here. For this client the completed
        # path is the read-only TorBox WebDAV mount.
        return True


    def get_download_path(self, download_id: str) -> str | None:
        """Return the real Torbox WebDAV path for a completed torrent."""
        list_url = f"{_API_BASE}/torrents/mylist"

        try:
            response = requests.get(
                list_url,
                headers=self._auth_headers(),
                params={"id": download_id, "bypass_cache": "true"},
                timeout=_STATUS_TIMEOUT,
                verify=get_ssl_verify(list_url),
            )
            response.raise_for_status()
            torrent = self._response_data(response.json())
        except (requests.exceptions.RequestException, RuntimeError, TypeError, ValueError):
            logger.exception("Failed to resolve Torbox WebDAV path for %s", download_id)
            return None

        if isinstance(torrent, list):
            torrent = torrent[0] if torrent else {}

        if not isinstance(torrent, dict):
            return None

        if not torrent.get("download_present", False):
            return None

        mount_root = Path(
            config_text(
                config.get("TORBOX_MOUNT_PATH", "/mnt/torbox-audiobooks")
            )
            or "/mnt/torbox-audiobooks"
        )

        # Torbox API file names contain the actual torrent directory structure.
        # If all files share a top-level directory, that is the directory WebDAV exposes.
        top_levels: list[str] = []

        files = torrent.get("files") or []
        if isinstance(files, list):
            for file_info in files:
                if not isinstance(file_info, dict):
                    continue

                raw_name = str(file_info.get("name") or "").strip("/")
                if not raw_name:
                    continue

                parts = Path(raw_name).parts
                if len(parts) > 1:
                    top_levels.append(parts[0])

        folder_name: str | None = None

        if top_levels and len(set(top_levels)) == 1:
            folder_name = top_levels[0]

        # Torrents whose files live directly at their root normally use the
        # torrent name as the WebDAV directory.
        if not folder_name:
            candidate_name = str(torrent.get("name") or "").strip("/")
            if candidate_name:
                folder_name = candidate_name

        if not folder_name:
            return None

        completed_path = mount_root / folder_name

        with self._downloads_lock:
            state = self._downloads.get(download_id)
            if state is not None:
                state.target_dir = completed_path
                state.phase = "complete"
                state.progress = 100.0

        logger.info(
            "Resolved Torbox WebDAV path for %s: %s",
            download_id,
            completed_path,
        )

        return str(completed_path)
    def download_web_url(
        self,
        url: str,
        name: str,
        size: str = "",
        progress_callback: Callable[[float], None] | None = None,
        cancel_flag: Event | None = None,
        status_callback: Callable[[str, str | None], None] | None = None,
    ) -> BytesIO | None:
        """Fetch a direct URL through Torbox's web-download service."""
        if not self._api_key:
            msg = "Torbox API key is not configured"
            raise RuntimeError(msg)
        if cancel_flag and cancel_flag.is_set():
            return None

        create_url = f"{_API_BASE}/webdl/createwebdownload"
        response = requests.post(
            create_url,
            headers=self._auth_headers(),
            data={"link": url, "name": name},
            timeout=_API_TIMEOUT,
            verify=get_ssl_verify(create_url),
        )
        response.raise_for_status()
        data = self._response_data(response.json())
        if not isinstance(data, dict):
            msg = "Unexpected Torbox web download response"
            _raise_type_error(msg)
        web_id = str(data.get("webdownload_id", data.get("web_id", data.get("id", ""))))
        if not web_id:
            msg = "No web download ID returned from Torbox"
            _raise_runtime_error(msg)

        try:
            while True:
                if cancel_flag and cancel_flag.is_set():
                    return None
                web_download = self._get_web_download(web_id)
                state = str(web_download.get("download_state", web_download.get("status", ""))).lower()
                if state in _WEBDL_ERROR_STATES:
                    msg = str(web_download.get("error") or "Torbox web download failed")
                    _raise_runtime_error(msg)
                if state in _WEBDL_READY_STATES:
                    break
                if status_callback:
                    status_callback("resolving", "Torbox is retrieving the file")
                time.sleep(_WEBDL_POLL_INTERVAL)

            if status_callback:
                status_callback("downloading", "Downloading via Torbox")
            direct_url = self._request_web_download_link(web_id)
            return download_url(
                direct_url,
                size,
                progress_callback,
                cancel_flag,
                status_callback=status_callback,
                referer="https://torbox.app/",
            )
        finally:
            self._remove_web_download(web_id)

    @staticmethod
    def _response_data(payload: dict[str, Any]) -> Any:
        """Return Torbox's data envelope or surface its API error detail."""
        if not payload.get("success", False):
            msg = str(payload.get("detail") or payload.get("error") or "Torbox API error")
            raise RuntimeError(msg)
        return payload.get("data")

    def _ensure_state(self, download_id: str) -> _DownloadState:
        with self._downloads_lock:
            state = self._downloads.get(download_id)
            if state:
                return state
            state = _DownloadState(download_id, f"Download {download_id}", TMP_DIR / f"torbox_{download_id}", phase="waiting_torbox")
            self._downloads[download_id] = state
            return state

    def _handle_torrent(self, torrent: dict[str, Any], state: _DownloadState) -> DownloadStatus:
        """Return the mounted TorBox path once the torrent is ready.

        Audiobook payloads remain in TorBox. Shelfmark must never retrieve
        them through requestdl/HTTP for this workflow.
        """
        status = str(torrent.get("download_state", "")).lower()
        progress = float(torrent.get("progress", 0.0)) * 100.0

        if status in _READY_STATES:
            torrent_name = str(torrent.get("name") or state.name).strip()
            if not torrent_name:
                return DownloadStatus.error("TorBox returned a ready torrent without a name")

            mount_value = config_text(
                config.get("TORBOX_MOUNT_PATH", "/mnt/torbox-audiobooks")
            )
            mount_root = Path(mount_value or "/mnt/torbox-audiobooks")

            # Prefer the actual top-level directory reported by TorBox's
            # file list. This handles torrents whose WebDAV directory differs
            # from TorBox's friendly/display name.
            top_levels: list[str] = []
            files = torrent.get("files") or []

            if isinstance(files, list):
                for file_info in files:
                    if not isinstance(file_info, dict):
                        continue

                    raw_name = str(file_info.get("name") or "").strip("/")
                    if not raw_name:
                        continue

                    parts = Path(raw_name).parts
                    if len(parts) > 1:
                        top_levels.append(parts[0])

            if top_levels and len(set(top_levels)) == 1:
                folder_name = top_levels[0]
            else:
                folder_name = torrent_name

            name_path = Path(folder_name)
            if name_path.is_absolute() or ".." in name_path.parts:
                return DownloadStatus.error("TorBox returned an unsafe WebDAV path")

            completed_path = mount_root / folder_name

            # TorBox can expose the WebDAV directory before the files inside
            # it have propagated. Do not report COMPLETE until Shelfmark can
            # actually see a usable audiobook/archive payload.
            payload_exts = {
                ".m4b", ".mp3", ".m4a", ".mp4", ".flac", ".ogg",
                ".wma", ".aac", ".wav", ".opus", ".zip", ".rar",
            }

            payload_visible = False

            if completed_path.is_dir():
                try:
                    payload_visible = any(
                        child.is_file() and child.suffix.lower() in payload_exts
                        for child in completed_path.rglob("*")
                    )
                except OSError as error:
                    logger.debug(
                        "TorBox WebDAV path not fully readable yet for %s: %s",
                        download_id,
                        error,
                    )

            if not payload_visible:
                with state.lock:
                    state.target_dir = completed_path
                    state.phase = "waiting_webdav"
                    state.progress = 99.0

                logger.info(
                    "TorBox ready; waiting for audiobook files on WebDAV: %s",
                    completed_path,
                )

                return DownloadStatus(
                    99.0,
                    DownloadState.DOWNLOADING,
                    "TorBox ready; waiting for WebDAV files",
                    False,
                    None,
                )

            with state.lock:
                state.target_dir = completed_path
                state.phase = "complete"
                state.progress = 100.0

            logger.info(
                "TorBox WebDAV path is now available: %s",
                completed_path,
            )

            return DownloadStatus(
                100.0,
                DownloadState.COMPLETE,
                "TorBox ready on mounted storage",
                True,
                str(completed_path),
            )

        if status == "paused":
            return DownloadStatus(
                progress * 0.5,
                DownloadState.PAUSED,
                "TorBox torrent is paused",
                False,
                None,
            )

        if status == "stalled (no seeds)":
            return DownloadStatus(
                progress * 0.5,
                DownloadState.DOWNLOADING,
                "TorBox torrent stalled (no seeds)",
                False,
                None,
            )

        return DownloadStatus(
            progress * 0.5,
            DownloadState.DOWNLOADING,
            f"TorBox downloading torrent ({torrent.get('name', state.name)})",
            False,
            None,
            download_speed=int(torrent.get("download_speed", 0)),
            eta=torrent.get("eta"),
        )

    def _get_web_download(self, web_id: str) -> dict[str, Any]:
        url = f"{_API_BASE}/webdl/mylist"
        response = requests.get(
            url,
            headers=self._auth_headers(),
            params={"id": web_id, "bypass_cache": "true"},
            timeout=_STATUS_TIMEOUT,
            verify=get_ssl_verify(url),
        )
        response.raise_for_status()
        data = self._response_data(response.json())
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            msg = "Unexpected Torbox web download status response"
            _raise_type_error(msg)
        return data

    def _request_web_download_link(self, web_id: str) -> str:
        url = f"{_API_BASE}/webdl/requestdl"
        response = requests.get(
            url,
            params={"token": self._api_key, "web_id": web_id},
            timeout=_API_TIMEOUT,
            verify=get_ssl_verify(url),
        )
        response.raise_for_status()
        data = self._response_data(response.json())
        if not isinstance(data, str) or not data:
            msg = "Torbox did not return a web download link"
            _raise_runtime_error(msg)
        return data

    def _remove_web_download(self, web_id: str) -> None:
        url = f"{_API_BASE}/webdl/controlwebdownload"
        try:
            requests.post(
                url,
                headers=self._auth_headers(),
                json={"webdl_id": int(web_id), "operation": "delete"},
                timeout=_STATUS_TIMEOUT,
                verify=get_ssl_verify(url),
            ).raise_for_status()
        except (requests.exceptions.RequestException, ValueError):
            logger.warning("Failed to delete Torbox web download %s", web_id)

    @staticmethod
    def _safe_filename(filename: str) -> Path:
        path = Path(filename.lstrip("/"))
        if not path.parts or ".." in path.parts:
            return Path(path.name or "download")
        return path
