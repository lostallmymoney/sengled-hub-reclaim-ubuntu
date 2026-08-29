#!/usr/bin/env python3
"""
sengled-reclaim - Ubuntu/Linux controller for Sengled Element Hub reclaim.

Drive the full reclaim from Linux: talk to the hub over telnet (HubShell),
serve the images over TFTP (TftpServer), build them locally (imagebuilder),
and reflash the hub-side MIPS payload binaries from payload/.

Usage examples:
  python3 -m reclaimhub controller --hub 10.42.0.119 --dryrun
  python3 -m reclaimhub controller --hub 10.42.0.119
  python3 -m reclaimhub controller --hub 10.42.0.119 --backup-only
"""
import argparse
import hashlib
import os
import re
import shutil
import socket
import subprocess
import sys
import time

from .hubshell import HubShell, ShellError, ShellTimeout
from .tftpserver import TftpServer
from .tarpatcher import patch as tar_patch
from . import imagebuilder

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PAYLOAD = os.path.join(REPO_ROOT, "payload")

COORD_URL = ("https://raw.githubusercontent.com/walthowd/husbzb-firmware/master/"
             "em357-v641-ncp-uart-sw.ebl")
COORD_EXPECTED_LEN = 146816
COORD_EXPECTED_GIT_SHA = "361738c5116a97e7d755df46d6bcc31e167038fd"


