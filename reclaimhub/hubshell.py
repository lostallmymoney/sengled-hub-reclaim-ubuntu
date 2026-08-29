"""
HubShell - telnet command shell driver for the Sengled Element Hub.

Handles:
  - telnet IAC negotiation parsing (split-across-read tolerant)
  - stock admin/admin login
  - marker-based command completion + exit code capture
"""
import socket
import time
import re

VERB_DONT = 254
VERB_DO = 253
VERB_WONT = 252
VERB_WILL = 251
IAC = 255
SB = 250
SE = 240
OPT_ECHO = 1
OPT_SGA = 3


class ShellError(Exception):
    pass


class ShellTimeout(ShellError):
    pass


class ShellResult:
    def __init__(self, output, exit_code):
        self.output = output
        self.exit_code = exit_code

    def __repr__(self):
        return "ShellResult(rc=%s, out=%r)" % (self.exit_code, self.output)


class HubShell:
    def __init__(self, host, port=23):
        self.host = host
        self.port = port
        self._sock = None
        self._state = 0       # 0=data, 1=after IAC, 2=wait option, 3=in SB, 4=IAC in SB
        self._neg = 0
        self._replies = b""  # bytes to send back (IAC negotiation answers)

    # ---- connection ---------------------------------------------------
    def connect(self, timeout_ms=5000):
        try:
            self._sock = socket.create_connection((self.host, self.port),
                                                  timeout=timeout_ms / 1000.0)
        except OSError as e:
            raise ShellError("Telnet connect failed: %s" % e)
        self._sock.settimeout(0.25)
        try:
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        self._state = 0
        self._neg = 0
        self._replies = b""
        time.sleep(0.15)
        initial = self._read_until(["login:", "username:", "password:"],
                                   min_timeout=1200, max_timeout=4000)
        self._authenticate(initial)

    def _authenticate(self, initial):
        seen = (initial or "").lower()
        wants_user = "login:" in seen or "username:" in seen
        wants_pass = "password:" in seen
        if not wants_user and not wants_pass:
            return   # already a direct shell
        if wants_user:
            self._send(b"admin\r\n")
            seen = self._read_until(["password:", "login:", "username:"],
                                    min_timeout=1200, max_timeout=4000).lower()
            if "login:" in seen or "username:" in seen:
                raise ShellError("Telnet username rejected by the hub")
            if "password:" not in seen:
                raise ShellTimeout("Telnet did not present a password prompt after username")
        self._send(b"admin\r\n")
        after = self._read_until(["login incorrect", "authentication failure",
                                  "login:", "username:", "password:"],
                                 min_timeout=1000, max_timeout=1800).lower()
        if any(k in after for k in ["login incorrect", "authentication failure",
                                    "login:", "username:", "password:"]):
            raise ShellError("Telnet login failed for stock user admin")

    # ---- low-level ----------------------------------------------------
    def _send(self, data):
        try:
            self._sock.sendall(data)
        except OSError as e:
            raise ShellError("Telnet write failed: %s" % e)

    def send_raw_line(self, line):
        self._send((line + "\r\n").encode())

    def _feed(self, data):
        """Run raw bytes through the IAC state machine; return plaintext and
        queue negotiation replies to be flushed."""
        plain = []
        for b in data:
            if self._state == 0:
                if b == IAC:
                    self._state = 1
                elif b != 0:
                    plain.append(b)
            elif self._state == 1:
                if b == IAC:
                    plain.append(IAC)
                    self._state = 0
                elif b in (VERB_WILL, VERB_WONT, VERB_DO, VERB_DONT):
                    self._neg = b
                    self._state = 2
                elif b == SB:
                    self._state = 3
                else:
                    self._state = 0
            elif self._state == 2:
                self._handle_negotiation(self._neg, b)
                self._neg = 0
                self._state = 0
            elif self._state == 3:
                if b == IAC:
                    self._state = 4
            elif self._state == 4:
                self._state = 3 if b != SE else 0
        return bytes(plain)

    def _handle_negotiation(self, command, option):
        if command == VERB_WILL:
            ans = VERB_DO if option in (OPT_ECHO, OPT_SGA) else VERB_DONT
        elif command == VERB_DO:
            ans = VERB_WILL if option == OPT_SGA else VERB_WONT
        else:
            return
        self._replies += bytes([IAC, ans, option])

    def _read_available(self, deadline):
        """Read until no more data is buffered or deadline. Return plaintext."""
        acc = bytearray()
        while time.time() < deadline:
            try:
                data = self._sock.recv(8192)
            except (socket.timeout, TimeoutError):
                pass
            except OSError:
                raise ShellError("Telnet connection closed")
            if data:
                plain = self._feed(data)
                if plain:
                    acc += plain
                if self._replies:
                    try:
                        self._sock.sendall(self._replies)
                    except OSError:
                        pass
                    self._replies = b""
            else:
                raise ShellError("Telnet connection closed by remote host")
            # keep draining while data available; small sleep to coalesce
            if self._sock.gettimeout() and not acc:
                # got at least one receive cycle
                break
        return bytes(acc)

    def _read_until(self, needles, min_timeout, max_timeout):
        """Return text (with IAC stripped) accumulated until a needle appears
        or timeout elapses."""
        lo = [n.lower() for n in needles]
        acc = bytearray()
        end = time.time() + max_timeout / 1000.0
        while time.time() < end:
            try:
                data = self._sock.recv(4096)
            except (socket.timeout, TimeoutError):
                text = bytes(acc).decode("utf-8", "replace").lower()
                for n in lo:
                    if n in text:
                        return bytes(acc).decode("utf-8", "replace")
                continue
            except OSError:
                raise ShellError("Telnet connection closed")
            if data:
                plain = self._feed(data)
                if plain:
                    acc += plain
                    text = bytes(acc).decode("utf-8", "replace").lower()
                    for n in lo:
                        if n in text:
                            return bytes(acc).decode("utf-8", "replace")
                if self._replies:
                    try:
                        self._sock.sendall(self._replies)
                    except OSError:
                        pass
                    self._replies = b""
            else:
                raise ShellError("Telnet connection closed by remote host")
        return bytes(acc).decode("utf-8", "replace")

    # ---- command execution -------------------------------------------
    def run(self, command, timeout_ms):
        if not self._sock:
            raise ShellError("Not connected")
        marker = "__SENGLED_RC_%s__" % id(command)
        wire = command + "; __sr_rc=$?; echo " + marker + "${__sr_rc}\r\n"
        self._send(wire.encode())
        rx = re.compile(re.escape(marker) + r"(\d+)")
        acc = bytearray()
        end = time.time() + timeout_ms / 1000.0
        while time.time() < end:
            try:
                data = self._sock.recv(8192)
            except (socket.timeout, TimeoutError):
                data = b""
            except OSError:
                raise ShellError("Telnet connection closed")
            if data:
                plain = self._feed(data)
                if plain:
                    acc += plain
                if self._replies:
                    try:
                        self._sock.sendall(self._replies)
                    except OSError:
                        pass
                    self._replies = b""
            if acc:
                text = bytes(acc).decode("utf-8", "replace")
                m = rx.search(text)
                if m:
                    code = int(m.group(1))
                    output = text[: m.start()]
                    lines = [l for l in output.split("\n") if marker not in l]
                    return ShellResult("\n".join(lines).strip(), code)
            else:
                time.sleep(0.02)
        raise ShellTimeout("Timed out waiting for hub command: %s. Last: %r"
                           % (command, bytes(acc).decode("utf-8", "replace")[-500:]))

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
