# Research notes — Sengled hub reclaim (hardware, flash layout, verification)

> These are the working notes that led to `runReclaim.py`. They record
> what was proven about the hardware and how the mirror reclaim was designed
> and verified. Not a step-by-step guide — see `README.md` for usage.

## Flash layout (proven from full 8 MiB dump + live /etc/version)

| MTD | offset | size     | name            | content                               |
|-----|--------|----------|-----------------|---------------------------------------|
| mtd0 | 0x30000 | 0x130000 | boot+cfg+linux(bank1) | kernel, mark 0x80000002         |
| mtd1 | 0x130000 | 0x2D0000 | root fs(bank1)  | 2018 Realtek SDK r324 rootfs          |
| mtd2 | 0x430000 | 0x130000 | linux(bank2)    | kernel, mark 0xFFFFFFF0              |
| mtd3 | 0x530000 | 0x2D0000 | root fs(bank2)  | 2020 Realtek SDK r362 rootfs          |

- Live `/etc/version` = `RTL8196E v1.0 2020-07-16 ... SDK r362` == mtd3 rootfs
  `/etc/version` (byte match).
- mtd1 rootfs `/etc/version` = `2018-01 ... SDK r324` (old, idle).
- MTD sizes: 0x130000 (kernel) / 0x2D0000 (rootfs) per bank.
- Bootloader boots the bank with the **higher kernel mark** (stock `0xFFFFFFF0`
  for bank2, `0x80000002` for bank1). Reclaimed banks get mark `0xFFFFFFF1`.

## The recover approach

The reclaim tool was designed for the common case where **Bank1 is active**
(source/firmware running) and **Bank2 is idle** (target). It builds reclaimed
images from the live Bank1 (or uses prebuilt ones), writes them into the idle
Bank2 — rootfs first, kernel LAST, each byte-verified by the on-hub MIPS
flasher.

On this particular hub, `/proc/bootbank` was reported as `2` (Bank2 active,
2020 firmware running, Bank1 idle). The driver's `FLOWS` table in
`runReclaim.py` therefore includes a **mirror flow**: active Bank2 →
reclaim idle Bank1 (rootfs mtd1, kernel mtd0 LAST):

- source = running Bank2 kernel (mtd2, mark 0xFFFFFFF0) → kernel payload into
  mtd0 with mark flipped to 0xFFFFFFF1 so the bootloader boots it next.
- source rootfs = running Bank2 (mtd3) → patched/rebuild rootfs into mtd1.
- target prefix = mtd0 bootloader+cfg region, preserved.
- flasher = idle-bank `mirror-flash-bank1-safe-v1` (rootfs `/dev/mtdblock1`
  then kernel `/dev/mtdblock0`, gated on bootbank == 2).

## Safety model (identical for both flows)

- Never write the active bank.
- On-hub flasher refuses to run unless the running-bank gate matches.
- Requires exactly one sentinel file; exact source sizes; block-device probe;
  rootfs first, kernel LAST; byte-for-byte verify; sync.
- Reserved mark `>= 0xFFFFFFF1` on the target is refused (never overwrite a
  newer/reclaimed bank).

## Verification

- `ImageBuilder` logic (build_kernel / wrap_rootfs / verify) implements the
  Realtek image conventions: kernel header prefix preserved, mark gates
  (proven source `0xFFFFFFF0`, refuse `>= 0xFFFFFFF1`), SquashFS4/LZMA/block
  size checks, aligned padding, big-endian Realtek checksums, all-FF tail.
- Prebuilt reference images (regenerated at `build/` from a device dump via
  `scripts/build_mirror_reclaimed.py`; not tracked in git):
  - `build/reclaim-hub-20260829/mtd0-bank1-kernel-reclaimed.bin` (0x130000):
    prefix == mtd0, kernel-area == mtd2 except mark byte @0x3000b,
    mark = 0xFFFFFFF1.
  - `build/reclaim-hub-20260829/mtd1-bank1-rootfs-reclaimed.bin` (0x2D0000):
    SquashFS4 LZMA 131072; rcS patched (telnetd on, stock gateway → ezsp
    startup, TCP/80 DROP removed); ezsp_gateway / hub-chmodx / ezsp_start.sh /
    reclaim-status / reclaim-build.txt present; checksums OK; tail all 0xFF.
- SHA-256 for both images recorded in `build/reclaim-hub-20260829/RECLAIM-SHA256.txt`;
  the reclaim ran live on the hub and verified byte-for-byte.

## Flasher profile (reference for rebuilding)

ELF32 BE MIPS R3000 (mips1) o32 static noreorder, entry 0x400000/0x20000;
gcc/clang flags ≈ `-mips1 -EB -O2 -nostdlib -static -fno-common`
(see `docs/BUILD-NOTES.md`).