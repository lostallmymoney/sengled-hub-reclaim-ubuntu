#!/usr/bin/env python3
"""
runReclaim.py - unified, bank-agnostic reclaim driver for the Sengled Element Hub.

This is the single entry point a human runs, regardless of which firmware
bank the hub currently boots. It connects to the hub, detects the ACTIVE
(protected / running) bank, and dispatches to the reclaim engine that targets
the IDLE (opposite) bank:

  active Bank1 (running mtd0/mtd1)  -> reclaim idle Bank2  (standard flow)
  active Bank2 (running mtd2/mtd3)  -> reclaim idle Bank1  (mirror flow)

The dual-bank bootloader boots the bank carrying the higher kernel mark; every
reclaimed bank is written with mark 0xFFFFFFF1 (> the stock 0xFFFFFFF0), so it
boots next. Every write is byte-for-byte verified by the on-hub MIPS flasher.

Safety model (identical to the originals):
  - We NEVER write the active (running) bank.
  - The on-hub flasher REFUSES to run unless the running bank gate matches.
  - Requires exactly one sentinel file; exact source sizes; block-device probe.
  - rootfs is written first, kernel LAST, each then byte-verified.

Usage:
  python3 runReclaim.py --hub 10.42.0.119            # full reclaim
  python3 runReclaim.py --hub 10.42.0.119 --plan      # detect+print plan, no writes
  python3 runReclaim.py --hub 10.42.0.119 --dryrun    # preflight, no writes
"""
import argparse
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reclaimhub.hubshell import HubShell, ShellError
from reclaimhub.controller import Controller, get_local_ip
from reclaimhub.tftpserver import TftpServer

PROJECT = os.path.abspath(os.path.dirname(__file__))
PAYLOAD = os.path.join(PROJECT, "payload")
BUILD = os.path.join(PROJECT, "build", "reclaim-hub-20260829")

# partition mapping for each physical bank: kernel / rootfs mtdblock
BANK_PARTITIONS = {
    1: (0, 1),   # kernel mtdblock0, rootfs mtdblock1
    2: (2, 3),   # kernel mtdblock2, rootfs mtdblock3
}

# prebuilt image and partition sizes (mirror/Bank1 flow)
ROOTFS_SZ = 0x2D0000
KERNEL_SZ = 0x130000

# --- single source of truth for every reclaim sub-flow --------------------
# Each entry describes how to reclaim one physical bank. The driver selects a
# flow by the bank that is ACTIVE (running = protected) and reclaims the other
# (idle) one. All flows share one engine (_run_flow); only this table differs.
#
#   rootfs_first: rootfs is always written before kernel.
#   images: ("built", ...)   -> backup + rebuild on the fly (standard Bank2)
#           ("prebuilt", ...)-> use prebuilt blobs (mirror Bank1)
#   probe_needles: substrings that must all appear in the flasher's read-only
#                  probe output (which asserts the running-bank gate + layout).
FLOWS = {
    # active Bank1 -> reclaim idle Bank2 (standard flow)
    1: {
        "target": 2,
        "flasher_real": os.path.join(PAYLOAD, "bank2-safe-flash-v2-block"),
        "flasher_name": "bank2-safe-flash-v2-block",
        "rootfs_go": "/tmp/FLASH_BANK2_ROOTFS_NOW",
        "kernel_go": "/tmp/FLASH_BANK2_KERNEL_NOW",
        "rootfs_hub": "/tmp/mtd3-bank2-rootfs-reclaimed.bin",
        "kernel_hub": "/tmp/mtd2-bank2-kernel-reclaimed.bin",
        "images": ("built",),
        "probe_needles": ("ACTIVE BOOT BANK REPORTS: 1", "mtdblock2", "mtdblock3", "OK"),
        "recov_msg": "Recovery Bank1 was not modified.",
        "desc": "reclaiming idle Bank2 (mtd3 rootfs, mtd2 kernel LAST)",
    },
    # active Bank2 -> reclaim idle Bank1 (mirror flow)
    2: {
        "target": 1,
        "flasher_real": os.path.join(PAYLOAD, "mirror-flash-bank1-safe-v1"),
        "flasher_name": "mirror-flash-bank1-safe-v1",
        "rootfs_go": "/tmp/FLASH_BANK1_ROOTFS_NOW",
        "kernel_go": "/tmp/FLASH_BANK1_KERNEL_NOW",
        "rootfs_hub": "/tmp/mtd1-bank1-rootfs-reclaimed.bin",
        "kernel_hub": "/tmp/mtd0-bank1-kernel-reclaimed.bin",
        "images": ("prebuilt",
                   (os.path.join(BUILD, "mtd1-bank1-rootfs-reclaimed.bin"), ROOTFS_SZ),
                   (os.path.join(BUILD, "mtd0-bank1-kernel-reclaimed.bin"), KERNEL_SZ)),
        "probe_needles": ("ACTIVE BOOT BANK REPORTS: 2",
                          "/dev/mtdblock0 ... size=0x00130000 OK",
                          "/dev/mtdblock1 ... size=0x002D0000 OK",
                          "create EXACTLY ONE sentinel"),
        "recov_msg": "Recovery Bank2 (2020 firmware) was not modified.",
        "desc": "reclaiming idle Bank1 (mtd1 rootfs, mtd0 kernel LAST)",
    },
}


