"""
imagebuilder - build and verify reclaimed Bank2 kernel + rootfs images.

Produces:
  - a 0x2D0000-byte rootfs image: raw SquashFS wrapped with the Realtek
    aligned-padding + big-endian 16-bit checksum convention;
  - a 0x130000-byte kernel image preserving the Bank2 header prefix and
    carrying the new 0xFFFFFFF1 bank mark.
"""
import os

KERNEL_PART_SIZE = 0x130000
ROOTFS_PART_SIZE = 0x2D0000
KERNEL_HEADER_OFFSET = 0x30000
NEW_BANK_MARK = 0xFFFFFFF1


class ImageError(Exception):
    pass


def _u16le(b, o):
    return b[o] | (b[o + 1] << 8)


def _u32le(b, o):
    return b[o] | (b[o + 1] << 8) | (b[o + 2] << 16) | (b[o + 3] << 24)


def _u64le(b, o):
    v = 0
    for i in range(7, -1, -1):
        v = (v << 8) | b[o + i]
    return v


def _u32be(b, o):
    return (b[o] << 24) | (b[o + 1] << 16) | (b[o + 2] << 8) | b[o + 3]


def _put_u32be(b, o, v):
    b[o] = (v >> 24) & 0xFF
    b[o + 1] = (v >> 16) & 0xFF
    b[o + 2] = (v >> 8) & 0xFF
    b[o + 3] = v & 0xFF


def _sum_be16(b, off, length):
    total = 0
    end = off + length
    i = off
    while i < end:
        w = b[i] << 8
        if i + 1 < end:
            w |= b[i + 1]
        total = (total + w) & 0xFFFF
        i += 2
    return total


def _sig(b, o, s):
    x = s.encode("ascii")
    if o + len(x) > len(b):
        return False
    return b[o:o + len(x)] == x


def _all_ff(b, off):
    return all(x == 0xFF for x in b[off:])


def build_kernel(bank1_path, bank2_path, output_path):
    b1 = open(bank1_path, "rb").read()
    b2 = open(bank2_path, "rb").read()
    if len(b1) != KERNEL_PART_SIZE or len(b2) != KERNEL_PART_SIZE:
        raise ImageError("Both kernel partitions must be exactly 0x130000 bytes")
    if not _sig(b1, KERNEL_HEADER_OFFSET, "cr6c") or not _sig(b2, KERNEL_HEADER_OFFSET, "cr6c"):
        raise ImageError("Expected cr6c headers at 0x30000 in both kernel partitions")
    start1 = _u32be(b1, KERNEL_HEADER_OFFSET + 4)
    mark1 = _u32be(b1, KERNEL_HEADER_OFFSET + 8)
    len1 = _u32be(b1, KERNEL_HEADER_OFFSET + 12)
    mark2 = _u32be(b2, KERNEL_HEADER_OFFSET + 8)
    if mark1 != 0xFFFFFFF0:
        raise ImageError("Unexpected Bank1 mark 0x%08X; public v1 only supports 0xFFFFFFF0" % mark1)
    if mark2 >= NEW_BANK_MARK:
        raise ImageError("Unexpected Bank2 mark 0x%08X; refusing to overwrite an equal/newer bank" % mark2)
    if KERNEL_HEADER_OFFSET + 16 + len1 > len(b1):
        raise ImageError("Bank1 kernel payload length out of range")
    if _sum_be16(b1, KERNEL_HEADER_OFFSET + 16, len1) != 0:
        raise ImageError("Bank1 kernel payload checksum is not zero")

    output = bytearray(KERNEL_PART_SIZE)
    output[0:KERNEL_HEADER_OFFSET] = b2[0:KERNEL_HEADER_OFFSET]
    output[KERNEL_HEADER_OFFSET:] = b1[KERNEL_HEADER_OFFSET:]
    _put_u32be(output, KERNEL_HEADER_OFFSET + 8, NEW_BANK_MARK)
    out_len = _u32be(output, KERNEL_HEADER_OFFSET + 12)
    if _sum_be16(bytes(output), KERNEL_HEADER_OFFSET + 16, out_len) != 0:
        raise ImageError("Output kernel checksum failed")
    open(output_path, "wb").write(bytes(output))
    return ("Bank2 kernel OK: bank1 mark=0x%08X, old bank2 mark=0x%08X, "
            "new mark=0x%08X, load=0x%08X, payload=%d"
            % (mark1, mark2, NEW_BANK_MARK, start1, out_len))


