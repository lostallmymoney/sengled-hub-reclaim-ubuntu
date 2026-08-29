# Sengled Element Hub Reclaim (Ubuntu)

Reclaim a Sengled Element Hub into a **local Zigbee coordinator** you own, and
use it with Home Assistant (ZHA) — entirely from Ubuntu/Linux.

It reflashes the hub's on-board EM357 radio to **EZSP v7**, rebuilds the hub's
rootfs to run an `ezsp_gateway` bridge (TCP/6638 → `/dev/ttyS1` @ 57600) instead
of Sengled's cloud gateway, and rewrites the idle firmware bank so the hub boots
your reclaimed firmware. The other bank stays untouched as your recovery.

## One driver for either active bank

The hub is dual-bank; the bootloader boots the bank with the higher kernel mark
(stock `0xFFFFFFF0`, reclaimed `0xFFFFFFF1`). Whichever bank is **running** is
treated as protected and never written; the **idle** bank is reclaimed.

```
python3 runReclaim.py --hub <hub-ip>   # detects the active bank and does the rest
```

| active bank | reclaims (idle) | partitions |
|-------------|-----------------|------------|
| Bank1       | Bank2           | rootfs mtd3, kernel mtd2 (written LAST) |
| Bank2       | Bank1           | rootfs mtd1, kernel mtd0 (written LAST) |

Both paths share one safety model: rootfs first, kernel LAST; every write is
byte-verified by the on-hub flasher; the flasher refuses unless the running-bank
gate matches; the running bank is never written.

## Requirements

- Python 3.12+ (developed against 3.14).
- `squashfs-tools-ng` (`sqfs2tar`, `tar2sqfs`) — only needed for the
  Bank1-active flow, which rebuilds images from a live backup.
- The hub must be able to reach this PC (the scripts run a local TFTP server).
- Do **not** unplug the hub during a flash stage.

The Bank2-active flow uses prebuilt reclaim images under `build/` (regenerated
from a device dump with `scripts/build_mirror_reclaimed.py`; not tracked in
git), so it needs nothing extra.

## Usage

```bash
# Safest first: show the plan (read-only, writes nothing):
python3 runReclaim.py --hub 10.42.0.119 --plan

# Read-only preflight (layout + ports):
python3 runReclaim.py --hub 10.42.0.119 --dryrun

# Network sanity:
./scripts/preflight-network.sh

# Full reclaim (rootfs first, kernel LAST, each byte-verified):
python3 runReclaim.py --hub 10.42.0.119
```

| flag | meaning |
|------|---------|
| `--plan` | print the reclaim plan; **no writes** |
| `--dryrun` | preflight validation; **no writes** |
| `--skip-coordinator` | skip the EM357/coordinator reflash |
| `--force-coordinator` | force the coordinator reflash even if already v7 |
| `--no-reboot` | flash and verify, but do not reboot into the reclaimed bank |

## Before you reclaim on a NEW hub

On the hub this repo was developed against (10.42.0.119), Bank2 was the active
bank, so the driver used the mirror flow and the prebuilt images under `build/`
(Bank2-active branch). On a hub where Bank1 is active, the standard flow
rebuilds the images from a live backup and needs `squashfs-tools-ng`. See
`docs/RESEARCH.md` for how the flash layout and both flows were established.

## After the reclaim

The hub exposes an EZSP coordinator at:

```
socket://<hub-ip>:6638
```

Run Home Assistant (Docker), then add the **ZHA** integration with radio type
**EZSP** and that path. Setup scripts live in `~/Documents/homeassistant/`
(not in this repo):

| script | what |
|--------|------|
| `installdockerhomeassistant-host.sh` | HA container, host networking (recommended) |
| `installdockerhomeassistant-bridge.sh` | HA container, bridge `-p 8123:8123` |
| `purge-pip-homeassistant.sh` | remove a previous HA Core (pip) install + deps |

The dashboard opens at `http://localhost:8123` (launcher on the desktop).

## Layout

```
README.md                  this file
docs/                      all documentation (hardware research, reclaim
                           design, build notes — see docs/README.md)
reclaimhub/                Python package (hub shell, TFTP, images, controller)
runReclaim.py              unified bank-agnostic driver (start here)
scripts/preflight-network.sh    port/ping pre-flight
scripts/build_mirror_reclaimed.py  rebuild the prebuilt mirror images
payload/                   vendored on-hub MIPS binaries + PROVENANCE manifest
source/                    MIPS C/assembly flasher sources + linker script
build/                     regenerable device-derived images (untracked)
```

This repo is self-contained: all on-hub payload binaries are vendored under
`payload/` (see `docs/README.md` / `payload/PROVENANCE.md`) and nothing outside
the repo is needed at runtime.