def fatal(msg):
    print("\n[ABORT] %s" % msg)
    sys.exit(2)


def get_boot_bank(ctl):
    """Return the active boot bank number (1 or 2)."""
    boot = ctl.run_cmd("cat /proc/bootbank", 15, quiet=True)
    m = re.search(r"(?m)^([12])\s*$", boot.output.strip())
    if not m:
        fatal("Could not determine a valid boot bank from /proc/bootbank: %r"
              % boot.output.strip())
    return int(m.group(1))


def validate_layout(ctl, active_bank):
    """Shared preflight: dual-bank on, expected MTD sizes, UART present."""
    dual = ctl.run_cmd("flash get DUALBANK_ENABLED", 15, quiet=True)
    if "DUALBANK_ENABLED=1" not in dual.output:
        fatal("DUALBANK_ENABLED is not 1: %s" % dual.output)
    mtd = ctl.run_cmd("cat /proc/mtd", 15, quiet=True)
    for needle in ("00130000", "002d0000", "00130000", "002d0000"):
        if needle not in mtd.output.lower():
            fatal("Unexpected MTD layout; missing '%s'" % needle)
    dev = ctl.run_cmd("ls -l /dev/mtdblock0 /dev/mtdblock1 /dev/mtdblock2 /dev/mtdblock3 /dev/ttyS1",
                      15, quiet=True)
    if dev.exit_code != 0:
        fatal("Required MTD/UART interfaces are missing")
    print("[OK] active bank %d; dual-bank enabled; expected RTL8196E layout present"
          % active_bank)


def open_controller(args):
    ctl = Controller(args.hub, args.tftp_port)
    ctl.run_dir = os.path.join(PROJECT, "output", "%s-reclaim-%s"
                               % (args.hub, time.strftime("%Y%m%d-%H%M%S", time.gmtime())))
    os.makedirs(ctl.run_dir, exist_ok=True)
    ctl.tftp_root = os.path.join(ctl.run_dir, "tftp")
    os.makedirs(ctl.tftp_root, exist_ok=True)
    return ctl


