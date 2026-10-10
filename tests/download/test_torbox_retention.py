"""Offline retention checks: no live TorBox calls or real audiobook transfers."""

import copy
import hashlib
import io
import json
import sys
from unittest.mock import MagicMock

import pytest

from scripts.torbox_audiobook_archive import (
    Rclone,
    RcloneError,
    Settings,
    backup,
    maintain,
    remote_path,
)
from shelfmark.core.utils import AUDIOBOOK_FORMATS
from shelfmark.download.clients import DownloadState
from shelfmark.download.clients.torbox import TorboxClient, _DownloadState
from shelfmark.download.postprocess.transfer import _create_symlink_no_fallback
from shelfmark.torbox_airlock import (
    AUDIO_SUFFIXES,
    EDIT_ROUTES,
    AirLockAPI,
    AirLockError,
    airlock_payload,
    file_paths,
)


@pytest.fixture
def item():
    return {
        "id": 7,
        "name": "Original Release",
        "hash": "torrent-hash",
        "tags": ["Audiobooks", "keep me"],
        "alternative_hashes": ["a" * 40],
        "airlocked": False,
        "download_present": True,
        "download_finished": True,
        "download_state": "cached",
        "size": 5,
        "files": [{"id": 0, "name": "Original Release/chapter.m4b", "size": 5}],
    }


@pytest.fixture
def settings(tmp_path):
    library = tmp_path / "library"
    mount = tmp_path / "mount"
    library.mkdir()
    mount.mkdir()
    return Settings(
        library=library,
        mount=mount,
        vault_mount=tmp_path / "vault",
        vault_remote="gdrive:Shelfmark-TorBox-Archive",
        source_remote="torbox-audiobooks:Audiobooks",
        state_dir=tmp_path / "state",
        shelfmark_config=tmp_path / "config",
    )


def add_link(settings, target="Original Release/chapter.m4b", name="Library Book.m4b"):
    link = settings.library / name
    # Deliberately broken locally: matching/protection must not read audio payloads.
    link.symlink_to(settings.mount / target)
    return link


def mock_api(item, extra=()):
    api = MagicMock()
    api.items.side_effect = lambda kind: [item, *extra] if kind == "torrents" else []
    api.protect.return_value = {**item, "airlocked": True}
    return api


def test_api_uses_the_documented_put_route_and_bearer_header_not_a_url_token(monkeypatch, item):
    response = MagicMock()
    response.__enter__.return_value = io.StringIO(json.dumps({"success": True, "data": None}))
    open_url = MagicMock(return_value=response)
    monkeypatch.setattr("shelfmark.torbox_airlock.urlopen", open_url)
    api = AirLockAPI("test-token")
    api.request("torrents", "edittorrent", payload=airlock_payload("torrents", item))
    request = open_url.call_args.args[0]
    assert request.full_url == "https://api.torbox.app/v1/api/torrents/edittorrent"
    assert request.get_method() == "PUT"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert json.loads(request.data)["airlocked"] is True


