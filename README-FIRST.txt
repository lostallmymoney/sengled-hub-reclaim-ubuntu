SENGLED ELEMENT HUB RECLAIM 0.2-rc4
===================================

Goal: turn the old Sengled Element Hub into a local EZSP v7 Zigbee coordinator
for Home Assistant ZHA, with no Sengled cloud gateway running.

WHAT YOU NEED
-------------
* 64-bit Windows 10/11 PC on the same LAN as the hub
* A first-generation Z01-HUB or second-generation Z02-HUB Sengled Element Hub
* Internet access on the PC when a download is actually needed:
  - EM357 EmberZNet 6.4.1 / EZSP v7 firmware, only when the coordinator must
    be flashed (this download is kept in that run's output folder)
  - squashfs-tools-ng Windows package, only when it is not already cached
* Do not unplug the hub while a flash stage is running.

NO PuTTY, separate TFTP server, WSL, Linux VM, or Mac is required by this RC.

COMPATIBILITY WARNING
---------------------
The third-generation Sengled hub is NOT compatible, even though it looks
similar to the second-generation hub. Check the model printed on the product
label. Proceed only with Z01-HUB or Z02-HUB. See README.md for reference photos.

TO RUN
------
1. Extract the entire ZIP to a normal folder.
2. Double-click: RECLAIM-SENGLED-HUB.cmd
3. Approve the Windows Administrator/UAC prompt.
4. Enter the Sengled hub IPv4 address when prompted. The tool checks that the
   address is valid and that the hub is reachable on TCP/8686 or TCP/23 before
   continuing.
5. Read the plan. Type RECLAIM to continue. The normal run preserves an
   existing EZSP v7 coordinator and only flashes the coordinator when needed.
6. Leave the hub and PC powered until it says RECLAIM COMPLETE. On the tested
   bootloader, the first Bank2 start may require one cold power-cycle. If so,
   the installer gives exact instructions and resumes the health check after
   you type POWERED. Do not restart the installer or reflash.

To check compatibility without dumping or flashing anything, double-click
CHECK-HUB-ONLY.cmd instead. It may start telnetd through the stock backdoor,
validates the supported layout, and saves six metadata text files plus a log.
It does not stop the stock gateway, touch the coordinator, dump MTD, download
build tools, build images, reboot, or write system flash.

After Bank2 has already been written, double-click TEST-BANK2-BOOT.cmd to check
only the active bank, reclaimed build marker, and TCP/6638 gateway. This mode
does not back up, build, reflash, or reboot anything. It is a troubleshooting
helper; the normal installer now guides the first cold power-cycle itself.

WHAT THE TOOL DOES
------------------
reuse responsive telnet, or start it once through TCP/8686 -> validate exact
hub/dual-bank layout ->
check/flash EM357 coordinator -> dump all four RTL flash partitions -> build a
per-device reclaimed Bank2 image from THAT HUB'S OWN firmware -> verify image
format/checksums -> flash Bank2 rootfs -> byte-verify -> flash Bank2 kernel last
-> byte-verify -> reboot -> verify Bank2 and TCP/6638.

IMPORTANT SAFETY DESIGN
-----------------------
* Bank1 is NEVER written by this tool.
* System images are built from each hub's own backups. No Sengled stock rootfs
  or full flash image is distributed in this ZIP.
* The rootfs is converted to a tar archive without extracting Linux device
  nodes onto Windows, patched, then rebuilt as SquashFS 4.0 LZMA/128K.
* The Bank2 flasher refuses to write unless Bank1 is active and both MTD block
  devices have the exact expected sizes.
* Rootfs is flashed and verified before the Bank2 kernel/boot mark is written.
* The coordinator firmware is verified before it is sent to the hub.
* Failures stop. There is no blind automatic retry of destructive stages.

AFTER SUCCESS
-------------
Home Assistant ZHA radio URL:

    socket://HUB-IP:6638

The completed run folder under output\ contains the complete 8 MiB RTL flash
backup and individual partitions under backup\, rebuilt images and build
intermediates under build\, SHA-256 files, and reclaim.log. Temporary TFTP
staging is removed automatically.

ADVANCED SWITCHES
-----------------
Run Reclaim-SengledHub.ps1 from an elevated Windows PowerShell 5.1+ shell.

* -Hub supplies the IPv4 address instead of prompting.
* -DryRun performs the same metadata-only check as CHECK-HUB-ONLY.cmd.
* -SkipCoordinator skips both the EZSP probe and coordinator flash, but still
  performs the RTL backup/build/Bank2 flash.
* -ForceCoordinator is an advanced debug switch. It programs the public
  EmberZNet 6.4.1 / EZSP v7 image even when the probe reports EZSP v7, and
  requires the explicit FORCE-COORDINATOR confirmation. It has no effect with
  -SkipCoordinator. This does not restore Sengled's original firmware.
* -NoReboot writes and verifies Bank2 but leaves Bank1 running, and therefore
  skips the post-reboot Bank2/TCP 6638 health check.
* -KeepWork preserves temporary TFTP staging; build intermediates are always
  retained.
* -TftpPort changes the built-in server/firewall UDP port from 6969 and accepts
  values from 1024 through 65535.

RELEASE STATUS
--------------
This is an integration RELEASE CANDIDATE. The underlying hub-side pieces and
Bank2 recovery design were proven on a physical Sengled Element Hub. The new
single-controller Windows TFTP + tar-based SquashFS build path still needs an
end-to-end run on another stock hub before this should be advertised broadly as
"one click" production-safe. Keep the first public tests supervised.
