using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;

namespace SengledReclaim
{
    public sealed class ShellResult
    {
        public string Output { get; set; }
        public int ExitCode { get; set; }
    }

    public sealed class TelnetShell : IDisposable
    {
        private TcpClient client;
        private NetworkStream stream;
        private readonly string host;
        private readonly int port;

        // Telnet protocol bytes.
        private const byte IAC  = 255;
        private const byte DONT = 254;
        private const byte DO   = 253;
        private const byte WONT = 252;
        private const byte WILL = 251;
        private const byte SB   = 250;
        private const byte SE   = 240;

        private const byte OPT_ECHO = 1;
        private const byte OPT_SGA  = 3;

        // Stock Sengled telnet credentials used by the TCP/8686-started telnetd.
        private const string STOCK_USER = "admin";
        private const string STOCK_PASSWORD = "admin";

        // Stateful parser so IAC sequences may be split across TCP reads.
        // 0=data, 1=after IAC, 2=waiting for option, 3=subnegotiation,
        // 4=IAC seen inside subnegotiation.
        private int telnetState;
        private byte telnetNegCommand;

        public TelnetShell(string host, int port)
        {
            this.host = host;
            this.port = port;
        }

        public void Connect(int timeoutMs)
        {
            Dispose();

            client = new TcpClient();
            IAsyncResult ar = client.BeginConnect(host, port, null, null);
            if (!ar.AsyncWaitHandle.WaitOne(timeoutMs, false))
            {
                client.Close();
                client = null;
                throw new IOException("Timed out connecting to telnet " + host + ":" + port);
            }

            try
            {
                client.EndConnect(ar);
                client.NoDelay = true;
                stream = client.GetStream();
                stream.ReadTimeout = 250;
                stream.WriteTimeout = 5000;

                telnetState = 0;
                telnetNegCommand = 0;

                // The stock telnetd launched through TCP/8686 runs a login
                // program. Preserve and inspect the initial banner instead of
                // draining/discarding it, otherwise the first shell command
                // can accidentally be consumed as the username.
                Thread.Sleep(150);

                int authTimeout = timeoutMs;
                if (authTimeout < 1500) authTimeout = 1500;
                if (authTimeout > 4000) authTimeout = 4000;

                string initial = ReadUntilAny(
                    authTimeout,
                    "login:",
                    "username:",
                    "password:"
                );

                AuthenticateStockLogin(initial, authTimeout);
            }
            catch
            {
                Dispose();
                throw;
            }
        }

