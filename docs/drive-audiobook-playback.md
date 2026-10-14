# Legacy Drive audiobook playback maintenance

TorBox audiobooks use TorBox symlinks and a backup-only Drive vault. A pre-existing
Drive library remains on its original source; these helpers do not migrate it
onto TorBox or delete it.

## Preserving the original library

`scripts/repair_legacy_audiobook_view.py` repairs the inspected legacy metadata
view using real author/book directories and per-file symlinks. This avoids
Audiobookshelf canonicalizing author-directory links outside the library root.

The helper validates the original source/current records, saves rollback links,
a manifest and a current SQLite backup, and preserves library paths and audio
bytes. It writes no database records or audio payloads and removes no remote
objects. Default mode is read-only; explicit `--apply` performs the guarded repair.
This targets the existing inspected deployment, not an arbitrary new server.
A normal library scan can clear old missing flags after the repair.

## Five minutes versus one hour

`--dir-cache-time` controls the lifetime of directory metadata held in memory.
Names, sizes and other file information can be needed before playback begins.
This is separate from audio VFS caching, buffering and read-ahead.

At `5m`, a listing can expire after five minutes and require another remote lookup.
At `1h`, unchanged listings can be reused for up to an hour before an expiry-driven
refresh. Polling can invalidate changed entries sooner. Expiry does not remove
the setting, audiobook or backup: the next needed lookup refreshes the listing
and starts another cache lifetime.

The policy is saved at
`/etc/systemd/system/rclone-gdrive-audiobooks.service.d/zz-shelfmark-metadata-cache.conf`.
It persists across normal service restarts/reboots. In-memory listings rebuild
after a restart. Only changing/removing the override changes that saved policy;
merging this repository does not install or remove it on a live server.

A first lookup after restart or expiry can still depend on Drive/API latency.
This reduces repeated expiry-driven lookups; it is not a universal two-second
cold-start promise and does not control AirLock or TorBox retention.

## Guarded preparation and activation

`scripts/prepare_drive_metadata_cache.py` targets the existing inspected
`rclone-gdrive-audiobooks.service` and `audiobookshelf-test` deployment.
Preparation runs as the existing server user, validates unit command/consumer
scope/database location, and saves private unit/container rollback copies,
a current database backup, the single-setting proposal and a plan. It changes
no installed configuration or running service.

Explicit `--activate STAGED_DIRECTORY`, using sudo, installs only `5m` to `1h`,
stops the audiobook container, renews its private Drive FUSE bind and starts
Audiobookshelf. Waiting uses the actual finite unit stop/start allowances with
progress updates. The image, environment values, bind specs, mounts and other
runtime settings remain guarded. Equivalent replacement IDs/generated hostnames
and ordering do not cause false failures; current labels are retained.

`--retry` requires preserved failed-attempt evidence and verified restored live
configuration. It retains previous copies and makes fresh private backups.
Readiness checks current host/container FUSE binding and retained book IDs/paths
and progress records. Rollback retains the current database instead of replacing
new listening progress with an older backup.

If the client disconnects, read the latest private `activation-state.json` and
`activation-result.json` and current service state before retrying. A disconnect
alone does not establish success or failure. Automatic rollback failures remain
explicit. Keep recovery material and live configuration; only disposable helper
downloads are cleanup candidates.

## Recorded result

On 14 October 2026, the measured metadata lookup took 24.32 seconds, while the
beginning/end audio reads took about one second each. The inspected lifetime was
five minutes. After the backed-up one-hour change, the unit/container were running,
the journal recorded `activation-complete`, and the user reported approximately
two seconds to first audio for *A Tale of Two Cities*.

The helper accessed that book's metadata before playback. This verifies the
attempt, not every untouched book. Existing audio-cache flags, polling,
credentials, audio bytes and library names were retained. The helper issued
no Plex or arrs service operation.