def test_http_success_with_api_failure_is_not_mistaken_for_airlock_success(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value = io.StringIO(
        json.dumps({"success": False, "error": "QUOTA_TEST", "data": None})
    )
    monkeypatch.setattr("shelfmark.torbox_airlock.urlopen", MagicMock(return_value=response))
    with pytest.raises(AirLockError, match="QUOTA_TEST"):
        AirLockAPI("test-token").request("torrents", "edittorrent", payload={"airlocked": True})


@pytest.mark.parametrize("kind", EDIT_ROUTES)
def test_put_preserves_all_editable_metadata_and_confirms_airlock(item, kind):
    api = AirLockAPI("test-token")
    api.request = MagicMock(side_effect=[item, None, {**item, "airlocked": True}])

    confirmed = api.protect(kind, item)

    assert confirmed["airlocked"] is True
    calls = api.request.call_args_list
    assert len(calls) == 3
    assert calls[1].args == (kind, EDIT_ROUTES[kind][0])
    assert calls[1].kwargs["payload"] == {
        EDIT_ROUTES[kind][1]: 7,
        "name": item["name"],
        "tags": item["tags"],
        "alternative_hashes": item["alternative_hashes"],
        "airlocked": True,
    }
    assert calls[2].kwargs["params"]["bypass_cache"] == "true"


@pytest.mark.parametrize("field", ["tags", "alternative_hashes", "airlocked", "name"])
def test_missing_metadata_never_causes_a_destructive_partial_put(item, field):
    item.pop(field)
    api = AirLockAPI("test-token")
    api.request = MagicMock(return_value=item)
    with pytest.raises(AirLockError):
        api.protect("torrents", item)
    assert api.request.call_count == 1


def test_success_response_without_confirmed_flag_is_not_reported_as_protected(item):
    api = AirLockAPI("test-token")
    api.request = MagicMock(side_effect=[item, None, item])
    with pytest.raises(AirLockError, match="could not be confirmed"):
        api.protect("torrents", item)


def test_expired_source_is_not_protected_or_redownloaded(item):
    item["download_present"] = False
    with pytest.raises(AirLockError, match="not complete and present"):
        airlock_payload("torrents", item)


def test_already_protected_item_does_not_consume_an_edit(item):
    item["airlocked"] = True
    api = AirLockAPI("test-token")
    api.request = MagicMock(return_value=item)
    api.protect("torrents", item)
    assert api.request.call_count == 1


def test_changed_metadata_is_reported_without_any_rollback_or_unlock(item):
    api = AirLockAPI("test-token")
    api.request = MagicMock(side_effect=[item, None, {**item, "airlocked": True, "tags": []}])
    with pytest.raises(AirLockError, match="Metadata changed"):
        api.protect("torrents", item)
    assert api.request.call_count == 3
    assert api.request.call_args_list[1].kwargs["payload"]["airlocked"] is True


def test_pagination_visits_books_beyond_the_first_page():
    api = AirLockAPI("test-token")
    api.request = MagicMock(side_effect=[[{"id": i} for i in range(100)], [{"id": 100}]])
    assert len(api.items("torrents")) == 101
    assert api.request.call_args_list[1].kwargs["params"]["offset"] == 100


def test_repeated_page_fails_closed_instead_of_looping():
    api = AirLockAPI("test-token")
    api.request = MagicMock(return_value=[{"id": i} for i in range(100)])
    with pytest.raises(AirLockError, match="pagination repeated"):
        api.items("torrents")


def test_paths_support_single_file_and_nested_webdav_without_fuzzy_matching(item):
    item["files"] = [{"id": 0, "name": "chapter.m4b", "size": 5}]
    assert [path for path, _info in file_paths(item)] == ["Original Release/chapter.m4b"]
    item["files"] = [{"name": "../outside/chapter.m4b"}]
    assert file_paths(item) == []
    assert AUDIO_SUFFIXES == {f".{fmt}" for fmt in AUDIOBOOK_FORMATS}


def test_read_only_plan_writes_nothing_and_ignores_unreferenced_account_books(settings, item):
    link = add_link(settings)
    unrelated = {**item, "id": 8, "name": "Other", "files": [{"name": "Other/book.m4b"}]}
    api = mock_api(item, [unrelated])

    report = maintain(settings, api, apply=False)

    assert report["pending_protection"] == ["torrents/7"]
    api.protect.assert_not_called()
    assert not settings.state_dir.exists()
    assert link.is_symlink()
    assert link.readlink() == settings.mount / "Original Release/chapter.m4b"


def test_existing_active_symlink_is_protected_without_changing_library_paths(settings, item):
    link = add_link(settings)
    api = mock_api(item)
    report = maintain(settings, api, apply=True, protect_only=True)
    api.protect.assert_called_once_with("torrents", item)
    assert report["protected"] == ["torrents/7"]
    assert link.readlink() == settings.mount / "Original Release/chapter.m4b"
    state = json.loads((settings.state_dir / "archive-state.json").read_text())
    assert state["origins"][link.name]["torbox_path"] == "Original Release/chapter.m4b"


def test_ambiguous_matching_preserves_everything_and_locks_neither_candidate(settings, item):
    link = add_link(settings)
    api = mock_api(item, [{**item, "id": 8}])
    report = maintain(settings, api, apply=True, protect_only=True)
    assert report["unmatched_links"] == [link.name]
    api.protect.assert_not_called()
    assert link.is_symlink()


def test_quota_failure_keeps_playback_and_never_claims_protection(settings, item):
    link = add_link(settings)
    api = mock_api(item)
    api.protect.side_effect = AirLockError("No AirLock space available")
    report = maintain(settings, api, apply=True, protect_only=True)
    assert not report["protected"]
    assert report["pending_protection"] == ["torrents/7"]
    assert link.readlink() == settings.mount / "Original Release/chapter.m4b"
    assert "No AirLock space" in report["errors"][0]


def test_existing_vault_links_are_reported_without_guessing_a_replacement(settings, item):
    link = settings.library / "Library Book.m4b"
    link.symlink_to(settings.vault_mount / "Library Book.m4b")
    report = maintain(settings, mock_api(item), apply=True, protect_only=True)
    assert report["vault_links_need_review"] == [link.name]
    assert not report["protected"]
    assert link.readlink() == settings.vault_mount / "Library Book.m4b"


class FakeRclone:
    def __init__(self):
        self.objects = {"torbox-audiobooks:Audiobooks/Original Release/chapter.m4b": b"audio"}
        self.copies = []
        self.reads = []

    def stat(self, path):
        if path not in self.objects:
            return None
        return {
            "Size": len(self.objects[path]),
            "ID": path,
            "ModTime": "fixed",
            "IsDir": False,
            "Hashes": {"MD5": hashlib.md5(self.objects[path]).hexdigest()},
        }

    def run(self, *args):
        assert args[0] == "copyto"
        assert "--immutable" in args and "--ignore-existing" in args
        self.copies.append(args)
        self.objects.setdefault(args[2], self.objects[args[1]])

    def digest(self, path):
        self.reads.append(path)
        content = self.objects[path]
        return hashlib.sha256(content).hexdigest(), len(content)


def test_backup_streams_between_remotes_verifies_once_and_never_relinks(settings, item):
    link = add_link(settings)
    entry = {"library_path": link.name, "torbox_path": "Original Release/chapter.m4b"}
    rclone = FakeRclone()
    state = {"origins": {}, "backups": {}}

    assert (
        backup(
            entry,
            "torrents",
            item,
            item["files"][0],
            settings,
            state,
            rclone,
            settings.source_remote,
        )
        == "verified"
    )
    assert len(rclone.copies) == 1
    assert len(rclone.reads) == 2
    assert rclone.copies[0][1].startswith("torbox-audiobooks:")
    assert rclone.copies[0][2].startswith("gdrive:")
    assert link.readlink() == settings.mount / entry["torbox_path"]
    assert (
        backup(
            entry,
            "torrents",
            item,
            item["files"][0],
            settings,
            state,
            rclone,
            settings.source_remote,
        )
        == "unchanged"
    )
    assert len(rclone.reads) == 2


def test_changed_vault_content_is_verified_and_preserved(settings, item):
    entry = {"library_path": "Book.m4b", "torbox_path": "Original Release/chapter.m4b"}
    rclone = FakeRclone()
    state = {"backups": {}}
    backup(
        entry, "torrents", item, item["files"][0], settings, state, rclone, settings.source_remote
    )
    previous = copy.deepcopy(state)
    rclone.objects["gdrive:Shelfmark-TorBox-Archive/Book.m4b"] = b"wrong"
    with pytest.raises(RcloneError, match="verification failed"):
        backup(
            entry,
            "torrents",
            item,
            item["files"][0],
            settings,
            state,
            rclone,
            settings.source_remote,
        )
    assert state == previous
    assert rclone.objects["gdrive:Shelfmark-TorBox-Archive/Book.m4b"] == b"wrong"
    assert len(rclone.copies) == 1


@pytest.mark.parametrize("remote", ["/tmp/local", ":local:/tmp", "bad remote:path"])
def test_local_payload_destinations_are_rejected(remote):
    with pytest.raises(ValueError, match="named rclone remote"):
        remote_path(remote, "book.m4b")


def test_real_verification_pipe_hashes_bounded_chunks_without_a_payload_file(settings, monkeypatch):
    rclone = Rclone(settings)
    monkeypatch.setattr(
        rclone,
        "command",
        lambda *_args: [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 2000000)",
        ],
    )
    digest, size = rclone.digest("test:book")
    assert size == 2000000
    assert digest == hashlib.sha256(b"x" * size).hexdigest()
    assert not settings.state_dir.exists()