def main():
    ap = argparse.ArgumentParser(description="Sengled hub unified reclaim driver")
    ap.add_argument("--hub", required=True)
    ap.add_argument("--tftp-port", type=int, default=6969)
    ap.add_argument("--plan", action="store_true",
                    help="detect active bank and print the reclaim plan; no writes")
    ap.add_argument("--dryrun", action="store_true",
                    help="preflight validation only; no coordinator/flash writes")
    ap.add_argument("--skip-coordinator", action="store_true")
    ap.add_argument("--force-coordinator", action="store_true")
    ap.add_argument("--no-reboot", action="store_true")
    args = ap.parse_args()

    ctl = open_controller(args)

    # ------------------------------------------------------------------
    # Open a shell and detect the active bank (read-only until we choose).
    # ------------------------------------------------------------------
    try:
        print("\n=== opening hub shell ===")
        if not ctl.tcp_open(23, 2000):
            ctl.wait_tcp(8686, 60, "stock debug service")
            ctl.invoke_at(["AT+START_TELNETD=1"])
            time.sleep(2)
            for _ in range(15):
                if ctl.tcp_open(23, 2000):
                    break
                time.sleep(2)
            else:
                fatal("telnetd did not become reachable on %s:23" % args.hub)
        ctl.connect_shell()
        ctl.pc_ip = get_local_ip(args.hub)
        print("[OK] PC address visible to hub: %s" % ctl.pc_ip)

        active = get_boot_bank(ctl)
        try:
            flow = FLOWS[active]
        except KeyError:
            fatal("No reclaim flow defined for active bank %d" % active)
        target = flow["target"]
        flasher = flow["flasher_real"]
        k_mtd, r_mtd = BANK_PARTITIONS[target]

        print("\n=== plan ===")
        print("  ACTIVE bank : Bank %d (running, protected - NOT touched)" % active)
        print("  TARGET bank : Bank %d (idle - will be reclaimed)" % target)
        print("  target devs : kernel=/dev/mtdblock%d  rootfs=/dev/mtdblock%d" % (k_mtd, r_mtd))
        print("  flasher     : %s" % os.path.basename(flasher))
        if not os.path.isfile(flasher):
            fatal("flasher payload missing: %s" % flasher)

        if args.plan:
            print("\n[OK] PLAN ONLY. Active bank discovered; no writes performed.")
            return

        validate_layout(ctl, active)

        if args.dryrun:
            print("\n[OK] DRY RUN: hub reachable, active Bank %d, layout valid. "
                  "No coordinator or flash writes performed." % active)
            return

        # ------------------------------------------------------------------
        # Dispatch to the single parameterized engine, which reclaims the
        # idle bank (the flow is chosen above by the active bank).
        # ------------------------------------------------------------------
        _run_flow(ctl, args, flow, active)
    finally:
        if ctl.shell:
            ctl.shell.close()
        if ctl.tftp:
            ctl.tftp.stop()


# ----------------------------------------------------------------------
# Single parameterized reclaim engine (replaces the old Bank2 standard and
# Bank1 mirror functions). 'flow' describes the bank to reclaim; 'active'
# is the running (protected) bank we must never touch.
# ----------------------------------------------------------------------
def _run_flow(ctl, args, flow, active):
    target = flow["target"]

    print("\n=== %s ===" % flow["desc"])

    images = _prepare_images(ctl, flow)

    DO_COORD = os.environ.get("RECLAIM_COORD", "auto")
    if DO_COORD == "skip":
        print("[!] coordinator stage skipped (RECLAIM_COORD=skip)")
    else:
        already_v7 = ctl.probe_coordinator()
        if already_v7 and DO_COORD != "force":
            ctl.step_pad("coordinator already speaks EZSP v7; destructive reflash skipped")
        else:
            ctl.flash_coordinator()

    exe = ctl.install_executable(flow["flasher_real"], flow["flasher_name"])
    ctl.run_cmd("rm -f %s %s" % (flow["rootfs_go"], flow["kernel_go"]),
                10, allow_failure=True, quiet=True)

    probe = ctl.run_cmd(exe, 30, allow_failure=True, quiet=True)
    print(probe.output)
    for needle in flow["probe_needles"]:
        if needle not in probe.output:
            fatal("Flasher read-only probe did not match the proven layout "
                  "(missing %r):\n%s" % (needle, probe.output))

    rootfs_img, kernel_img = images
    _write_and_verify(ctl, exe, rootfs_img, flow["rootfs_hub"], flow["rootfs_go"],
                      "ROOTFS", target, 420)
    _write_and_verify(ctl, exe, kernel_img, flow["kernel_hub"], flow["kernel_go"],
                      "KERNEL", target, 300)

    # confirm the running bank was never written
    bank = ctl.run_cmd("cat /proc/bootbank", 10, quiet=True)
    if not re.match(r"(?m)^%d\s*$" % active, bank.output.strip()):
        fatal("Active bank no longer %d after flash; refusing to reboot: %r"
              % (active, bank.output.strip()))
    print("[OK] Bank %d (running) still active. Bank %d rewritten + verified. "
          "Next boot will try reclaimed Bank %d (mark 0xFFFFFFF1)."
          % (active, target, target))

    if args.no_reboot:
        print("[!] --no-reboot: Bank %d still running. Reboot later manually." % active)
        return

    print("\n=== rebooting into reclaimed Bank %d ===" % target)
    ctl.shell.send_raw_line("reboot")
    ctl.shell.close(); ctl.shell = None
    time.sleep(4)
    _connect_after_reboot(ctl)
    _health_check(ctl, target)
    print("\nRECLAIM COMPLETE")
    print("Home Assistant ZHA radio URL: socket://%s:6638" % args.hub)
    print(flow["recov_msg"])


