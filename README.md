# Sengled Element Hub Reclaim — One-Stop Installer 0.2-rc4

This package automates the full reclaim path for the tested Sengled Element Hub / RTL8196E + onboard EM357 hardware layout. The end state is a local standards-based EZSP v7 coordinator exposed as TCP/6638 for Home Assistant ZHA.

## Requirements

- A 64-bit Windows PC with Windows PowerShell 5.1 or later
- The PC and hub on the same LAN
- Administrator access on the PC (needed for a temporary inbound UDP firewall rule)
- Internet access when the coordinator firmware or Windows SquashFS tools must be downloaded

No separate TFTP server, PuTTY, WSL, Linux VM, or Mac is required.

## Normal reclaim

Extract the ZIP and double-click `RECLAIM-SENGLED-HUB.cmd`. The launcher elevates only so it can add a temporary inbound UDP rule for its built-in TFTP server. The PowerShell controller asks for the Sengled hub IPv4 address at startup and checks that the hub is reachable on TCP/8686 or TCP/23. At the destructive-operation confirmation, type `RECLAIM` for the normal run, which preserves an existing EZSP v7 coordinator, or type `REFLASH` to deliberately reflash the coordinator and exercise the entire workflow.

The intended flow is:

```text
reuse a responsive telnet shell, or use stock TCP/8686 to start telnetd once
        |
        v
validate Bank1 + dual-bank + exact MTD/UART/GPIO layout
        |
        v
stop stock Sengled UART owner
        |
        v
probe EM357 -> flash public EZSP v7 only when needed -> verify v7
        |
        v
dump mtd0..mtd3 to PC + assemble fullflash-8mb.bin + SHA256
        |
        v
convert the hub's OWN Bank1 SquashFS to tar without filesystem extraction
        |
        v
patch rcS + add ezsp_gateway / watchdog / status helper
        |
        v
rebuild SquashFS 4.0 LZMA 128K + Realtek rootfs checksum wrapper
        |
        v
build Bank2 kernel from own Bank1 kernel + own Bank2 prefix
        |
        v
verify images
        |
        v
flash mtd3 -> byte-for-byte verify
        |
        v
flash mtd2 LAST -> byte-for-byte verify
        |
        v
warm reboot -> if still on Bank1, guide one cold power-cycle
        |
        v
require /proc/bootbank=2 -> require reclaimed marker + TCP/6638
```

The temporary firewall rule and TFTP server are removed when the controller exits, including after an error. Do not unplug the hub during either coordinator or Bank2 flashing. On the tested stock bootloader, the first switch to Bank2 may require a cold power-cycle even though both partitions were written and verified. The installer detects that specific condition, gives the user exact unplug/reconnect instructions, and then resumes the health check without reflashing.

## Safe compatibility check

Double-click `CHECK-HUB-ONLY.cmd` to run the controller with `-DryRun`. This mode:

- validates the IPv4 address and obtains a responsive telnet shell, starting `telnetd` once through TCP/8686 if necessary;
- validates the supported Bank1, dual-bank, MTD, UART, and GPIO layout;
- creates `output\<hub>-<UTC timestamp>\`, writes `reclaim.log`, and saves six text metadata snapshots under `backup\`;
- does not stop the stock Sengled gateway, probe or flash the EM357, dump any MTD partition, download build dependencies, build images, reboot, or write RTL flash.

## Read-only flash backup

Run `DUMP-HUB-FLASH-ONLY.cmd` to validate the hub and stream all four RTL flash partitions directly to the PC. This mode saves the individual partitions, an assembled 8 MiB image, metadata, SHA-256 hashes, and a log under `output\`. It does not probe or flash the coordinator, build reclaimed images, write RTL flash, or reboot the hub.

Run `TEST-BANK2-BOOT.cmd` after Bank2 has already been written to check only `/proc/bootbank`, the reclaimed build marker, and TCP/6638 from the PC. It performs no backup, build, flash write, or reboot. This remains available as a troubleshooting check; the normal installer handles the known first-boot cold power-cycle interactively.

## Why it builds from each hub instead of shipping a firmware image

The installer deliberately does **not** ship Sengled's stock firmware. The reclaimed rootfs is rebuilt from the user's own Bank1 rootfs. The Bank2 kernel image preserves that unit's original first `0x30000` bytes from Bank2 and then copies the current Bank1 kernel area before setting the tested `0xFFFFFFF1` bank mark. This also avoids pretending that one unit's raw partition images have been proven universal across all hardware revisions.

## Safety gates

The public RC refuses to proceed unless the tested layout is present: Bank1 active, `DUALBANK_ENABLED=1`, MTD sizes `0x130000/0x2d0000/0x130000/0x2d0000`, `/dev/ttyS1`, `/proc/gpio_ctrl`, and all four MTD block devices. The image builder additionally requires the known `cr6c` kernel headers and the proven Bank1 mark `0xFFFFFFF0`. It will not overwrite an equal/newer Bank2 mark.

Bank1 is never opened for writing. The system flasher only accepts `/dev/mtdblock2` and `/dev/mtdblock3`, and only while `/proc/bootbank` reports `1`. Rootfs is written first; the kernel containing the new Bank2 mark is written last. Each partition is reopened and compared byte-for-byte with its source before the workflow advances.

The EM357 coordinator stage is separate from RTL flash. The controller first probes for EZSP v7 and skips the destructive coordinator reflash when v7 is already live. If the first VERSION request is silent, it initializes the ASH link with the standard software-reset handshake and retries once; this is necessary for a freshly booted NCP that is still waiting for host link initialization. Otherwise it downloads the public 6.4.1 image, checks the exact 146,816-byte length and expected Git blob SHA-1, then uses the onboard serial bootloader/XMODEM path. After EOT is acknowledged, the flasher allows five seconds for legacy-bootloader EBL validation/finalization before resetting into the application. A post-flash live EZSP VERSION probe must return v7.

## Downloads and caching

The ZIP intentionally does not redistribute the coordinator firmware or third-party SquashFS binaries. The controller downloads them only when their corresponding stage needs them:

- If the live probe does not report EZSP v7, or `-ForceCoordinator` is used, the controller downloads `em357-v641-ncp-uart-sw.ebl` from the public `walthowd/husbzb-firmware` repository. It requires an exact length of 146,816 bytes and Git blob SHA-1 `361738c5116a97e7d755df46d6bcc31e167038fd`. This file is stored in that run's output directory, not in `cache\`.
- During image building, if the required executables are not already present in the extracted cache, the controller downloads `squashfs-tools-ng-1.3.2-mingw64.zip` from the infraroot Windows binary archive and extracts it. The package provides `sqfs2tar.exe` and `tar2sqfs.exe` with LZMA support.

The SquashFS ZIP and extracted tools remain under `cache\` for later runs. The script checks that both executables exist after extraction; it does not currently pin or verify the SquashFS archive by hash.

## Windows rootfs build design

Classic `unsquashfs` extraction is awkward on Windows because the stock image contains hundreds of Linux device nodes. This RC avoids that entirely. `sqfs2tar` serializes the SquashFS metadata and special inodes into a tar stream. `ReclaimSupport.cs` copies the tar entries raw, patches only `/etc/init.d/rcS`, and adds the reclaimed binaries/scripts as tar records. `tar2sqfs` then reconstructs a SquashFS image from that metadata-preserving archive. No `/dev` node ever has to exist on NTFS.

The rebuilt SquashFS must pass the internal structural checks (SquashFS 4.0, compression ID 2/LZMA, block size 131072), then the tool installs the Realtek bootloader checksum convention and validates its big-endian 16-bit sum before a flash is possible.

## Output and recovery material

Every invocation that gets past package validation creates a timestamped `output\<hub>-<UTC timestamp>\` directory. A completed reclaim contains:

- `backup\mtd0-bank1-kernel.bin`, `mtd1-bank1-rootfs.bin`, `mtd2-bank2-kernel.bin`, and `mtd3-bank2-rootfs.bin`
- `backup\fullflash-8mb.bin`, assembled in MTD0-through-MTD3 order
- six metadata snapshots and `backup\BACKUP-SHA256.txt`
- `build\mtd2-bank2-kernel-reclaimed.bin` and `build\mtd3-bank2-rootfs-reclaimed.bin`
- `build\RECLAIM-SHA256.txt`, intermediate tar/SquashFS files, and converter logs
- `reclaim.log`

If the coordinator is flashed, its downloaded `.ebl` also remains at the top of the run directory. The temporary `tftp\` staging directory is deleted on exit unless `-KeepWork` is supplied. Keep the whole run directory; Bank1 remains the untouched recovery bank.

## Advanced invocation

The double-click path is the normal one. Advanced users can open an elevated Windows PowerShell 5.1+ shell and run:

```powershell
.\Reclaim-SengledHub.ps1 -Hub 192.168.1.42
```

The parameters are:

| Parameter | Actual behavior |
| --- | --- |
| `-Hub <IPv4>` | Uses the supplied IPv4 address instead of prompting. It must be reachable on TCP/8686 or TCP/23. |
| `-DryRun` | Performs the compatibility check described above and returns before confirmation, coordinator work, backups, builds, or flashes. |
| `-BackupOnly` | Validates the hub and creates the complete read-only RTL flash backup, then exits before coordinator work, builds, flash writes, or reboot. |
| `-BootTestOnly` | Checks only that reclaimed Bank2 is active and TCP/6638 is reachable. Performs no backup, build, flash write, or reboot. |
| `-SkipCoordinator` | Skips both the EZSP v7 probe and coordinator reflash. The RTL backup/build/Bank2 flash still proceeds. |
| `-ForceCoordinator` | After probing, reflashes the coordinator even if it already reports EZSP v7 and requires the explicit `REFLASH` confirmation. Has no effect when combined with `-SkipCoordinator`. |
| `-NoReboot` | Completes and verifies both Bank2 writes, then exits with Bank1 still running. It does not perform the post-reboot Bank2 or TCP/6638 health checks. |
| `-KeepWork` | Preserves the run's `tftp\` staging directory. Build intermediates and logs are retained regardless. |
| `-TftpPort <1024-65535>` | Changes the built-in TFTP server and temporary UDP firewall-rule port from the default `6969`. |

All direct PowerShell invocations still require an elevated shell. These switches are primarily diagnostic/development controls; in particular, skipping the coordinator can leave the reclaimed gateway paired with an incompatible NCP.

## Package checksum manifest

`SHA256SUMS.txt` covers every packaged file except itself and `Generate-SHA256SUMS.ps1`. Runtime output, downloaded cache files, and locally assembled release artifacts are excluded; `cache\README.txt` remains covered. To regenerate it after changing the package, run the generator from the extracted package directory:

```powershell
.\Generate-SHA256SUMS.ps1
```

## Release-candidate status

The full Windows workflow was exercised on a physical hub through coordinator reflash, complete flash backup, per-device rootfs rebuild, verified Bank2 writes, cold Bank2 boot, and a live TCP/6638 EZSP gateway health check. This remains a release candidate because the exact hardware/layout gates have so far been validated on a limited number of units; keep first runs supervised and retain the generated Bank1 backup.

## Third-party projects

`squashfs-tools-ng` is by David Oberhollenzer and contributors and is GPLv3+. Its project documents official prebuilt Windows packages. The coordinator image is obtained from the public `walthowd/husbzb-firmware` repository at runtime. No Sengled stock firmware image is included in this package.

## License

The original code and documentation in this repository are licensed under the
[MIT License](LICENSE). Runtime-downloaded third-party components remain under
their respective upstream licenses; see [THIRD-PARTY.md](THIRD-PARTY.md).
