"""
tarpatcher - metadata-preserving tar manipulation.

Reads the
stock rootfs SquashFS converted to a tar stream, patches /etc/init.d/rcS,
removes the entries that will be replaced, and appends the reclaimed
binaries/scripts/marker. No device nodes ever need to exist on the local
filesystem.
"""
import os

BLOCK = 512

# Entries the stock tar provides that we replace from our payload.
REPLACE = [
    "bin/ezsp_gateway",
    "bin/hub-chmodx",
    "bin/ezsp_start.sh",
    "bin/reclaim-status",
    "etc/reclaim-build.txt",
]


class TarError(Exception):
    pass


def _is_zero(b):
    return all(x == 0 for x in b)


def _field_string(h, off, length):
    n = 0
    while n < length and h[off + n] != 0:
        n += 1
    return h[off:off + n].decode("ascii", "replace").strip()


def _octal(h, off, length):
    s = _field_string(h, off, length).strip("\0 ")
    if not s:
        return 0
    v = 0
    for c in s:
        if '0' <= c <= '7':
            v = (v << 3) + (ord(c) - ord('0'))
    return v


def _name(h):
    n = _field_string(h, 0, 100)
    p = _field_string(h, 345, 155)
    return p + "/" + n if p else n


def _norm(s):
    s = s.replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    while s.startswith("/"):
        s = s[1:]
    return s.rstrip("/")


def _count(haystack, needle):
    return haystack.count(needle)


def _put_octal(h, off, length, value):
    s = "%o" % value
    if len(s) > length - 1:
        raise TarError("Tar numeric field overflow")
    s = s.rjust(length - 1, "0") + "\0"
    b = s.encode("ascii")
    h[off:off + length] = b[:length]


def _fix_checksum(h):
    h[148:156] = b"        "
    total = sum(h)
    s = "%o" % total
    s = s.rjust(6, "0") + "\0 "
    h[148:156] = s.encode("ascii")


def _write_padded(dst, data):
    dst.write(data)
    pad = (BLOCK - (len(data) % BLOCK)) % BLOCK
    if pad:
        dst.write(b"\0" * pad)


def _header(name, mode, size, mtime):
    h = bytearray(BLOCK)
    nb = name.encode("ascii")
    if len(nb) > 100:
        raise TarError("Tar path too long: " + name)
    h[0:len(nb)] = nb
    _put_octal(h, 100, 8, mode)
    _put_octal(h, 108, 8, 0)
    _put_octal(h, 116, 8, 0)
    _put_octal(h, 124, 12, size)
    _put_octal(h, 136, 12, mtime)
    h[156] = ord('0')
    h[257:263] = b"ustar\0"
    h[263] = ord('0'); h[264] = ord('0')
    h[265:269] = b"root"
    h[297:301] = b"root"
    _put_octal(h, 329, 8, 0)
    _put_octal(h, 337, 8, 0)
    _fix_checksum(h)
    return bytes(h)


def _add_file(dst, name, data, mode, mtime):
    h = _header(name, mode, len(data), mtime)
    dst.write(h)
    _write_padded(dst, data)


def _patch_rcs(data):
    s = data.decode("ascii", "replace")
    a = "iptables -A INPUT -p tcp --dport 80 -j DROP"
    b = "#telnetd&"
    c = "sengled_startup&"
    d = "#sengled_gateway_app&"
    e = "\nboa\n"
    web_count = _count(s, a)
    if web_count > 1 or _count(s, b) != 1 or _count(s, c) != 1 \
            or _count(s, d) != 1 or _count(s, e) != 1:
        raise TarError("Stock rcS did not match the known Sengled layout; refusing to build a flash image.")
    if web_count == 1:
        s = s.replace(a, "# RECLAIM: stock TCP/80 DROP disabled; Boa remains reachable on trusted LAN")
    s = s.replace(b, "telnetd&")
    s = s.replace(c, "# RECLAIM: stock sengled_startup disabled\n/bin/sh /bin/ezsp_start.sh &")
    return s.encode("ascii")


def patch(input_tar, output_tar, gateway, chmodx, start_script,
          build_text, status_text):
    saw_rcs = False
    with open(input_tar, "rb") as src, open(output_tar, "wb") as dst:
        while True:
            h = src.read(BLOCK)
            if len(h) < BLOCK:
                if len(h) == 0:
                    break
                raise TarError("Truncated tar header")
            if _is_zero(h):
                break
            size = _octal(h, 124, 12)
            padded = ((size + BLOCK - 1) // BLOCK) * BLOCK
            payload = src.read(padded)
            if len(payload) < padded:
                raise TarError("Truncated tar payload")
            norm = _norm(_name(h))
            skip = norm in REPLACE

            if norm == "etc/init.d/rcS":
                if saw_rcs:
                    raise TarError("Multiple rcS entries in stock rootfs tar")
                saw_rcs = True
                original = payload[:size]
                patched = _patch_rcs(original)
                nh = bytearray(h)
                _put_octal(nh, 124, 12, len(patched))
                _fix_checksum(nh)
                dst.write(bytes(nh))
                _write_padded(dst, patched)
            elif not skip:
                dst.write(h)
                if padded:
                    dst.write(payload)
        if not saw_rcs:
            raise TarError("etc/init.d/rcS not found in stock rootfs")
        now = int(os.path.getmtime(input_tar))
        _add_file(dst, "./bin/ezsp_gateway", open(gateway, "rb").read(), 0o755, now)
        _add_file(dst, "./bin/hub-chmodx", open(chmodx, "rb").read(), 0o755, now)
        _add_file(dst, "./bin/ezsp_start.sh", open(start_script, "rb").read(), 0o755, now)
        _add_file(dst, "./bin/reclaim-status", status_text.replace("\r\n", "\n").encode(), 0o755, now)
        _add_file(dst, "./etc/reclaim-build.txt", build_text.replace("\r\n", "\n").encode(), 0o644, now)
        dst.write(b"\0" * (BLOCK * 2))
        dst.flush()