def _prepare_images(ctl, flow):
    """Return (rootfs_img, kernel_img) for the flow's image source."""
    kind = flow["images"][0]
    if kind == "built":
        backup_dir = os.path.join(ctl.run_dir, "backup")
        build_dir = os.path.join(ctl.run_dir, "build")
        ctl.backup_rtl_flash(backup_dir)
        imgs = ctl.build_images(backup_dir, build_dir)
        return imgs["rootfs"], imgs["kernel"]
    if kind == "prebuilt":
        imgs = []
        for rel, sz in flow["images"][1:]:
            path = os.path.join(BUILD, rel) if not os.path.isabs(rel) else rel
            if not os.path.isfile(path):
                fatal("missing prebuilt mirror image: %s "
                      "(run scripts/build_mirror_reclaimed.py first)" % path)
            if os.path.getsize(path) != sz:
                fatal("%s is %d bytes; expected %d" % (path, os.path.getsize(path), sz))
            imgs.append(path)
        return imgs[0], imgs[1]
    fatal("unknown image source: %r" % kind)


def _write_and_verify(ctl, exe, img, hub_path, go_path, what, target, timeout):
    print("\n=== writing + verifying BANK%d %s (/dev/mtdblock%d) ==="
          % (target, what, BANK_PARTITIONS[target][0 if what == "KERNEL" else 1]))
    ctl.send_file(img, hub_path, os.path.basename(img))
    ctl.run_cmd("echo 1 > %s" % go_path, 10, quiet=True)
    print("[WAIT] %s being written + byte-verified. Keep hub powered." % what)
    try:
        rr = ctl.run_cmd(exe, timeout, allow_failure=True, quiet=True)
        print(rr.output)
        if rr.exit_code != 0 or "VERIFY: PASS" not in rr.output:
            fatal("%s flash/verify failed (rc=%s)" % (what, rr.exit_code))
    finally:
        ctl.run_cmd("rm -f %s" % go_path, 10, allow_failure=True, quiet=True)
    ctl.run_cmd("rm -f %s" % hub_path, 15, allow_failure=True, quiet=True)


# ----------------------------------------------------------------------
# shared post-reboot helpers
# ----------------------------------------------------------------------
def _connect_after_reboot(ctl):
    last = None
    sh = None
    for attempt in range(30):
        try:
            sh = HubShell(ctl.hub, 23)
            sh.connect(5000)
            time.sleep(0.75)
            sh.send_raw_line("")
            time.sleep(0.35)
            probe = sh.run("echo __SENGLED_SHELL_READY__", 7000)
            if probe.exit_code == 0 and "__SENGLED_SHELL_READY__" in probe.output:
                ctl.shell = sh
                return sh
            sh.close()
            sh = None
        except Exception as e:
            last = e
            try:
                if sh is not None:
                    sh.close()
                    sh = None
            except Exception:
                pass
        time.sleep(3)
    raise SystemExit("[ABORT] Hub shell did not become responsive after reboot: %s" % last)


def _health_check(ctl, expected_bank):
    bank = ctl.run_cmd("cat /proc/bootbank", 15, allow_failure=True, quiet=True)
    if bank.output.strip() != str(expected_bank):
        print("[!] Hub booted bank %r; expected reclaimed Bank %d. "
              "A cold power-cycle may be required on this bootloader."
              % (bank.output.strip(), expected_bank))
        return
    marker = ctl.run_cmd("test -f /etc/reclaim-build.txt && cat /etc/reclaim-build.txt",
                         15, allow_failure=True, quiet=True)
    if marker.exit_code != 0 or "Sengled reclaimed rootfs" not in marker.output:
        fatal("Bank %d active but reclaimed marker missing:\n%s" % (expected_bank, marker.output))
    print(marker.output)
    status = ctl.run_cmd("reclaim-status", 30, allow_failure=True, quiet=True)
    print(status.output)
    ctl.wait_tcp(6638, 30, "EZSP gateway")
    print("[OK] Bank %d reclaimed filesystem + TCP/6638 health checks passed." % expected_bank)


if __name__ == "__main__":
    main()
