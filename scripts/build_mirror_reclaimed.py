#!/usr/bin/env python3
"""
build_mirror_reclaimed.py - build reclaimed Bank1 (idle) images for the
INVERTED hub 10.42.0.119 from a read-only flash backup.

On this hub:
  running/protected = Bank2 (mtd2 kernel mark 0xFFFFFFF0 + mtd3 rootfs 2020)
  idle/target        = Bank1 (mtd0 kernel + mtd1 rootfs)

We patch the RUNNING 2020 rootfs (mtd3) and write the result into mtd1,
and take the RUNNING 2020 kernel (mtd2) payload into mtd0, flipping the
mtd0 kernel mark to 0xFFFFFFF1 so the bootloader boots it next time.

The two output files are exactly the ones the mirror-flasher expects:
  /tmp/mtd1-bank1-rootfs-reclaimed.bin  (0x2D0000)
  /tmp/mtd0-bank1-kernel-reclaimed.bin  (0x130000)

Safety: never touches Bank2 (mtd2/mtd3) on disk. Build is fully local.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from reclaimhub.tarpatcher import patch as tar_patch
from reclaimhub import imagebuilder

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PAYLOAD = os.path.join(REPO_ROOT, "payload")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backup", required=True, help="dir containing mtd*.bin")
    ap.add_argument("--out", required=True, help="output build dir")
    args = ap.parse_args()

    BK = args.backup
    os.makedirs(args.out, exist_ok=True)

    # ---- inputs (inverted mapping for THIS hub) ----
    src_root = os.path.join(BK, "mtd3-bank2-rootfs.bin")   # running 2020 rootfs
    src_kernel = os.path.join(BK, "mtd2-bank2-kernel.bin") # running 2020 kernel
    tgt_kernel = os.path.join(BK, "mtd0-bank1-kernel.bin") # idle target slot (prefix)

    stock_tar = os.path.join(args.out, "rootfs-stock.tar")
    patched_tar = os.path.join(args.out, "rootfs-reclaimed.tar")
    raw_sqfs = os.path.join(args.out, "rootfs-reclaimed.raw.sqfs")
    out_root = os.path.join(args.out, "mtd1-bank1-rootfs-reclaimed.bin")
    out_kernel = os.path.join(args.out, "mtd0-bank1-kernel-reclaimed.bin")

    for p in (src_root, src_kernel, tgt_kernel):
        if not os.path.exists(p):
            raise SystemExit("missing input: %s" % p)

    print("[build] source rootfs (running 2020): %s" % src_root)
    print("[build] source kernel  (running 2020): %s" % src_kernel)
    print("[build] target prefix  (idle mtd0)   : %s" % tgt_kernel)

    # ---- SquashFS -> tar (metadata-preserving) ----
    print("[build] sqfs2tar: SquashFS -> tar")
    with open(stock_tar, "wb") as fh:
        subprocess.run(["sqfs2tar", "-r", ".", "-X", src_root],
                       stdout=fh, check=True,
                       stderr=open(os.path.join(args.out, "sqfs2tar.log"), "w"))
    if os.path.getsize(stock_tar) < 1024:
        raise SystemExit("tar conversion produced unexpectedly small archive")

    # ---- patch + embed payloads ----
    build_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    build_text = ("Sengled reclaimed rootfs (mirror, Bank1)\nBuilt: %s\n"
                  "Gateway: EZSP TCP/6638 -> /dev/ttyS1 @ 57600\n" % build_time)
    status_text = '''#!/bin/sh
echo "=== Sengled reclaimed hub (mirror Bank1) ==="
cat /etc/reclaim-build.txt 2>/dev/null
echo "--- boot bank ---"
cat /proc/bootbank 2>/dev/null
echo "--- cmdline ---"
cat /proc/cmdline
echo "--- gateway process ---"
ps | grep '[e]zsp_gateway'
echo "--- Sengled processes (should be none) ---"
ps | grep '[s]engled'
echo "--- listeners ---"
if grep ':19EE ' /proc/net/tcp 2>/dev/null; then
    echo "TCP/6638 listener: present"
else
    echo "TCP/6638 listener: absent"
fi
'''
    tar_patch(stock_tar, patched_tar,
              os.path.join(PAYLOAD, "ezsp_gateway-v3"),
              os.path.join(PAYLOAD, "hub-chmodx-v1"),
              os.path.join(PAYLOAD, "ezsp_start.sh"),
              build_text, status_text)
    print("[OK] Patched rootfs archive without extracting device nodes")

    # ---- rebuild SquashFS (LZMA block 131072, matching stock) ----
    print("[WAIT] Rebuilding SquashFS (LZMA) - can take a few minutes...")
    with open(patched_tar, "rb") as fin, \
            open(os.path.join(args.out, "tar2sqfs-output.log"), "w") as so, \
            open(os.path.join(args.out, "tar2sqfs-error.log"), "w") as se:
        subprocess.run(["tar2sqfs", "-c", "lzma", "-b", "131072", "-e", "-x", "-f", raw_sqfs],
                       stdin=fin, stdout=so, stderr=se, check=True)
    if not os.path.exists(raw_sqfs):
        raise SystemExit("tar2sqfs did not create rootfs image")

    print(imagebuilder.wrap_rootfs(raw_sqfs, out_root))
    print(imagebuilder.build_kernel(src_kernel, tgt_kernel, out_kernel))
    print(imagebuilder.verify(out_kernel, out_root))

    with open(os.path.join(args.out, "RECLAIM-SHA256.txt"), "w") as fh:
        fh.write("%s  %s\n" % (imagebuilder.sha256_file(out_kernel),
                               os.path.basename(out_kernel)))
        fh.write("%s  %s\n" % (imagebuilder.sha256_file(out_root),
                               os.path.basename(out_root)))

    print("\n[OK] Reclaimed mirror images built and verified.")
    print("  kernel: %s" % out_kernel)
    print("  rootfs: %s" % out_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
