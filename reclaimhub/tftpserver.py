"""
tftpserver - minimal octet-mode TFTP server.

Serves RRQ
(GET) and WRQ (PUT) in octet mode using 512-byte blocks. PUT writes to
<name>.part then atomically renames to <name> on completion, matching the
controller's expectation (it waits for the final file, not the .part).
"""
import os
import socket
import threading
import time


class TftpServer:
    def __init__(self, root, port=6969, bind="0.0.0.0"):
        self.root = os.path.abspath(root)
        self.port = port
        self.bind = bind
        self._udp = None
        self._thread = None
        self._running = False
        os.makedirs(self.root, exist_ok=True)

    def start(self):
        if self._running:
            return
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind((self.bind, self.port))
        self._udp.settimeout(1.2)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="reclaimhub-tftp")
        self._thread.start()
        print("[TFTP] serving %s on UDP/%s" % (self.root, self.port))

    def stop(self):
        if not self._running:
            return
        self._running = False
        try:
            self._udp.close()
        except Exception:
            pass
        self._udp = None
        self._thread = None

    # ---- helpers ------------------------------------------------------
    @staticmethod
    def _u16(p, off):
        return (p[off] << 8) | p[off + 1]

    @staticmethod
    def _packet(op, block, data=b""):
        p = bytearray(4 + len(data))
        p[0] = (op >> 8) & 0xFF
        p[1] = op & 0xFF
        p[2] = (block >> 8) & 0xFF
        p[3] = block & 0xFF
        p[4:] = data
        return bytes(p)

    @staticmethod
    def _read_z(p, off):
        start = off
        while off < len(p) and p[off] != 0:
            off += 1
        return p[start:off].decode("ascii", "replace")

    def _safe_path(self, requested):
        name = requested.replace("\\", "/").split("/")[-1]
        if not name or name in (".", ".."):
            raise OSError("Bad TFTP filename")
        return os.path.join(self.root, name)

    def _send_error(self, sock, peer, code, message):
        m = message.encode()
        p = bytearray(4 + len(m) + 1)
        p[1] = 5
        p[2] = (code >> 8) & 0xFF
        p[3] = code & 0xFF
        p[4:] = m
        sock.sendto(bytes(p), peer)

    # ---- request handlers ---------------------------------------------
    def _handle_read(self, sock, peer, name):
        path = self._safe_path(name)
        if not os.path.isfile(path):
            self._send_error(sock, peer, 1, "File not found")
            print("[TFTP] RRQ missing: " + name)
            return
        print("[TFTP] GET " + name)
        block = 1
        with open(path, "rb") as f:
            while True:
                data = f.read(512)
                pkt = self._packet(3, block, data)
                acked = False
                for _ in range(12):
                    sock.sendto(pkt, peer)
                    end = time.time() + 1.2
                    while time.time() < end:
                        try:
                            r, src = sock.recvfrom(2048)
                        except socket.timeout:
                            break
                        if self._same_peer(src, peer) and len(r) >= 4 \
                                and self._u16(r, 0) == 4 and self._u16(r, 2) == block:
                            acked = True
                            break
                        if time.time() >= end:
                            break
                    if acked:
                        break
                if not acked:
                    raise OSError("TFTP GET timed out waiting for ACK block %d" % block)
                if len(data) < 512:
                    break
                block += 1
        print("[TFTP] GET complete " + name)

    def _handle_write(self, sock, peer, name):
        final = self._safe_path(name)
        part = final + ".part"
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        print("[TFTP] PUT " + name)
        expected = 1
        last_ack = 0
        ack = self._packet(4, 0)
        sock.sendto(ack, peer)
        timeouts = 0
        with open(part, "wb") as f:
            while True:
                try:
                    r, src = sock.recvfrom(2048)
                except socket.timeout:
                    if timeouts > 15:
                        raise OSError("TFTP PUT timed out at block %d" % expected)
                    timeouts += 1
                    sock.sendto(ack, peer)
                    continue
                if not self._same_peer(src, peer):
                    continue
                if len(r) < 4 or self._u16(r, 0) != 3:
                    continue
                block = self._u16(r, 2)
                if block == expected:
                    count = len(r) - 4
                    if count > 0:
                        f.write(r[4:])
                    last_ack = block
                    ack = self._packet(4, block)
                    sock.sendto(ack, peer)
                    expected += 1
                    timeouts = 0
                    if count < 512:
                        break
                elif block == last_ack:
                    sock.sendto(ack, peer)
        os.replace(part, final)
        print("[TFTP] PUT complete " + name)

    @staticmethod
    def _same_peer(a, b):
        return a is not None and b is not None and a[1] == b[1] and a[0] == b[0]

    # ---- main loop ----------------------------------------------------
    def _run(self):
        sock = self._udp
        while self._running:
            try:
                r, peer = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    print("[TFTP] socket error")
                continue
            if len(r) < 4:
                continue
            op = self._u16(r, 0)
            if op not in (1, 2):
                continue
            off = 2
            name = self._read_z(r, off)
            name_end = r.index(0, off)
            mode = self._read_z(r, name_end + 1)
            if mode.lower() != "octet":
                self._send_error(sock, peer, 0, "Only octet mode supported")
                continue
            try:
                if op == 1:
                    self._handle_read(sock, peer, name)
                else:
                    self._handle_write(sock, peer, name)
            except OSError as e:
                print("[TFTP] transfer error: %s" % e)
