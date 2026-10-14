# Updating the inspected Shelfmark services

This server-specific updater preserves the configuration of `shelfmark-ebooks`
and `shelfmark-torbox`. It is not a portable installer. It does not operate on
the acquisition bridge, Audiobookshelf, rclone mount services, Plex or arrs.

## Prepared versions

| Service | Target image | Customization |
| --- | --- | --- |
| Shelfmark eBooks | `ghcr.io/calibrain/shelfmark:v1.4.0` | Retains the exact-job InfiniDysk cleanup in a version-matched mounted handler, and existing custom scripts |
| Shelfmark TorBox | `ghcr.io/sawdoctor/shelfmark-torbox:0.1.0-torbox.1` | Uses the already published standard image with the TorBox symlink and AirLock changes |

The inspected eBook handler SHA-256 is
`6b38603b8b2fb57964050c9d1a4385eb20d8120843709bee1714cd00c4bede5f`.
The v1.4.0 adapted handler is pinned to eBook repository commit
`7b47e02d415936850461238e13ec4f9d4e2cce5b` and SHA-256
`ae0c9ac4b380bd64d3140a998594c650df188bf6bb805736a83cb0e6f6d6bf22`.
Its successful-import cleanup function matches the legacy customization;
the rest follows official v1.4.0. The upstream SABnzbd client is unchanged
between v1.3.15 and v1.4.0. No eBook image rebuild is required.

## Operation

Use `scripts/update_shelfmark_services.py` without arguments to prepare only,
or with `--apply` to perform the explicitly authorized scoped update.
The updater refuses an unmatched handler, different Compose locations/projects,
configuration drift in mounts/environment/ports/networks, or broader lifecycle
hooks. It confirms that only the selected image field changes in the resolved
Compose configuration. It retains each actual server Compose file, `.env`,
mounted custom scripts and handler. Both image downloads complete before any
service is stopped. No image builds are requested.

Each selected service is stopped individually to copy its application settings
and databases, then recreated using its existing Compose project with
`--no-deps --no-build --pull never`. The eBook handler is upgraded while that
service is stopped. The updater waits for container health and verifies the
expected image, mounts, configured environment, ports and networks. An eBook
handler checksum is also checked inside the new container. Changes are written
into the real Compose files, so later Dockhand redeployments retain the pins.

Private recovery directories are created below
`/home/sawdoctor/torbox-recovery-20261011/shelfmark-update-*`.
Read `update-state.json` and `progress.log` there if the SSH phone application
closes. The process ignores SSH hangup and records progress privately; do not
start concurrent updates. A lock rejects a second updater while one runs.

On a failure after stopping a selected service, the updater restores only that
service's original Compose/handler and attempts to start its original image.
It records whether availability was verified. An earlier successfully updated
service is retained. Old application databases are never automatically restored
over newer state. Original images and private rollback copies remain available.
No book files are copied into backups, moved, deleted or unprotected. Temporary
candidate Compose files are removed; the command wrapper should remove its
exact temporary downloaded updater after execution. Keep recovery directories.

## Validation and deployment status

Preparation checks covered the exact cleanup preservation, Python syntax,
selected-image edits, a successful two-service update simulation, and a
second-service failure/rollback simulation. Simulations are not live server
verification. As of preparation on 14 October 2026, the live server reported
v1.3.15 eBooks with the matched mounted handler and local `shelfmark-torbox:test`.
The authorized update subsequently completed on 14 October 2026. The server
reported both selected services healthy after image and runtime-preservation
checks, including the adapted eBook handler checksum inside its container.
Private recovery material is retained at
`/home/sawdoctor/torbox-recovery-20261011/shelfmark-update-20261014T203659Z-catec3zh`.
The real Compose pins were updated, both application configuration/database
backups and old images were retained, and the wrapper removed its downloaded
temporary updater. No bridge, mount service, Audiobookshelf, Plex or arr
containers were operated on. No book payloads were copied or deleted.
This confirms deployment and container health; a new end-to-end acquisition
was not requested or run as part of this operator update. No additional application release is required for these
operator scripts: the target TorBox application image is already released.
