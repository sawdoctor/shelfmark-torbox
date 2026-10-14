# TorBox audiobook retention and backup-only vault

Audiobooks remain on TorBox. Audiobookshelf keeps the same library files and
absolute symlinks into `/mnt/torbox-audiobooks`. The Google Drive vault is an
independent backup; the archive job never substitutes it as a playback source.
There is no local audio download or staging directory in this maintenance job.

## AirLock support

TorBox's [AirLock documentation](https://support.torbox.app/en/articles/15417147-torbox-airlock)
states that AirLock prevents removal due to the normal 30-day inactivity policy.
Current included capacity: Essential 300 GB, Standard 500 GB, Pro 1 TB; Free has
none. Retaining a multi-file download protects the entire download, so its
whole size counts against available capacity. This is not a purchase or a plan
upgrade; the job uses only the account's existing allowance.

The [official API](https://api-docs.torbox.app/) and
[v9 changelog](https://feedback.torbox.app/en/changelog) document `airlocked` in
the list responses and the PUT edit endpoints:

| Download type | Edit route | ID field |
| --- | --- | --- |
| Torrent | `/torrents/edittorrent` | `torrent_id` |
| Usenet | `/usenet/editusenetdownload` | `usenet_download_id` |
| Web download | `/webdl/editwebdownload` | `webdl_id` |

Edits replace the item's editable metadata. The implementation reads the item
again immediately before the edit, sends its existing name, tags and alternative
hashes unchanged with `airlocked: true`, then reads again to confirm protection
and preserved metadata. Missing metadata, ambiguous matching, a missing source,
quota errors and API failures produce warnings rather than destructive fallbacks.
It never unlocks another download or deletes anything to free capacity.

AirLock does not restore expired files, and exceptional removals remain possible.
The independent vault remains necessary. WebDAV browsing alone is not reliable
retention protection; see TorBox's
[inactive-download guidance](https://support.torbox.app/en/articles/10333785-why-is-my-download-inactive).

## New and existing books

Shelfmark requests AirLock when a new audiobook torrent becomes ready. The
`Protect Audiobooks with AirLock` setting defaults on. Playback can still import
if protection fails; the completion status and log show that AirLock is pending.
Generic torrent cleanup only releases Shelfmark's tracking; it retains the
TorBox source even if the generic post-import action is configured to remove.

The host job runs on the existing ten-minute archive schedule. It reads the
library's audio symlinks and matches their exact TorBox mount paths to completed,
present items in the torrent, Usenet and web-download API inventories. It protects
only uniquely matched active library sources, not every audio or media item in
the account. It also retries protection after application/API failures. Already
protected items require no edit.

Existing links to `/mnt/gdrive-torbox-archive` are reported in
`vault_links_need_review`. They are not automatically guessed back to TorBox by
title or filename, since a wrong match could replace a book's audio. During live
migration, recover original targets from any existing archive manifest/backups.
If those are absent, confirm each TorBox candidate against the vault's actual
content before restoring a link at the same Audiobookshelf pathname. Preserve
unavailable or ambiguous books until their source can be recovered. Do not
re-add library items, delete records, rebuild the database or force a rescan.

## Backup behavior

The job uses `rclone copyto` between the original TorBox remote and
`gdrive:Shelfmark-TorBox-Archive`, preserving the library-relative destination
name. `--ignore-existing` and `--immutable` prevent overwriting an existing vault
object. A differing existing copy is reported and retained for review.

New or previously unverified backups are compared using complete SHA-256 streams
from both remotes with bounded memory. No audio is written to local files or
read through a mount's local VFS cache by this job. A small local metadata state
file records verified hashes, remote identities and original playback targets.
Later runs skip unchanged verified copies; a changed remote identity triggers
verification again. Two unverified backup attempts run per invocation by default;
attempts rotate so one bad book cannot block all others. Protection happens first
for every matched source, independently of this backup limit.

The job never rewrites a playback link, removes TorBox items, deletes vault
objects, or writes Audiobookshelf's database. Its default direct invocation is
a read-only inventory; `--apply` explicitly enables protection and backup writes.
`--protect-only` skips backup transfers. An exclusive lock prevents overlapping
apply runs. Pending protection, unmatched TorBox links and failures return a
nonzero exit code and remain visible in the systemd journal.

## Live installation layout

The table describes the host layout. The recorded installation verified the
backup-only job and AirLock for three available active sources on 10 October 2026.
A repository checkout or container update alone does not install these host units.
Inspect and save the current archive executable, unit files, configuration and
any existing manifest before replacing them. Do not install while the old
archiver is running, since it could still rewrite links. Stop scheduling that
old job during the swap; wait for an existing invocation to finish rather than
interrupting playback or file operations. Reuse the timer's existing identity.

| Repository source | Host location |
| --- | --- |
| `scripts/torbox_audiobook_archive.py` | `/usr/local/lib/torbox-audiobooks/scripts/torbox_audiobook_archive.py` |
| `shelfmark/torbox_airlock.py` | `/usr/local/lib/torbox-audiobooks/shelfmark/torbox_airlock.py` |
| `shelfmark/__init__.py` | `/usr/local/lib/torbox-audiobooks/shelfmark/__init__.py` |
| `deployment/retention/torbox-audiobook-archive` | `/usr/local/sbin/torbox-audiobook-archive` |
| `deployment/retention/torbox-audiobook-archive.service` | `/etc/systemd/system/torbox-audiobook-archive.service` |
| `deployment/retention/torbox-audiobook-archive.timer` | `/etc/systemd/system/torbox-audiobook-archive.timer` |
| Private copy of `archive.env.example`, if needed | `/etc/torbox-audiobook-archive.env` |

The host job needs system Python 3.10+, rclone and the existing user's access to
the library/configuration. It has no third-party Python dependencies. Native
Shelfmark changes use the application's usual Python 3.14 runtime.

The job reads `TORBOX_API_KEY` from the environment, an optional private file at
`TORBOX_API_KEY_FILE`, or the existing
`SHELFMARK_CONFIG_DIR/plugins/prowlarr_clients.json`. No token belongs in GitHub
or in command arguments. Existing rclone credentials stay in its current private
configuration; `RCLONE_CONFIG` may identify that file if it is nonstandard.

`TORBOX_SOURCE_REMOTE` must identify the exact source of the existing mount,
including its remote subfolder. Normally this is detected using `findmnt`.
If the mount uses a custom filesystem name, set it from that mount's current
rclone command. Do not guess `Audiobooks`, `All`, or a tag folder: a mismatch
could point to the wrong file. Other paths and the backup limit are configurable
in the provided environment example.

Deploying just the host job covers newly imported symlink books and existing
TorBox links without rebuilding or restarting Shelfmark or Audiobookshelf. The
native protection and cleanup fix can be included in a later approved application
update. Keep saved copies of the original scripts and units for reference, but
do not resume the old relinking archiver when rolling back this maintenance job.

## Existing Drive playback libraries

Pre-existing Drive books remain separate from the TorBox backup vault. See
[legacy Drive playback maintenance](drive-audiobook-playback.md) for the preserved
filesystem view and persistent one-hour directory metadata policy. Metadata
expiry does not remove audiobook files or undo AirLock protection.
