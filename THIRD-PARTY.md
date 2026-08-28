# Third-party components downloaded at runtime

The repository's original code is covered by the MIT License in `LICENSE`.
That license does not replace or alter the licenses of the components below.

## squashfs-tools-ng 1.3.2 (Windows x64)

Project: https://github.com/AgentD/squashfs-tools-ng
Official Windows archive: https://infraroot.at/pub/squashfs/windows/squashfs-tools-ng-1.3.2-mingw64.zip
License: GPLv3+ (see upstream project/package).

The reclaim ZIP does not contain this third-party binary package. When the image-build stage cannot find the required executables in `cache\`, the controller downloads and extracts the archive there for reuse. The current script checks that `sqfs2tar.exe` and `tar2sqfs.exe` exist after extraction but does not verify the archive against a pinned hash.

## EM357 EmberZNet 6.4.1 / EZSP v7 image

Runtime download:
https://raw.githubusercontent.com/walthowd/husbzb-firmware/master/em357-v641-ncp-uart-sw.ebl

Expected length: 146816 bytes
Expected Git blob SHA-1: 361738c5116a97e7d755df46d6bcc31e167038fd

The reclaim ZIP does not contain this firmware image. The controller downloads it only when the live EZSP probe indicates a flash is needed or `-ForceCoordinator` is supplied. It verifies the exact length and Git blob SHA-1 before use and keeps the download in that run's output directory rather than `cache\`. Consult the upstream repository for provenance and licensing information.