def wrap_rootfs(raw_sqfs_path, output_path):
    raw = open(raw_sqfs_path, "rb").read()
    if len(raw) < 96 or not _sig(raw, 0, "hsqs"):
        raise ImageError("Raw filesystem is not little-endian SquashFS")
    block_size = _u32le(raw, 12)
    comp = _u16le(raw, 20)
    major = _u16le(raw, 28)
    minor = _u16le(raw, 30)
    bytes_used64 = _u64le(raw, 40)
    if major != 4 or minor != 0:
        raise ImageError("Expected SquashFS 4.0")
    if comp != 2:
        raise ImageError("Expected LZMA compression id 2")
    if block_size != 131072:
        raise ImageError("Expected 131072-byte SquashFS block size")
    if bytes_used64 > len(raw) or bytes_used64 > 0x7FFFFFFF:
        raise ImageError("SquashFS bytes_used out of range")
    bytes_used = int(bytes_used64)
    aligned = (bytes_used + 0xFFF) & ~0xFFF
    check_end = aligned + 2
    if check_end > ROOTFS_PART_SIZE:
        raise ImageError("Rebuilt rootfs does not fit Bank2 partition")
    rtk_field = aligned - 640

    output = bytearray([0xFF]) * ROOTFS_PART_SIZE
    output[0:bytes_used] = raw[0:bytes_used]
    _put_u32be(output, 8, rtk_field)
    for i in range(bytes_used, aligned):
        output[i] = 0
    total = _sum_be16(bytes(output), 0, aligned)
    ck = (0x10000 - total) & 0xFFFF
    output[aligned] = (ck >> 8) & 0xFF
    output[aligned + 1] = ck & 0xFF
    if _sum_be16(bytes(output), 0, check_end) != 0:
        raise ImageError("Internal Realtek rootfs checksum verification failed")
    open(output_path, "wb").write(bytes(output))
    return ("Rootfs wrapper OK: bytes_used=%d (0x%X), check_end=0x%X, "
            "RTK field=0x%08X, checksum=0x%04X"
            % (bytes_used, bytes_used, check_end, rtk_field, ck))


def verify(kernel_path, rootfs_path):
    k = open(kernel_path, "rb").read()
    r = open(rootfs_path, "rb").read()
    if len(k) != KERNEL_PART_SIZE:
        raise ImageError("Kernel output size mismatch")
    if len(r) != ROOTFS_PART_SIZE:
        raise ImageError("Rootfs output size mismatch")
    if not _sig(k, KERNEL_HEADER_OFFSET, "cr6c"):
        raise ImageError("Bad output kernel signature")
    mark = _u32be(k, KERNEL_HEADER_OFFSET + 8)
    length = _u32be(k, KERNEL_HEADER_OFFSET + 12)
    if mark != NEW_BANK_MARK:
        raise ImageError("Bad output Bank2 mark")
    if KERNEL_HEADER_OFFSET + 16 + length > len(k) or \
            _sum_be16(k, KERNEL_HEADER_OFFSET + 16, length) != 0:
        raise ImageError("Bad output kernel payload checksum")
    if not _sig(r, 0, "hsqs") or _u32le(r, 12) != 131072 or _u16le(r, 20) != 2 \
            or _u16le(r, 28) != 4 or _u16le(r, 30) != 0:
        raise ImageError("Bad output SquashFS header")
    bytes_used = _u64le(r, 40)
    field = _u32be(r, 8)
    check_len64 = field + 640 + 2
    if check_len64 > len(r) or check_len64 > 0x7FFFFFFF:
        raise ImageError("Realtek rootfs checked length out of range")
    check_len = int(check_len64)
    if _sum_be16(r, 0, check_len) != 0:
        raise ImageError("Realtek rootfs checksum failed")
    if not _all_ff(r, check_len):
        raise ImageError("Rootfs partition tail not all 0xFF")
    return ("VERIFY PASS: kernel mark=0x%08X, kernel checksum=0; "
            "SquashFS 4.0 LZMA block=131072 bytes_used=%d; Realtek checksum=0"
            % (mark, bytes_used))


def sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
