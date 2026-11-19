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
- `squashfs-tools-ng` (`sqfs2tar`, `tar2sqfs`) — needed for both flows. The
  Bank1-active flow rebuilds images from a live backup during the run; the
  Bank2-active flow needs them to build its images beforehand (see below).
- The hub must be able to reach this PC (the scripts run a local TFTP server).
- Do **not** unplug the hub during a flash stage.

The Bank2-active flow flashes prebuilt reclaim images from
`build/reclaim-hub-20260829/`. They are **not** tracked in git: you build them
from a flash dump of your own hub before running the reclaim (see
[Bank2-active hubs](#bank2-active-hubs-build-the-bank1-images-first)).

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

Run `--plan` first: it prints which bank is active.

### Bank2-active hubs: build the Bank1 images first

The mirror flow aborts with `missing prebuilt mirror image` until
`build/reclaim-hub-20260829/mtd{0,1}-bank1-*-reclaimed.bin` exist. Build them
from **your own hub's** flash dump: the reclaimed kernel image keeps the
header prefix of that hub's `mtd0`, so images built from another hub's dump
are not interchangeable.

```bash
# 1. Read-only dump of all four partitions (works on either active bank;
#    writes output/<hub-ip>-<timestamp>/backup/):
python3 -m reclaimhub.controller --hub <hub-ip> --dump-only

# 2. Optional: dump a second time and compare. The hub has no md5sum/sha256sum,
#    so two matching PC-side reads are the integrity check:
diff output/<first-run>/backup/BACKUP-SHA256.txt \
     output/<second-run>/backup/BACKUP-SHA256.txt

# 3. Build the Bank1 images into the path runReclaim.py reads:
python3 scripts/build_mirror_reclaimed.py \
    --backup output/<first-run>/backup --out build/reclaim-hub-20260829

# 4. Reclaim as usual:
python3 runReclaim.py --hub <hub-ip>
```

Use `--dump-only`, not `--backup-only`: `--backup-only` keeps the stock
installer's Bank1-active check and refuses a Bank2-active hub.

### Network setup notes

Observed on one hub; your hub may differ.

- A hub that was set up on Wi-Fi in the Sengled app kept its Ethernet port
  dead (link LED on for about 2 s at boot, then off) until it was factory
  reset. After the reset, Ethernet worked.
- The MAC on the label is the Wi-Fi interface (`wlan0`). The Ethernet
  interface (`eth0`, and the `br0` bridge) is the label MAC **plus one**. Use
  that one for a DHCP reservation when the hub is wired.
- In the reclaimed firmware, `br0` bridges `eth0` and `wlan0`.

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