class Controller:
    def __init__(self, hub, tftp_port=6969, keep_work=False):
        self.hub = hub
        self.tftp_port = tftp_port
        self.keep_work = keep_work
        self.shell = None
        self.tftp = None
        self.run_dir = None

    # ---------- helpers ----------
    def step(self, text):
        print("\n=== %s ===" % text)

    def step_pad(self, text):
        print("[OK] %s" % text)

    def warn(self, text):
        print("[!] %s" % text)

    def tcp_open(self, port, timeout_ms=1200):
        try:
            s = socket.create_connection((self.hub, port), timeout=timeout_ms / 1000.0)
            s.close()
            return True
        except OSError:
            return False

    def wait_tcp(self, port, timeout_s, label):
        until = time.time() + timeout_s
        while time.time() < until:
            if self.tcp_open(port, 700):
                self.step_pad("%s is online at %s:%d" % (label, self.hub, port))
                return
            time.sleep(2)
        raise RuntimeError("Timed out waiting for %s at %s:%d" % (label, self.hub, port))

    def invoke_at(self, commands):
        """Stock TCP/8686 AT backdoor. Faithful to Invoke-SengledAT."""
        s = socket.create_connection((self.hub, 8686), timeout=5)
        s.settimeout(0.08)
        try:
            for cmd in commands:
                print("AT backdoor: %s" % cmd)
                s.sendall((cmd + "\r\n").encode())
                reply = b""
                deadline = time.time() + 4
                while time.time() < deadline:
                    try:
                        data = s.recv(4096)
                    except socket.timeout:
                        data = b""
                    if data:
                        reply += data
                        text = reply.decode("ascii", "replace")
                        if ":OK" in text or ":FAIL" in text:
                            break
                    else:
                        time.sleep(0.08)
                text = reply.decode("ascii", "replace").strip()
                if ":FAIL" in text:
                    raise RuntimeError("Hub rejected %s : %s" % (cmd, text))
                if ":OK" not in text:
                    raise RuntimeError("Unexpected/no response to %s : %s" % (cmd, text))
        finally:
            s.close()

    def run_cmd(self, command, timeout_s=30, allow_failure=False, quiet=False):
        r = self.shell.run(command, timeout_s * 1000)
        if not quiet and r.output:
            print(r.output)
        if not allow_failure and r.exit_code != 0:
            raise RuntimeError("Hub command failed (rc=%s): %s\n%s"
                               % (r.exit_code, command, r.output))
        return r

    def connect_shell(self):
        # Try an existing responsive telnet shell first.
        try:
            sh = HubShell(self.hub, 23)
            sh.connect(5000)
            time.sleep(0.75)
            sh.send_raw_line("")
            time.sleep(0.35)
            probe = sh.run("echo __SENGLED_SHELL_READY__", 7000)
            if probe.exit_code == 0 and "__SENGLED_SHELL_READY__" in probe.output:
                self.step_pad("telnet command shell is responsive at %s:23" % self.hub)
                self.shell = sh
                return
            sh.close()
        except (ShellError, ShellTimeout) as e:
            self.warn("Existing telnet shell check failed: %s" % e)
            try:
                sh.close()
            except Exception:
                pass

        # Start telnetd once via the stock backdoor.
        self.wait_tcp(8686, 60, "stock debug service")
        self.step("No usable telnet shell found; starting telnetd once")
        self.invoke_at(["AT+START_TELNETD=1"])
        time.sleep(2)

        last = None
        for attempt in range(5):
            try:
                sh = HubShell(self.hub, 23)
                sh.connect(5000)
                time.sleep(0.75)
                sh.send_raw_line("")
                time.sleep(0.35)
                probe = sh.run("echo __SENGLED_SHELL_READY__", 7000)
                if probe.exit_code == 0 and "__SENGLED_SHELL_READY__" in probe.output:
                    self.step_pad("telnet command shell is responsive at %s:23" % self.hub)
                    self.shell = sh
                    return
                sh.close()
            except (ShellError, ShellTimeout) as e:
                last = e
                try:
                    sh.close()
                except Exception:
                    pass
            if attempt < 4:
                time.sleep(2)
        raise RuntimeError("Hub shell at %s:23 did not become command-responsive. Last error: %s" % (self.hub, last))

    # ---------- TFTP transfer ----------
    def send_file(self, local_path, hub_path, remote_name, timeout_s=180):
        shutil.copyfile(local_path, os.path.join(self.tftp_root, remote_name))
        self.run_cmd("tftp -g -r %s -l %s %s %d" % (remote_name, hub_path, self.pc_ip, self.tftp_port),
                     timeout_s, quiet=True)
        expected = os.path.getsize(local_path)
        r = self.run_cmd("wc -c %s" % hub_path, 20, quiet=True)
        if str(expected) not in r.output:
            raise RuntimeError("Hub file size check failed for %s (expected %d): %s"
                               % (hub_path, expected, r.output))
        self.step_pad("Transferred %s (%d bytes)" % (remote_name, expected))

    def install_executable(self, local_path, name):
        dat = "/tmp/%s.dat" % name
        exe = "/tmp/%s" % name
        self.send_file(local_path, dat, name)
        r = self.run_cmd("cp /bin/busybox %s && cat %s > %s && rm -f %s && ls -l %s"
                         % (exe, dat, exe, dat, exe), 30, quiet=True)
        if "rwx" not in r.output:
            raise RuntimeError("Executable-carrier bootstrap failed for %s : %s" % (name, r.output))
        self.step_pad("Installed executable /tmp/%s" % name)
        return exe

    def receive_file(self, hub_path, remote_name, destination, expected):
        server_file = os.path.join(self.tftp_root, remote_name)
        for p in (server_file, server_file + ".part"):
            if os.path.exists(p):
                os.remove(p)
        self.run_cmd("tftp -p -l %s -r %s %s %d"
                     % (hub_path, remote_name, self.pc_ip, self.tftp_port), 300, quiet=True)
        deadline = time.time() + 5
        while not os.path.exists(server_file) and time.time() < deadline:
            time.sleep(0.02)
        if not os.path.exists(server_file):
            raise RuntimeError("TFTP upload did not commit %s within 5 seconds" % remote_name)
        size = os.path.getsize(server_file)
        if size != expected:
            raise RuntimeError("Backup %s has %d bytes; expected %d" % (remote_name, size, expected))
        shutil.copyfile(server_file, destination)
        self.step_pad("Backed up %s (%d bytes)" % (remote_name, size))

    # ---------- stages ----------
    def validate_stock_hub(self):
        self.step("Validating supported hub layout")
        boot = self.run_cmd("cat /proc/bootbank", 15, quiet=True)
        # Faithful port: tool requires Bank1 active.
        if not re.match(r"(?m)^1\s*$", boot.output.strip()):
            raise RuntimeError("Public installer requires Bank1 active. /proc/bootbank output:\n%s"
                               % boot.output)
        dual = self.run_cmd("flash get DUALBANK_ENABLED", 15, quiet=True)
        if "DUALBANK_ENABLED=1" not in dual.output:
            raise RuntimeError("DUALBANK_ENABLED is not 1: %s" % dual.output)
        mtd = self.run_cmd("cat /proc/mtd", 15, quiet=True)
        for needle in ("mtd0: 00130000", "mtd1: 002d0000", "mtd2: 00130000", "mtd3: 002d0000"):
            if needle.lower() not in mtd.output.lower():
                raise RuntimeError("Unexpected MTD layout; missing '%s'" % needle)
        dev = self.run_cmd("ls -l /dev/mtdblock0 /dev/mtdblock1 /dev/mtdblock2 /dev/mtdblock3 /dev/ttyS1 /proc/gpio_ctrl",
                           15, quiet=True)
        if dev.exit_code != 0:
            raise RuntimeError("Required MTD/UART/GPIO interfaces are missing")
        self.step_pad("Bank1 active, dual-bank enabled, expected RTL8196E layout present")

    def stop_stock_gateway(self):
        stop = ("killall sengled_startup 2>/dev/null; killall sengled_gateway_app 2>/dev/null; "
                "killall sengled_start.sh 2>/dev/null; sleep 2; "
                "if ps | grep '[s]engled_gateway_app\\|[s]engled_startup\\|[s]engled_start\\.sh' >/dev/null; "
                "then echo __SENGLED_FORCE_KILL__; killall -9 sengled_startup 2>/dev/null; "
                "killall -9 sengled_gateway_app 2>/dev/null; killall -9 sengled_start.sh 2>/dev/null; sleep 1; fi")
        result = self.run_cmd(stop, 20, allow_failure=True, quiet=True)
        if "__SENGLED_FORCE_KILL__" in result.output:
            self.warn("Stock gateway ignored graceful termination; forced it to release /dev/ttyS1")
        check = self.run_cmd("ps | grep '[s]engled'", 10, allow_failure=True, quiet=True)
        if any(k in check.output for k in ("sengled_gateway_app", "sengled_startup", "sengled_start.sh")):
            raise RuntimeError("Stock Sengled process is still alive and may own /dev/ttyS1: %s" % check.output)

    def probe_coordinator(self):
        self.stop_stock_gateway()
        probe = self.install_executable(os.path.join(PAYLOAD, "em357-v641-live-probe-v1"),
                                        "em357-v641-live-probe-v1")
        r = self.run_cmd(probe, 15, allow_failure=True, quiet=True)
        print(r.output)
        if r.exit_code == 0 and "EZSP_V7_OK" in r.output:
            return True
        self.warn("No VERSION response; initializing ASH link and retrying once")
        reset = self.run_cmd("printf '\\032\\300\\070\\274\\176' > /dev/ttyS1; sleep 1",
                             10, allow_failure=True, quiet=True)
        if reset.exit_code != 0:
            self.warn("Could not send ASH reset frame (rc=%s): %s" % (reset.exit_code, reset.output))
            return False
        r = self.run_cmd(probe, 15, allow_failure=True, quiet=True)
        print(r.output)
        return r.exit_code == 0 and "EZSP_V7_OK" in r.output

    def get_coordinator_firmware(self, destination):
        print("Downloading public EM357 EmberZNet 6.4.1 / EZSP v7 firmware...")
        import urllib.request
        urllib.request.urlretrieve(COORD_URL, destination)
        length = os.path.getsize(destination)
        if length != COORD_EXPECTED_LEN:
            raise RuntimeError("Coordinator firmware length %d != %d" % (length, COORD_EXPECTED_LEN))
        git = git_blob_sha1(destination)
        if git != COORD_EXPECTED_GIT_SHA:
            raise RuntimeError("Coordinator firmware Git blob SHA-1 mismatch: %s" % git)
        self.step_pad("Coordinator firmware verified: %d bytes, Git blob %s" % (length, git))

    def backup_rtl_flash(self, backup_dir):
        self.step("Dumping complete RTL8196E flash")
        os.makedirs(backup_dir, exist_ok=True)
        self.save_metadata(backup_dir)
        parts = [
            ("/dev/mtdblock0", "mtd0-bank1-kernel.bin", 0x130000),
            ("/dev/mtdblock1", "mtd1-bank1-rootfs.bin", 0x2D0000),
            ("/dev/mtdblock2", "mtd2-bank2-kernel.bin", 0x130000),
            ("/dev/mtdblock3", "mtd3-bank2-rootfs.bin", 0x2D0000),
        ]
        for hub_path, name, size in parts:
            self.receive_file(hub_path, name, os.path.join(backup_dir, name), size)
        full = os.path.join(backup_dir, "fullflash-8mb.bin")
        with open(full, "wb") as out:
            for _, name, _ in parts:
                with open(os.path.join(backup_dir, name), "rb") as src:
                    shutil.copyfileobj(src, out)
        if os.path.getsize(full) != 0x800000:
            raise RuntimeError("Assembled fullflash backup is not exactly 8 MiB")
        hashes = []
        for f in sorted(os.listdir(backup_dir)):
            if f.endswith(".bin"):
                hashes.append("%s  %s" % (imagebuilder.sha256_file(os.path.join(backup_dir, f)), f))
        with open(os.path.join(backup_dir, "BACKUP-SHA256.txt"), "w") as fh:
            fh.write("\n".join(hashes) + "\n")
        self.step_pad("Complete flash backup saved to %s" % backup_dir)

    def save_metadata(self, backup_dir):
        items = {
            "proc-mtd.txt": "cat /proc/mtd",
            "cmdline.txt": "cat /proc/cmdline",
            "bootbank.txt": "cat /proc/bootbank",
            "dualbank.txt": "flash get DUALBANK_ENABLED",
            "mounts.txt": "mount",
            "flash-all.txt": "flash all",
        }
        for name, cmd in items.items():
            r = self.run_cmd(cmd, 45, allow_failure=True, quiet=True)
            with open(os.path.join(backup_dir, name), "w") as fh:
                fh.write(r.output + "\n")

    def build_images(self, backup_dir, build_dir):
        self.step("Preparing per-device reclaimed Bank2 images")
        os.makedirs(build_dir, exist_ok=True)
        stock_tar = os.path.join(build_dir, "rootfs-stock.tar")
        patched_tar = os.path.join(build_dir, "rootfs-reclaimed.tar")
        raw_sqfs = os.path.join(build_dir, "rootfs-reclaimed.raw.sqfs")
        bank1_root = os.path.join(backup_dir, "mtd1-bank1-rootfs.bin")
        bank1_kernel = os.path.join(backup_dir, "mtd0-bank1-kernel.bin")
        bank2_kernel = os.path.join(backup_dir, "mtd2-bank2-kernel.bin")
        out_root = os.path.join(build_dir, "mtd3-bank2-rootfs-reclaimed.bin")
        out_kernel = os.path.join(build_dir, "mtd2-bank2-kernel-reclaimed.bin")

        # SquashFS -> tar. Prefer sqfs2tar (metadata-preserving); else unsquashfs.
        sqfs2tar = which_sqfs2tar()
        if sqfs2tar:
            print("[build] sqfs2tar: SquashFS -> metadata-preserving tar")
            subprocess.run([sqfs2tar, "-r", ".", "-X", bank1_root],
                           stdout=open(stock_tar, "wb"),
                           stderr=open(os.path.join(build_dir, "sqfs2tar.log"), "w"), check=True)
        else:
            print("[build] sqfs2tar not found; using unsquashfs (device nodes split)")
            use_unsquashfs(bank1_root, build_dir, stock_tar)

        if os.path.getsize(stock_tar) < 1024:
            raise RuntimeError("tar conversion produced an unexpectedly small archive")

        build_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        build_text = ("Sengled reclaimed rootfs v2\nBuilt: %s\n"
                      "Gateway: EZSP TCP/6638 -> /dev/ttyS1 @ 57600\n" % build_time)
        status_text = '''#!/bin/sh
echo "=== Sengled reclaimed hub ==="
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

        tar2sqfs = which_tar2sqfs()
        print("[WAIT] Rebuilding SquashFS can take several minutes...")
        if tar2sqfs:
            subprocess.run([tar2sqfs, "-c", "lzma", "-b", "131072", "-e", "-x",
                            "-f", raw_sqfs],
                           stdin=open(patched_tar, "rb"),
                           stdout=open(os.path.join(build_dir, "tar2sqfs-output.log"), "w"),
                           stderr=open(os.path.join(build_dir, "tar2sqfs-error.log"), "w"),
                           check=True)
        else:
            use_mksquashfs(patched_tar, raw_sqfs, bank1_root)

        if not os.path.exists(raw_sqfs):
            raise RuntimeError("tar2sqfs did not create rootfs image")

        print(imagebuilder.wrap_rootfs(raw_sqfs, out_root))
        print(imagebuilder.build_kernel(bank1_kernel, bank2_kernel, out_kernel))
        print(imagebuilder.verify(out_kernel, out_root))

        with open(os.path.join(build_dir, "RECLAIM-SHA256.txt"), "w") as fh:
            fh.write("%s  %s\n" % (imagebuilder.sha256_file(out_kernel), "mtd2-bank2-kernel-reclaimed.bin"))
            fh.write("%s  %s\n" % (imagebuilder.sha256_file(out_root), "mtd3-bank2-rootfs-reclaimed.bin"))

        print("[OK] Reclaimed images built and verified")
        return {"kernel": out_kernel, "rootfs": out_root,
                "hashes": os.path.join(build_dir, "RECLAIM-SHA256.txt")}

    def flash_system_bank2(self, images):
        self.step("Flashing reclaimed system to inactive Bank2")
        flasher = self.install_executable(os.path.join(PAYLOAD, "bank2-safe-flash-v2-block"),
                                          "bank2-safe-flash-v2-block")
        self.run_cmd("rm -f /tmp/FLASH_BANK2_ROOTFS_NOW /tmp/FLASH_BANK2_KERNEL_NOW", 10,
                     allow_failure=True, quiet=True)
        dry = self.run_cmd(flasher, 30, allow_failure=True, quiet=True)
        print(dry.output)
        if ("ACTIVE BOOT BANK REPORTS: 1" not in dry.output
                or "mtdblock2" not in dry.output or "OK" not in dry.output
                or "mtdblock3" not in dry.output):
            raise RuntimeError("Bank2 flasher read-only probe did not match the proven layout")

        self.send_file(images["rootfs"], "/tmp/mtd3-bank2-rootfs-reclaimed.bin",
                       "mtd3-bank2-rootfs-reclaimed.bin")
        self.run_cmd("echo 1 > /tmp/FLASH_BANK2_ROOTFS_NOW", 10, quiet=True)
        print("[WAIT] Bank2 rootfs is being written and byte-verified. Keep the hub powered.")
        try:
            rr = self.run_cmd(flasher, 360, allow_failure=True, quiet=True)
            print(rr.output)
            if rr.exit_code != 0 or "VERIFY: PASS" not in rr.output:
                raise RuntimeError("Bank2 rootfs flash/verify failed")
        finally:
            self.run_cmd("rm -f /tmp/FLASH_BANK2_ROOTFS_NOW", 10, allow_failure=True, quiet=True)
        self.run_cmd("rm -f /tmp/mtd3-bank2-rootfs-reclaimed.bin", 15, allow_failure=True, quiet=True)
        self.step_pad("Bank2 rootfs written and byte-for-byte verified")

        self.send_file(images["kernel"], "/tmp/mtd2-bank2-kernel-reclaimed.bin",
                       "mtd2-bank2-kernel-reclaimed.bin")
        self.run_cmd("echo 1 > /tmp/FLASH_BANK2_KERNEL_NOW", 10, quiet=True)
        print("[WAIT] Bank2 kernel is being written and byte-verified. Keep the hub powered.")
        try:
            kr = self.run_cmd(flasher, 300, allow_failure=True, quiet=True)
            print(kr.output)
            if kr.exit_code != 0 or "VERIFY: PASS" not in kr.output:
                raise RuntimeError("Bank2 kernel flash/verify failed")
        finally:
            self.run_cmd("rm -f /tmp/FLASH_BANK2_KERNEL_NOW", 10, allow_failure=True, quiet=True)
        bank = self.run_cmd("cat /proc/bootbank", 10, quiet=True)
        if not re.match(r"(?m)^1\s*$", bank.output.strip()):
            raise RuntimeError("Active bank changed before reboot; refusing to continue")
        self.step_pad("Bank2 kernel written and byte-for-byte verified; Bank1 is still running")

    # ---------- top-level flows ----------
    def run(self, dryrun=False, backup_only=False, dump_only=False,
            skip_coordinator=False,
            force_coordinator=False, no_reboot=False):
        self.step("Opening stock hub through TCP/8686 backdoor")
        self.connect_shell()
        self.step("Checking connectivity for TFTP")
        self.pc_ip = get_local_ip(self.hub)
        self.step_pad("PC address visible to hub: %s" % self.pc_ip)
        self.tftp_root = os.path.join(self.run_dir, "tftp")
        os.makedirs(self.tftp_root, exist_ok=True)
        self.tftp = TftpServer(self.tftp_root, self.tftp_port)
        self.tftp.start()

        backup_dir = os.path.join(self.run_dir, "backup")
        build_dir = os.path.join(self.run_dir, "build")

        try:
            if dryrun:
                self.validate_stock_hub()
                self.save_metadata(backup_dir)
                print("\n[OK] DRY RUN: metadata saved to %s" % backup_dir)
                print("No coordinator or system flash was performed.")
                return

            if dump_only:
                # Read-only flash backup. Distinct from --backup-only: this is
                # safe to run on any boot state (reading never writes) and does
                # NOT apply the Bank1-active gate, which exists to protect the
                # destructive flash path. Nothing is flashed in this mode.
                print("\n[OK] DUMP ONLY: reading all four RTL flash partitions to the PC.")
                self.backup_rtl_flash(backup_dir)
                print("[OK] Read-only flash backup complete. No coordinator or RTL flash was modified.")
                return

            if backup_only:
                self.validate_stock_hub()
                self.backup_rtl_flash(backup_dir)
                print("\n[OK] BACKUP ONLY: no coordinator or flash-write stages ran.")
                return

            # Destructive confirmation
            if force_coordinator:
                ans = input("Type FORCE-COORDINATOR to confirm the debug reflash and full reclaim: ").strip()
                if ans != "FORCE-COORDINATOR":
                    raise RuntimeError("Cancelled by user")
            else:
                ans = input("Type RECLAIM to continue with %s: " % self.hub).strip()
                if ans != "RECLAIM":
                    raise RuntimeError("Cancelled by user")

            self.validate_stock_hub()

            if not skip_coordinator:
                self.step("Checking onboard coordinator")
                already_v7 = self.probe_coordinator()
                if already_v7 and not force_coordinator:
                    self.step_pad("Coordinator already speaks EZSP v7; destructive coordinator reflash skipped")
                else:
                    self.flash_coordinator()
            else:
                self.warn("Coordinator stage skipped by --skip-coordinator")

            self.backup_rtl_flash(backup_dir)
            images = self.build_images(backup_dir, build_dir)
            self.flash_system_bank2(images)

            if no_reboot:
                self.warn("All flashes verified. --no-reboot requested, Bank1 remains active until you reboot manually.")
                return

            self.step("Rebooting into reclaimed Bank2")
            self.shell.send_raw_line("reboot")
            self.shell.close()
            self.shell = None
            time.sleep(4)
            self.connect_shell_after_reboot()
            self.test_reclaimed_health()
        finally:
            if self.shell:
                self.shell.close()
            if self.tftp:
                self.tftp.stop()

    def connect_shell_after_reboot(self):
        last = None
        for attempt in range(20):
            try:
                sh = HubShell(self.hub, 23)
                sh.connect(5000)
                time.sleep(0.75)
                sh.send_raw_line("")
                time.sleep(0.35)
                probe = sh.run("echo __SENGLED_SHELL_READY__", 7000)
                if probe.exit_code == 0 and "__SENGLED_SHELL_READY__" in probe.output:
                    self.shell = sh
                    return
                sh.close()
            except (ShellError, ShellTimeout) as e:
                last = e
                try:
                    sh.close()
                except Exception:
                    pass
            time.sleep(3)
        raise RuntimeError("Hub shell did not become responsive after reboot: %s" % last)

    def test_reclaimed_health(self):
        try:
            bank = self.run_cmd("cat /proc/bootbank", 15, quiet=True)
            if not re.match(r"(?m)^2\s*$", bank.output.strip()):
                raise RuntimeError("Hub is running Bank %s, not reclaimed Bank2" % bank.output.strip())
        except RuntimeError as e:
            if "not reclaimed Bank2" not in str(e) and "Bank" not in str(e):
                raise
            self.warn("The verified Bank2 image needs one cold boot on this hub. Nothing needs to be reflashed.")
            input("1. Unplug power. 2. Wait 10s. 3. Reconnect. Type POWERED then press Enter: ")
            self.shell.close()
            self.shell = None
            self.connect_shell_after_reboot()
            bank = self.run_cmd("cat /proc/bootbank", 15, quiet=True)
            if not re.match(r"(?m)^2\s*$", bank.output.strip()):
                raise RuntimeError("Hub is running Bank %s after cold boot" % bank.output.strip())

        marker = self.run_cmd("test -f /etc/reclaim-build.txt && cat /etc/reclaim-build.txt",
                              15, allow_failure=True, quiet=True)
        if marker.exit_code != 0 or "Sengled reclaimed rootfs" not in marker.output:
            raise RuntimeError("Bank2 is active, but the reclaimed build marker is missing")
        status = self.run_cmd("reclaim-status", 30, allow_failure=True, quiet=True)
        print(status.output)
        self.wait_tcp(6638, 30, "EZSP gateway")
        self.step_pad("Bank2 reclaimed filesystem and TCP/6638 health checks passed")
        print("\nRECLAIM COMPLETE")
        print("Home Assistant ZHA radio URL: socket://%s:6638" % self.hub)
        print("Recovery Bank1 was not modified.")
        print("Backup + build artifacts: %s" % self.run_dir)

    def flash_coordinator(self):
        self.step("Flashing onboard EM357 coordinator to EZSP v7")
        self.stop_stock_gateway()
        fw = os.path.join(self.run_dir, "em357-v641-ncp-uart-sw.ebl")
        self.get_coordinator_firmware(fw)
        self.send_file(fw, "/tmp/em357-v641-ncp-uart-sw.ebl", "em357-v641-ncp-uart-sw.ebl")
        flasher = self.install_executable(os.path.join(PAYLOAD, "em357-flash-v641-public-v1"),
                                          "em357-flash-v641-public-v1")
        self.run_cmd("echo YES > /tmp/FLASH_EM357_NOW", 10, quiet=True)
        try:
            print("\n[WAIT] Coordinator flash is now running on the hub. This can take several minutes.")
            r = self.run_cmd(flasher, 360, allow_failure=True, quiet=True)
            print(r.output)
            if r.exit_code != 0 or "FLASH COMPLETE" not in r.output:
                raise RuntimeError("EM357 flash failed (rc=%s). DO NOT POWER-CYCLE if the flasher reported that it left the bootloader active." % r.exit_code)
        finally:
            self.run_cmd("rm -f /tmp/FLASH_EM357_NOW", 10, allow_failure=True, quiet=True)
        time.sleep(2)
        if not self.probe_coordinator():
            raise RuntimeError("EM357 flash completed, but post-flash EZSP v7 probe failed")
        self.step_pad("EM357 is running EZSP protocol v7")


def git_blob_sha1(path):
    data = open(path, "rb").read()
    header = b"blob %d\0" % len(data)
    return hashlib.sha1(header + data).hexdigest()


def get_local_ip(remote):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def which_sqfs2tar():
    return shutil.which("sqfs2tar") or shutil.which("squashfs-tools-ng") \
        or (shutil.which("wsl") and None)  # no wsl fallback needed


def which_tar2sqfs():
    return shutil.which("tar2sqfs") or shutil.which("squashfs-tools-ng")


def use_unsquashfs(bank1_root, build_dir, stock_tar):
    """Fallback: unsquashfs to a staging dir (device nodes colliding) is risky;
    instead attempt as root with -d to preserve. Best effort; sqfs2tar preferred."""
    if os.geteuid() != 0:
        raise RuntimeError("unsquashfs fallback requires root; install squashfs-tools-ng (sqfs2tar) instead")
    staging = os.path.join(build_dir, "squashfs-root")
    subprocess.run(["unsquashfs", "-f", "-d", staging, bank1_root], check=True)
    subprocess.run(["tar", "-C", staging, "-cf", stock_tar, "."], check=True)


def use_mksquashfs(patched_tar, raw_sqfs, bank1_root):
    """Fallback: reconstruct from tar (device nodes) as root. sqfs2tar/tar2sqfs is preferred."""
    if os.geteuid() != 0:
        raise RuntimeError("mksquashfs fallback requires root; install squashfs-tools-ng (tar2sqfs) instead")
    staging = os.path.join(os.path.dirname(raw_sqfs), "rootfs-staging")
    if os.path.exists(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)
    subprocess.run(["tar", "-C", staging, "-xf", patched_tar], check=True)
    subprocess.run(["mksquashfs", staging, raw_sqfs,
                    "-comp", "xz", "-b", "131072",
                    "-noappend"], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sengled Element Hub reclaim (Linux)")
    parser.add_argument("--hub", required=True, help="Hub IPv4 address")
    parser.add_argument("--tftp-port", type=int, default=6969)
    parser.add_argument("--dryrun", action="store_true")
    parser.add_argument("--backup-only", action="store_true")
    parser.add_argument("--dump-only", action="store_true")
    parser.add_argument("--skip-coordinator", action="store_true")
    parser.add_argument("--force-coordinator", action="store_true")
    parser.add_argument("--no-reboot", action="store_true")
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args(argv)

    ctl = Controller(args.hub, args.tftp_port, args.keep_work)
    run_dir = os.path.join("output", "%s-%s" % (args.hub, time.strftime("%Y%m%d-%H%M%S", time.gmtime())))
    os.makedirs(run_dir, exist_ok=True)
    ctl.run_dir = run_dir
    ctl.run(dryrun=args.dryrun, backup_only=args.backup_only,
            dump_only=args.dump_only,
            skip_coordinator=args.skip_coordinator,
            force_coordinator=args.force_coordinator,
            no_reboot=args.no_reboot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