        private static bool ContainsIgnoreCase(string text, string value)
        {
            if (String.IsNullOrEmpty(text) || String.IsNullOrEmpty(value)) return false;
            return text.IndexOf(value, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private string ReadUntilAny(int timeoutMs, params string[] needles)
        {
            DateTime until = DateTime.UtcNow.AddMilliseconds(timeoutMs);
            StringBuilder sb = new StringBuilder();
            byte[] buf = new byte[4096];

            while (DateTime.UtcNow < until)
            {
                if (stream == null) throw new InvalidOperationException("Not connected");

                if (stream.DataAvailable)
                {
                    int n;
                    try
                    {
                        n = stream.Read(buf, 0, buf.Length);
                    }
                    catch (IOException ex)
                    {
                        if (SocketLooksClosed())
                            throw new IOException("Telnet connection closed while waiting for login prompt", ex);

                        string msg = ex.Message == null ? "" : ex.Message.ToLowerInvariant();
                        if (msg.IndexOf("timed out") >= 0 || msg.IndexOf("timeout") >= 0)
                            continue;
                        throw;
                    }

                    if (n <= 0)
                        throw new IOException("Telnet connection closed while waiting for login prompt");

                    sb.Append(ProcessTelnet(buf, n));
                    string current = sb.ToString();

                    for (int i = 0; i < needles.Length; i++)
                    {
                        if (ContainsIgnoreCase(current, needles[i]))
                            return current;
                    }
                }
                else
                {
                    if (SocketLooksClosed())
                        throw new IOException("Telnet connection closed by remote host while waiting for login prompt");

                    Thread.Sleep(20);
                }
            }

            return sb.ToString();
        }

        private void AuthenticateStockLogin(string initial, int timeoutMs)
        {
            string seen = initial == null ? "" : initial;

            bool wantsUser =
                ContainsIgnoreCase(seen, "login:") ||
                ContainsIgnoreCase(seen, "username:");
            bool wantsPassword = ContainsIgnoreCase(seen, "password:");

            // No login prompt means this may already be a direct shell.
            // The caller's echo readiness probe will verify that case.
            if (!wantsUser && !wantsPassword)
                return;

            if (wantsUser)
            {
                Console.WriteLine("[TELNET] stock login prompt detected; authenticating as admin");
                SendBytes(Encoding.ASCII.GetBytes(STOCK_USER + "\r\n"));

                seen = ReadUntilAny(
                    timeoutMs,
                    "password:",
                    "login:",
                    "username:"
                );

                if (ContainsIgnoreCase(seen, "login:") ||
                    ContainsIgnoreCase(seen, "username:"))
                {
                    throw new IOException("Telnet username was rejected by the hub");
                }

                if (!ContainsIgnoreCase(seen, "password:"))
                {
                    throw new TimeoutException(
                        "Telnet accepted the connection but did not present a password prompt after username");
                }
            }

            SendBytes(Encoding.ASCII.GetBytes(STOCK_PASSWORD + "\r\n"));

            // Give login a moment to hand control to /bin/sh. We deliberately
            // do not require a particular shell prompt because these hubs may
            // use different PS1 strings. The caller's first Run() call proves
            // that the authenticated shell is actually command-responsive.
            string after = ReadUntilAny(
                Math.Min(timeoutMs, 1800),
                "login incorrect",
                "authentication failure",
                "login:",
                "username:",
                "password:"
            );

            if (ContainsIgnoreCase(after, "login incorrect") ||
                ContainsIgnoreCase(after, "authentication failure") ||
                ContainsIgnoreCase(after, "login:") ||
                ContainsIgnoreCase(after, "username:") ||
                ContainsIgnoreCase(after, "password:"))
            {
                throw new IOException("Telnet login failed for stock user admin");
            }

            if (SocketLooksClosed())
                throw new IOException("Telnet connection closed immediately after stock login");

            Console.WriteLine("[TELNET] stock admin login submitted successfully");
        }

        private bool SocketLooksClosed()
        {
            try
            {
                if (client == null || client.Client == null) return true;
                Socket s = client.Client;
                return s.Poll(0, SelectMode.SelectRead) && s.Available == 0;
            }
            catch
            {
                return true;
            }
        }

        private void SendBytes(byte[] b)
        {
            if (stream == null) throw new InvalidOperationException("Not connected");
            try
            {
                stream.Write(b, 0, b.Length);
                stream.Flush();
            }
            catch (Exception ex)
            {
                throw new IOException("Telnet write failed: " + ex.Message, ex);
            }
        }

        private void SendTelnetReply(byte command, byte option)
        {
            SendBytes(new byte[] { IAC, command, option });
        }

        private void HandleNegotiation(byte command, byte option)
        {
            // BusyBox telnetd normally offers server-side ECHO and
            // SUPPRESS-GO-AHEAD. Accept those two; reject everything else.
            if (command == WILL)
            {
                if (option == OPT_ECHO || option == OPT_SGA)
                    SendTelnetReply(DO, option);
                else
                    SendTelnetReply(DONT, option);
            }
            else if (command == DO)
            {
                // We can safely agree to suppress-go-ahead from our side.
                // We do not advertise terminal type, NAWS, client echo, etc.
                if (option == OPT_SGA)
                    SendTelnetReply(WILL, option);
                else
                    SendTelnetReply(WONT, option);
            }
            // WONT and DONT are acknowledgements/refusals; no reply needed.
        }

        private string ProcessTelnet(byte[] input, int count)
        {
            MemoryStream plain = new MemoryStream();

            for (int i = 0; i < count; i++)
            {
                byte b = input[i];

                if (telnetState == 0) // normal data
                {
                    if (b == IAC)
                    {
                        telnetState = 1;
                    }
                    else if (b != 0)
                    {
                        plain.WriteByte(b);
                    }
                    continue;
                }

                if (telnetState == 1) // command after IAC
                {
                    if (b == IAC)
                    {
                        plain.WriteByte(IAC);
                        telnetState = 0;
                    }
                    else if (b == WILL || b == WONT || b == DO || b == DONT)
                    {
                        telnetNegCommand = b;
                        telnetState = 2;
                    }
                    else if (b == SB)
                    {
                        telnetState = 3;
                    }
                    else
                    {
                        // Single-byte Telnet command.
                        telnetState = 0;
                    }
                    continue;
                }

                if (telnetState == 2) // option byte
                {
                    HandleNegotiation(telnetNegCommand, b);
                    telnetNegCommand = 0;
                    telnetState = 0;
                    continue;
                }

                if (telnetState == 3) // inside SB ... IAC SE
                {
                    if (b == IAC) telnetState = 4;
                    continue;
                }

                if (telnetState == 4) // IAC inside subnegotiation
                {
                    if (b == SE)
                        telnetState = 0;
                    else
                        telnetState = 3;
                    continue;
                }
            }

            return Encoding.ASCII.GetString(plain.ToArray());
        }

        private string DrainFor(int durationMs)
        {
            DateTime until = DateTime.UtcNow.AddMilliseconds(durationMs);
            StringBuilder sb = new StringBuilder();
            byte[] buf = new byte[4096];

            while (DateTime.UtcNow < until)
            {
                if (stream == null) throw new InvalidOperationException("Not connected");

                if (stream.DataAvailable)
                {
                    int n;
                    try
                    {
                        n = stream.Read(buf, 0, buf.Length);
                    }
                    catch (IOException ex)
                    {
                        if (SocketLooksClosed())
                            throw new IOException("Telnet connection closed while reading", ex);

                        string msg = ex.Message == null ? "" : ex.Message.ToLowerInvariant();
                        if (msg.IndexOf("timed out") >= 0 || msg.IndexOf("timeout") >= 0)
                            continue;
                        throw;
                    }

                    if (n <= 0)
                        throw new IOException("Telnet connection closed");

                    sb.Append(ProcessTelnet(buf, n));
                }
                else
                {
                    if (SocketLooksClosed())
                        throw new IOException("Telnet connection closed by remote host");

                    Thread.Sleep(20);
                }
            }

            return sb.ToString();
        }

        private static string TailForError(string text)
        {
            if (String.IsNullOrEmpty(text)) return "<no text received>";
            text = text.Replace("\r", "").Replace("\0", "");
            if (text.Length > 500) text = text.Substring(text.Length - 500);
            return text.Trim();
        }

        public ShellResult Run(string command, int timeoutMs)
        {
            if (stream == null) throw new InvalidOperationException("Not connected");
            if (command == null) throw new ArgumentNullException("command");

            // Clear any stale prompt/banner before sending this command.
            string preface = DrainFor(80);

            string marker = "__SENGLED_RC_" +
                Guid.NewGuid().ToString("N").Substring(0, 12) + "__";

            // ${__sr_rc} makes the variable boundary explicit. The echoed
            // command line contains marker+"${", while the real completion
            // line contains marker+digits, so it cannot satisfy the regex.
            string wire = command +
                "; __sr_rc=$?; echo " + marker + "${__sr_rc}\r\n";

            SendBytes(Encoding.ASCII.GetBytes(wire));

            DateTime until = DateTime.UtcNow.AddMilliseconds(timeoutMs);
            StringBuilder sb = new StringBuilder();
            byte[] buf = new byte[8192];
            Regex rx = new Regex(Regex.Escape(marker) + "([0-9]+)");

            while (DateTime.UtcNow < until)
            {
                if (stream.DataAvailable)
                {
                    int n;
                    try
                    {
                        n = stream.Read(buf, 0, buf.Length);
                    }
                    catch (IOException ex)
                    {
                        if (SocketLooksClosed())
                            throw new IOException(
                                "Telnet connection closed while running: " + command, ex);

                        string msg = ex.Message == null ? "" : ex.Message.ToLowerInvariant();
                        if (msg.IndexOf("timed out") >= 0 || msg.IndexOf("timeout") >= 0)
                            continue;
                        throw;
                    }

                    if (n <= 0)
                        throw new IOException("Telnet connection closed while running: " + command);

                    sb.Append(ProcessTelnet(buf, n));

                    string all = sb.ToString();
                    Match m = rx.Match(all);
                    if (m.Success)
                    {
                        int code = Int32.Parse(m.Groups[1].Value);
                        string output = all.Substring(0, m.Index).Replace("\r", "");

                        // Remove the echoed command line (it contains the
                        // unique marker followed by the literal ${...}).
                        string[] lines = output.Split(new char[] { '\n' });
                        StringBuilder clean = new StringBuilder();
                        for (int li = 0; li < lines.Length; li++)
                        {
                            if (lines[li].IndexOf(marker, StringComparison.Ordinal) >= 0)
                                continue;

                            if (clean.Length != 0) clean.Append('\n');
                            clean.Append(lines[li]);
                        }

                        return new ShellResult
                        {
                            Output = clean.ToString().Trim(),
                            ExitCode = code
                        };
                    }
                }
                else
                {
                    if (SocketLooksClosed())
                        throw new IOException(
                            "Telnet connection closed by remote host while running: " + command);

                    Thread.Sleep(20);
                }
            }

            string received = preface + sb.ToString();
            throw new TimeoutException(
                "Timed out waiting for hub command: " + command +
                ". Last received text: " + TailForError(received));
        }

        public void SendRawLine(string line)
        {
            if (line == null) line = "";
            SendBytes(Encoding.ASCII.GetBytes(line + "\r\n"));
        }

        public void Dispose()
        {
            try { if (stream != null) stream.Dispose(); } catch { }
            try { if (client != null) client.Close(); } catch { }
            stream = null;
            client = null;
            telnetState = 0;
            telnetNegCommand = 0;
        }
    }

    public sealed class TftpServer : IDisposable
    {
        private readonly string root;
        private readonly int port;
        private UdpClient udp;
        private Thread thread;
        private volatile bool running;

        public TftpServer(string root, int port)
        {
            this.root = Path.GetFullPath(root);
            this.port = port;
            Directory.CreateDirectory(this.root);
        }

        public void Start()
        {
            if (running) return;
            udp = new UdpClient(new IPEndPoint(IPAddress.Any, port));
            udp.Client.ReceiveTimeout = 1200;
            running = true;
            thread = new Thread(Run);
            thread.IsBackground = true;
            thread.Name = "SengledReclaim-TFTP";
            thread.Start();
            Console.WriteLine("[TFTP] serving " + root + " on UDP/" + port);
        }

        private static UInt16 U16(byte[] p, int off)
        {
            return (UInt16)((p[off] << 8) | p[off + 1]);
        }

        private static byte[] Packet(UInt16 op, UInt16 block, byte[] data, int count)
        {
            byte[] p = new byte[4 + count];
            p[0] = (byte)(op >> 8); p[1] = (byte)op;
            p[2] = (byte)(block >> 8); p[3] = (byte)block;
            if (count > 0) Buffer.BlockCopy(data, 0, p, 4, count);
            return p;
        }

        private static string ReadZ(byte[] p, ref int off)
        {
            int start = off;
            while (off < p.Length && p[off] != 0) off++;
            string s = Encoding.ASCII.GetString(p, start, off - start);
            if (off < p.Length) off++;
            return s;
        }

        private string SafePath(string requested)
        {
            string name = Path.GetFileName(requested.Replace('\\', '/'));
            if (String.IsNullOrEmpty(name) || name == "." || name == "..") throw new IOException("Bad TFTP filename");
            return Path.Combine(root, name);
        }

        private byte[] Receive(ref IPEndPoint remote)
        {
            return udp.Receive(ref remote);
        }

        private bool SamePeer(IPEndPoint a, IPEndPoint b)
        {
            return a != null && b != null && a.Port == b.Port && a.Address.Equals(b.Address);
        }

        private void HandleRead(IPEndPoint peer, string name)
        {
            string path = SafePath(name);
            if (!File.Exists(path))
            {
                SendError(peer, 1, "File not found");
                Console.WriteLine("[TFTP] RRQ missing: " + name);
                return;
            }
            Console.WriteLine("[TFTP] GET " + name);
            using (FileStream fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
            {
                UInt16 block = 1;
                byte[] data = new byte[512];
                while (true)
                {
                    int count = 0;
                    while (count < data.Length)
                    {
                        int n = fs.Read(data, count, data.Length - count);
                        if (n <= 0) break;
                        count += n;
                    }
                    byte[] pkt = Packet(3, block, data, count);
                    bool acked = false;
                    for (int attempt = 0; attempt < 12 && !acked; attempt++)
                    {
                        udp.Send(pkt, pkt.Length, peer);
                        DateTime until = DateTime.UtcNow.AddMilliseconds(1200);
                        while (DateTime.UtcNow < until)
                        {
                            try
                            {
                                IPEndPoint from = new IPEndPoint(IPAddress.Any, 0);
                                byte[] r = Receive(ref from);
                                if (!SamePeer(peer, from)) continue;
                                if (r.Length >= 4 && U16(r, 0) == 4 && U16(r, 2) == block) { acked = true; break; }
                            }
                            catch (SocketException ex)
                            {
                                if (ex.SocketErrorCode == SocketError.TimedOut) break;
                                throw;
                            }
                        }
                    }
                    if (!acked) throw new IOException("TFTP GET timed out waiting for ACK block " + block);
                    if (count < 512) break;
                    block++;
                }
            }
            Console.WriteLine("[TFTP] GET complete " + name);
        }

        private void HandleWrite(IPEndPoint peer, string name)
        {
            string finalPath = SafePath(name);
            string partPath = finalPath + ".part";
            try { if (File.Exists(partPath)) File.Delete(partPath); } catch { }
            Console.WriteLine("[TFTP] PUT " + name);
            using (FileStream fs = new FileStream(partPath, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                UInt16 expected = 1;
                UInt16 lastAck = 0;
                byte[] ack = Packet(4, 0, new byte[0], 0);
                udp.Send(ack, ack.Length, peer);
                int timeouts = 0;
                while (true)
                {
                    try
                    {
                        IPEndPoint from = new IPEndPoint(IPAddress.Any, 0);
                        byte[] r = Receive(ref from);
                        if (!SamePeer(peer, from)) continue;
                        if (r.Length < 4 || U16(r, 0) != 3) continue;
                        UInt16 block = U16(r, 2);
                        if (block == expected)
                        {
                            int count = r.Length - 4;
                            if (count > 0) fs.Write(r, 4, count);
                            lastAck = block;
                            ack = Packet(4, block, new byte[0], 0);
                            udp.Send(ack, ack.Length, peer);
                            expected++;
                            timeouts = 0;
                            if (count < 512) break;
                        }
                        else if (block == lastAck)
                        {
                            udp.Send(ack, ack.Length, peer);
                        }
                    }
                    catch (SocketException ex)
                    {
                        if (ex.SocketErrorCode != SocketError.TimedOut) throw;
                        if (++timeouts > 15) throw new IOException("TFTP PUT timed out at block " + expected);
                        udp.Send(ack, ack.Length, peer);
                    }
                }
                fs.Flush(true);
            }
            if (File.Exists(finalPath)) File.Delete(finalPath);
            File.Move(partPath, finalPath);
            Console.WriteLine("[TFTP] PUT complete " + name);
        }

        private void SendError(IPEndPoint peer, UInt16 code, string message)
        {
            byte[] m = Encoding.ASCII.GetBytes(message);
            byte[] p = new byte[4 + m.Length + 1];
            p[0] = 0; p[1] = 5; p[2] = (byte)(code >> 8); p[3] = (byte)code;
            Buffer.BlockCopy(m, 0, p, 4, m.Length);
            udp.Send(p, p.Length, peer);
        }

        private void Run()
        {
            while (running)
            {
                try
                {
                    IPEndPoint peer = new IPEndPoint(IPAddress.Any, 0);
                    byte[] p = Receive(ref peer);
                    if (p == null || p.Length < 4) continue;
                    UInt16 op = U16(p, 0);
                    if (op != 1 && op != 2) continue;
                    int off = 2;
                    string name = ReadZ(p, ref off);
                    string mode = ReadZ(p, ref off);
                    if (!String.Equals(mode, "octet", StringComparison.OrdinalIgnoreCase))
                    {
                        SendError(peer, 0, "Only octet mode supported");
                        continue;
                    }
                    if (op == 1) HandleRead(peer, name); else HandleWrite(peer, name);
                }
                catch (SocketException ex)
                {
                    if (!running) break;
                    if (ex.SocketErrorCode != SocketError.TimedOut) Console.WriteLine("[TFTP] socket error: " + ex.Message);
                }
                catch (ObjectDisposedException) { if (!running) break; }
                catch (Exception ex)
                {
                    Console.WriteLine("[TFTP] transfer error: " + ex.Message);
                }
            }
        }

        public void Stop()
        {
            running = false;
            try { if (udp != null) udp.Close(); } catch { }
            try { if (thread != null && thread.IsAlive) thread.Join(1500); } catch { }
            udp = null;
            thread = null;
        }

        public void Dispose() { Stop(); }
    }

    public static class TarPatcher
    {
        private const int Block = 512;

        private static bool IsZero(byte[] b)
        {
            for (int i = 0; i < b.Length; i++) if (b[i] != 0) return false;
            return true;
        }

        private static string FieldString(byte[] h, int off, int len)
        {
            int n = 0;
            while (n < len && h[off + n] != 0) n++;
            return Encoding.ASCII.GetString(h, off, n).Trim();
        }

        private static long Octal(byte[] h, int off, int len)
        {
            string s = FieldString(h, off, len).Trim('\0', ' ');
            if (s.Length == 0) return 0;
            long v = 0;
            for (int i = 0; i < s.Length; i++)
            {
                char c = s[i];
                if (c < '0' || c > '7') continue;
                v = (v << 3) + (c - '0');
            }
            return v;
        }

        private static string Name(byte[] h)
        {
            string n = FieldString(h, 0, 100);
            string p = FieldString(h, 345, 155);
            return String.IsNullOrEmpty(p) ? n : p + "/" + n;
        }

        private static string Norm(string s)
        {
            s = s.Replace('\\', '/');
            while (s.StartsWith("./", StringComparison.Ordinal)) s = s.Substring(2);
            while (s.StartsWith("/", StringComparison.Ordinal)) s = s.Substring(1);
            return s.TrimEnd('/');
        }

        private static int Count(string haystack, string needle)
        {
            int c = 0, i = 0;
            while ((i = haystack.IndexOf(needle, i, StringComparison.Ordinal)) >= 0) { c++; i += needle.Length; }
            return c;
        }

        private static byte[] PatchRcS(byte[] data)
        {
            string s = Encoding.ASCII.GetString(data);
            string a = "iptables -A INPUT -p tcp --dport 80 -j DROP";
            string b = "#telnetd&";
            string c = "sengled_startup&";
            string d = "#sengled_gateway_app&";
            string e = "\nboa\n";
            int webDropCount = Count(s, a);
            if (webDropCount > 1 || Count(s, b) != 1 || Count(s, c) != 1 ||
                Count(s, d) != 1 || Count(s, e) != 1)
                throw new InvalidDataException("Stock rcS did not match the known Sengled layout; refusing to build a flash image.");
            if (webDropCount == 1)
                s = s.Replace(a, "# RECLAIM: stock TCP/80 DROP disabled; Boa remains reachable on trusted LAN");
            s = s.Replace(b, "telnetd&");
            s = s.Replace(c, "# RECLAIM: stock sengled_startup disabled\n/bin/sh /bin/ezsp_start.sh &");
            return Encoding.ASCII.GetBytes(s);
        }

        private static void PutOctal(byte[] h, int off, int len, long value)
        {
            string s = Convert.ToString(value, 8);
            if (s.Length > len - 1) throw new InvalidDataException("Tar numeric field overflow");
            s = s.PadLeft(len - 1, '0') + "\0";
            byte[] b = Encoding.ASCII.GetBytes(s);
            Buffer.BlockCopy(b, 0, h, off, len);
        }

        private static void FixChecksum(byte[] h)
        {
            for (int i = 148; i < 156; i++) h[i] = 0x20;
            int sum = 0;
            for (int i = 0; i < h.Length; i++) sum += h[i];
            string s = Convert.ToString(sum, 8).PadLeft(6, '0') + "\0 ";
            byte[] b = Encoding.ASCII.GetBytes(s);
            Buffer.BlockCopy(b, 0, h, 148, 8);
        }

        private static void WritePadded(Stream dst, byte[] data)
        {
            dst.Write(data, 0, data.Length);
            int pad = (int)((Block - (data.Length % Block)) % Block);
            if (pad != 0) dst.Write(new byte[pad], 0, pad);
        }

        private static byte[] Header(string name, int mode, long size, long mtime)
        {
            byte[] h = new byte[Block];
            byte[] nb = Encoding.ASCII.GetBytes(name);
            if (nb.Length > 100) throw new InvalidDataException("Tar path too long: " + name);
            Buffer.BlockCopy(nb, 0, h, 0, nb.Length);
            PutOctal(h, 100, 8, mode);
            PutOctal(h, 108, 8, 0);
            PutOctal(h, 116, 8, 0);
            PutOctal(h, 124, 12, size);
            PutOctal(h, 136, 12, mtime);
            h[156] = (byte)'0';
            byte[] magic = Encoding.ASCII.GetBytes("ustar\0"); Buffer.BlockCopy(magic, 0, h, 257, magic.Length);
            h[263] = (byte)'0'; h[264] = (byte)'0';
            byte[] root = Encoding.ASCII.GetBytes("root"); Buffer.BlockCopy(root, 0, h, 265, root.Length); Buffer.BlockCopy(root, 0, h, 297, root.Length);
            PutOctal(h, 329, 8, 0); PutOctal(h, 337, 8, 0);
            FixChecksum(h);
            return h;
        }

        private static void AddFile(Stream dst, string name, byte[] data, int mode, long mtime)
        {
            byte[] h = Header(name, mode, data.Length, mtime);
            dst.Write(h, 0, h.Length);
            WritePadded(dst, data);
        }

        public static void Patch(string inputTar, string outputTar, string gateway, string chmodx, string startScript, string buildText, string statusText)
        {
            string[] replace = new string[] { "bin/ezsp_gateway", "bin/hub-chmodx", "bin/ezsp_start.sh", "bin/reclaim-status", "etc/reclaim-build.txt" };
            bool sawRcS = false;
            using (FileStream src = new FileStream(inputTar, FileMode.Open, FileAccess.Read, FileShare.Read))
            using (FileStream dst = new FileStream(outputTar, FileMode.Create, FileAccess.Write, FileShare.None))
            {
                byte[] h = new byte[Block];
                while (true)
                {
                    int got = 0;
                    while (got < Block)
                    {
                        int n = src.Read(h, got, Block - got);
                        if (n == 0) break;
                        got += n;
                    }
                    if (got == 0) break;
                    if (got != Block) throw new InvalidDataException("Truncated tar header");
                    if (IsZero(h)) break;

                    long size = Octal(h, 124, 12);
                    long padded = ((size + Block - 1) / Block) * Block;
                    if (padded > Int32.MaxValue) throw new InvalidDataException("Unexpected huge tar entry");
                    byte[] payloadPadded = new byte[(int)padded];
                    int pgot = 0;
                    while (pgot < payloadPadded.Length)
                    {
                        int n = src.Read(payloadPadded, pgot, payloadPadded.Length - pgot);
                        if (n <= 0) throw new InvalidDataException("Truncated tar payload");
                        pgot += n;
                    }
                    string norm = Norm(Name(h));
                    bool skip = false;
                    for (int i = 0; i < replace.Length; i++) if (norm == replace[i]) { skip = true; break; }

                    if (norm == "etc/init.d/rcS")
                    {
                        if (sawRcS) throw new InvalidDataException("Multiple rcS entries in stock rootfs tar");
                        sawRcS = true;
                        byte[] original = new byte[(int)size];
                        if (size > 0) Buffer.BlockCopy(payloadPadded, 0, original, 0, (int)size);
                        byte[] patched = PatchRcS(original);
                        byte[] nh = (byte[])h.Clone();
                        PutOctal(nh, 124, 12, patched.Length);
                        FixChecksum(nh);
                        dst.Write(nh, 0, nh.Length);
                        WritePadded(dst, patched);
                    }
                    else if (!skip)
                    {
                        dst.Write(h, 0, h.Length);
                        if (payloadPadded.Length > 0) dst.Write(payloadPadded, 0, payloadPadded.Length);
                    }
                }
                if (!sawRcS) throw new InvalidDataException("etc/init.d/rcS not found in stock rootfs");

                long now = (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds;
                AddFile(dst, "./bin/ezsp_gateway", File.ReadAllBytes(gateway), 493, now);
                AddFile(dst, "./bin/hub-chmodx", File.ReadAllBytes(chmodx), 493, now);
                AddFile(dst, "./bin/ezsp_start.sh", File.ReadAllBytes(startScript), 493, now);
                AddFile(dst, "./bin/reclaim-status", Encoding.ASCII.GetBytes(statusText.Replace("\r\n", "\n")), 493, now);
                AddFile(dst, "./etc/reclaim-build.txt", Encoding.ASCII.GetBytes(buildText.Replace("\r\n", "\n")), 420, now);
                dst.Write(new byte[Block * 2], 0, Block * 2);
                dst.Flush(true);
            }
        }
    }

    public static class ImageBuilder
    {
        public const int KernelPartSize = 0x130000;
        public const int RootfsPartSize = 0x2D0000;
        public const int KernelHeaderOffset = 0x30000;
        public const UInt32 NewBankMark = 0xFFFFFFF1U;

        private static UInt16 U16LE(byte[] b, int o) { return (UInt16)(b[o] | (b[o + 1] << 8)); }
        private static UInt32 U32LE(byte[] b, int o) { return (UInt32)(b[o] | (b[o+1]<<8) | (b[o+2]<<16) | (b[o+3]<<24)); }
        private static UInt64 U64LE(byte[] b, int o)
        {
            UInt64 v = 0;
            for (int i = 7; i >= 0; i--) v = (v << 8) | b[o + i];
            return v;
        }
        private static UInt32 U32BE(byte[] b, int o) { return ((UInt32)b[o]<<24) | ((UInt32)b[o+1]<<16) | ((UInt32)b[o+2]<<8) | b[o+3]; }
        private static void PutU32BE(byte[] b, int o, UInt32 v) { b[o]=(byte)(v>>24); b[o+1]=(byte)(v>>16); b[o+2]=(byte)(v>>8); b[o+3]=(byte)v; }
        private static UInt16 SumBE16(byte[] b, int off, int len)
        {
            UInt32 sum = 0;
            int end = off + len;
            int i = off;
            while (i < end)
            {
                UInt16 w = (UInt16)(b[i] << 8);
                if (i + 1 < end) w |= b[i + 1];
                sum = (sum + w) & 0xFFFFU;
                i += 2;
            }
            return (UInt16)sum;
        }
        private static bool Sig(byte[] b, int o, string s)
        {
            byte[] x = Encoding.ASCII.GetBytes(s);
            if (o + x.Length > b.Length) return false;
            for (int i=0;i<x.Length;i++) if (b[o+i]!=x[i]) return false;
            return true;
        }
        private static bool AllFF(byte[] b, int off)
        {
            for (int i = off; i < b.Length; i++) if (b[i] != 0xFF) return false;
            return true;
        }

        public static string BuildKernel(string bank1Path, string bank2Path, string outputPath)
        {
            byte[] b1 = File.ReadAllBytes(bank1Path);
            byte[] b2 = File.ReadAllBytes(bank2Path);
            if (b1.Length != KernelPartSize || b2.Length != KernelPartSize)
                throw new InvalidDataException("Both kernel partitions must be exactly 0x130000 bytes");
            if (!Sig(b1, KernelHeaderOffset, "cr6c") || !Sig(b2, KernelHeaderOffset, "cr6c"))
                throw new InvalidDataException("Expected cr6c headers at 0x30000 in both kernel partitions");
            UInt32 start1 = U32BE(b1, KernelHeaderOffset + 4);
            UInt32 mark1 = U32BE(b1, KernelHeaderOffset + 8);
            UInt32 len1 = U32BE(b1, KernelHeaderOffset + 12);
            UInt32 mark2 = U32BE(b2, KernelHeaderOffset + 8);
            if (mark1 != 0xFFFFFFF0U)
                throw new InvalidDataException(String.Format("Unexpected Bank1 mark 0x{0:X8}; public v1 only supports the proven 0xFFFFFFF0 layout", mark1));
            if (mark2 >= NewBankMark)
                throw new InvalidDataException(String.Format("Unexpected Bank2 mark 0x{0:X8}; refusing to overwrite an equal/newer bank", mark2));
            if ((UInt64)KernelHeaderOffset + 16U + len1 > (UInt64)b1.Length)
                throw new InvalidDataException("Bank1 kernel payload length is out of range");
            if (SumBE16(b1, KernelHeaderOffset + 16, (int)len1) != 0)
                throw new InvalidDataException("Bank1 kernel payload checksum is not zero");

            byte[] output = new byte[KernelPartSize];
            Buffer.BlockCopy(b2, 0, output, 0, KernelHeaderOffset);
            Buffer.BlockCopy(b1, KernelHeaderOffset, output, KernelHeaderOffset, KernelPartSize - KernelHeaderOffset);
            PutU32BE(output, KernelHeaderOffset + 8, NewBankMark);
            UInt32 outLen = U32BE(output, KernelHeaderOffset + 12);
            if (SumBE16(output, KernelHeaderOffset + 16, (int)outLen) != 0)
                throw new InvalidDataException("Output kernel checksum failed");
            File.WriteAllBytes(outputPath, output);
            return String.Format("Bank2 kernel OK: bank1 mark=0x{0:X8}, old bank2 mark=0x{1:X8}, new mark=0x{2:X8}, load=0x{3:X8}, payload={4}", mark1, mark2, NewBankMark, start1, outLen);
        }

        public static string WrapRootfs(string rawSqfsPath, string outputPath)
        {
            byte[] raw = File.ReadAllBytes(rawSqfsPath);
            if (raw.Length < 96 || !Sig(raw, 0, "hsqs")) throw new InvalidDataException("Raw filesystem is not little-endian SquashFS");
            UInt32 blockSize = U32LE(raw, 12);
            UInt16 comp = U16LE(raw, 20);
            UInt16 major = U16LE(raw, 28), minor = U16LE(raw, 30);
            UInt64 bytesUsed64 = U64LE(raw, 40);
            if (major != 4 || minor != 0) throw new InvalidDataException("Expected SquashFS 4.0");
            if (comp != 2) throw new InvalidDataException("Expected LZMA compression id 2");
            if (blockSize != 131072U) throw new InvalidDataException("Expected 131072-byte SquashFS block size");
            if (bytesUsed64 > (UInt64)raw.Length || bytesUsed64 > Int32.MaxValue) throw new InvalidDataException("SquashFS bytes_used is out of range");
            int bytesUsed = (int)bytesUsed64;
            int aligned = (bytesUsed + 0xFFF) & ~0xFFF;
            int checkEnd = aligned + 2;
            if (checkEnd > RootfsPartSize) throw new InvalidDataException("Rebuilt rootfs does not fit Bank2 partition");
            UInt32 rtkField = (UInt32)(aligned - 640);

            byte[] output = new byte[RootfsPartSize];
            for (int i = 0; i < output.Length; i++) output[i] = 0xFF;
            Buffer.BlockCopy(raw, 0, output, 0, bytesUsed);
            PutU32BE(output, 8, rtkField);
            for (int i = bytesUsed; i < aligned; i++) output[i] = 0;
            UInt16 sum = SumBE16(output, 0, aligned);
            UInt16 ck = (UInt16)((0x10000U - sum) & 0xFFFFU);
            output[aligned] = (byte)(ck >> 8);
            output[aligned + 1] = (byte)ck;
            if (SumBE16(output, 0, checkEnd) != 0) throw new InvalidDataException("Internal Realtek rootfs checksum verification failed");
            File.WriteAllBytes(outputPath, output);
            return String.Format("Rootfs wrapper OK: bytes_used={0} (0x{0:X}), check_end=0x{1:X}, RTK field=0x{2:X8}, checksum=0x{3:X4}", bytesUsed, checkEnd, rtkField, ck);
        }

        public static string Verify(string kernelPath, string rootfsPath)
        {
            byte[] k = File.ReadAllBytes(kernelPath), r = File.ReadAllBytes(rootfsPath);
            if (k.Length != KernelPartSize) throw new InvalidDataException("Kernel output size mismatch");
            if (r.Length != RootfsPartSize) throw new InvalidDataException("Rootfs output size mismatch");
            if (!Sig(k, KernelHeaderOffset, "cr6c")) throw new InvalidDataException("Bad output kernel signature");
            UInt32 mark = U32BE(k, KernelHeaderOffset + 8), len = U32BE(k, KernelHeaderOffset + 12);
            if (mark != NewBankMark) throw new InvalidDataException("Bad output Bank2 mark");
            if ((UInt64)KernelHeaderOffset + 16U + len > (UInt64)k.Length || SumBE16(k, KernelHeaderOffset + 16, (int)len) != 0)
                throw new InvalidDataException("Bad output kernel payload checksum");
            if (!Sig(r, 0, "hsqs") || U32LE(r, 12) != 131072U || U16LE(r, 20) != 2 || U16LE(r, 28) != 4 || U16LE(r, 30) != 0)
                throw new InvalidDataException("Bad output SquashFS header");
            UInt64 bytesUsed = U64LE(r, 40);
            UInt32 field = U32BE(r, 8);
            UInt64 checkLen64 = (UInt64)field + 640U + 2U;
            if (checkLen64 > (UInt64)r.Length || checkLen64 > Int32.MaxValue) throw new InvalidDataException("Realtek rootfs checked length out of range");
            int checkLen = (int)checkLen64;
            if (SumBE16(r, 0, checkLen) != 0) throw new InvalidDataException("Realtek rootfs checksum failed");
            if (!AllFF(r, checkLen)) throw new InvalidDataException("Rootfs partition tail is not all 0xFF");
            return String.Format("VERIFY PASS: kernel mark=0x{0:X8}, kernel checksum=0; SquashFS 4.0 LZMA block=131072 bytes_used={1}; Realtek checksum=0", mark, bytesUsed);
        }
    }

}