@pytest.fixture
def native_client(monkeypatch, settings):
    values = {
        "TORBOX_API_KEY": "test-token",
        "TORBOX_MOUNT_PATH": str(settings.mount),
        "TORBOX_AUTO_AIRLOCK": True,
    }
    monkeypatch.setattr(
        "shelfmark.download.clients.torbox.config.get",
        lambda key, default=None: values.get(key, default),
    )
    return TorboxClient()


def test_native_ready_audiobook_is_protected_once_without_http_download(
    monkeypatch, settings, item, native_client
):
    directory = settings.mount / item["name"]
    directory.mkdir()
    (directory / "chapter.m4b").write_bytes(b"audio")
    protect = MagicMock(return_value={**item, "airlocked": True})
    monkeypatch.setattr(AirLockAPI, "protect", protect)
    http = MagicMock()
    monkeypatch.setattr("shelfmark.download.clients.torbox.download_url", http)
    state = _DownloadState("7", item["name"], directory, audiobook=True)
    assert native_client._handle_torrent(item, state).state == DownloadState.COMPLETE
    native_client._handle_torrent(item, state)
    protect.assert_called_once()
    http.assert_not_called()


def test_native_airlock_failure_preserves_ready_playback_and_reports_pending(
    monkeypatch, settings, item, native_client
):
    directory = settings.mount / item["name"]
    directory.mkdir()
    (directory / "chapter.m4b").write_bytes(b"audio")
    monkeypatch.setattr(AirLockAPI, "protect", MagicMock(side_effect=AirLockError("quota")))
    state = _DownloadState("7", item["name"], directory, audiobook=True)
    result = native_client._handle_torrent(item, state)
    assert result.state == DownloadState.COMPLETE
    assert "AirLock pending" in result.message
    assert (directory / "chapter.m4b").read_bytes() == b"audio"


@pytest.mark.parametrize("delete_files", [False, True])
def test_streaming_cleanup_never_deletes_torbox_item(monkeypatch, native_client, delete_files):
    post = MagicMock()
    monkeypatch.setattr("shelfmark.download.clients.torbox.requests.post", post)
    assert native_client.remove("7", delete_files=delete_files)
    post.assert_not_called()


def test_import_still_creates_the_exact_torbox_symlink_with_no_copy(settings):
    source = settings.mount / "Book.m4b"
    source.write_bytes(b"audio")
    destination = settings.library / "Book.m4b"
    _create_symlink_no_fallback(source, destination)
    assert destination.is_symlink()
    assert destination.readlink() == source
