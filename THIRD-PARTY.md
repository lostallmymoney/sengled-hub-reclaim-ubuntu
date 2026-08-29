# Third-party components

The repository's original code is covered by the MIT License in `LICENSE`.
That license does not replace or alter the licenses of the components below.

## squashfs-tools-ng (Ubuntu package)

Project: https://github.com/AgentD/squashfs-tools-ng
License: GPLv3+ (see the project/package).

On Ubuntu, this is provided by the distro package `squashfs-tools-ng`
(`sqfs2tar`, `tar2sqfs`). It is only needed for the Bank1-active flow that
rebuilds images from a live backup; it is **not** vendored in this repo.

## EM357 EmberZNet 6.4.1 / EZSP v7 image

Runtime download (only when a coordinator flash is needed or `--force-coordinator`
is passed to `runReclaim.py`):
https://raw.githubusercontent.com/walthowd/husbzb-firmware/master/em357-v641-ncp-uart-sw.ebl

Expected length: 146816 bytes
Expected Git blob SHA-1: 361738c5116a97e7d755df46d6bcc31e167038fd

The controller verifies the exact length and Git blob SHA-1 before use and
keeps the download in that run's output directory. Consult the upstream
repository for provenance and licensing information.