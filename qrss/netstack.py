"""A tiny SLIP / IPv4 / ICMP / TCP stack that runs entirely in user space over a serial port.

Just enough to talk to a Quantar station control module in boot (SLIP) mode, so no
operating-system SLIP driver, IP configuration or administrator rights are needed.
The same code serves both ends (the virtual SCM uses it as a server).

  * SLIP (RFC 1055): END 0xC0, ESC 0xDB, ESC_END 0xDC, ESC_ESC 0xDD.
  * IPv4 without fragmentation or routing: one peer on the other end of the wire.
  * ICMP echo request/reply (WinRSS checks the link with a ping first).
  * TCP: active and passive open, in-order receive, cumulative ACK, go-back-N
    retransmission, MSS option, FIN/RST.  Window 4 KB each way.  Good for 9600-115200 baud.

WinRSS addresses (from SlipOperation's ping packets): front port station 193.0.0.2 /
PC 193.0.0.5, back port station 193.0.0.1 / PC 193.0.0.4.  In download mode the station
also announces both addresses in its "FLASH ME <station ip> <pc ip>" reply.
"""
import random, socket, struct, threading, time

END, ESC, ESC_END, ESC_ESC = 0xC0, 0xDB, 0xDC, 0xDD
FRONT = ("193.0.0.2", "193.0.0.5")      # (station, pc)
BACK = ("193.0.0.1", "193.0.0.4")

FIN, SYN, RST, PSH, ACK = 0x01, 0x02, 0x04, 0x08, 0x10


def ip2b(ip):
    return socket.inet_aton(ip)


def b2ip(b):
    return socket.inet_ntoa(b)


def csum(data):
    if len(data) & 1:
        data += b"\0"
    s = sum(struct.unpack('>%dH' % (len(data) // 2), data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def slip_encode(pkt):
    out = bytearray([END])
    for b in pkt:
        if b == END:
            out += bytes([ESC, ESC_END])
        elif b == ESC:
            out += bytes([ESC, ESC_ESC])
        else:
            out.append(b)
    out.append(END)
    return bytes(out)


class SlipDecoder:
    def __init__(self):
        self.buf = bytearray()
        self.esc = False

    def feed(self, data):
        pkts = []
        for b in data:
            if self.esc:
                self.buf.append(END if b == ESC_END else ESC if b == ESC_ESC else b)
                self.esc = False
            elif b == ESC:
                self.esc = True
            elif b == END:
                if self.buf:
                    pkts.append(bytes(self.buf))
                self.buf = bytearray()
            else:
                self.buf.append(b)
        return pkts


class NetError(Exception):
    pass


class TcpConn:
    MSS = 512
    WINDOW = 4096
    # Unacknowledged bytes we let fly (also capped by the peer's window).  The Quantar boot
    # loader advertises 4 KB.  Filling it completely makes its window update just another
    # packet it holds until our next kick (+50 ms each time, seen on the virtual SCM), so keep
    # 1 KB of headroom: 3 KB covers ~275 ms of round trip at 115200, the loader ACKs in 10-25 ms.
    MAX_INFLIGHT = 3072

    def __init__(self, stack, lport, rip, rport):
        self.stack, self.lport, self.rip, self.rport = stack, lport, rip, rport
        self.state = "CLOSED"
        self.iss = random.randrange(1 << 32)
        self.snd_una = self.snd_nxt = self.iss
        self.rcv_nxt = 0
        self.peer_mss = 536
        self.peer_win = 1024
        self.txbuf = bytearray()          # unacknowledged + unsent data (starts at snd_una)
        self.rxbuf = bytearray()
        self.fin_queued = self.fin_sent = self.peer_fin = False
        self.cv = threading.Condition(stack.lock)
        self.rto = 1.5
        self.timer = None
        self.retries = 0
        self.error = None
        self.accepted_by = None

    # ------------------------------------------------------------ output
    def _seg(self, flags, seq, payload=b"", opts=b""):
        hl = (20 + len(opts)) // 4
        win = max(0, self.WINDOW - len(self.rxbuf))
        hdr = struct.pack('>HHIIBBHHH', self.lport, self.rport, seq, self.rcv_nxt if flags & ACK else 0,
                          hl << 4, flags, win, 0, 0) + opts
        ph = ip2b(self.stack.ip) + ip2b(self.rip) + struct.pack('>BBH', 0, 6, len(hdr) + len(payload))
        c = csum(ph + hdr + payload)
        hdr = hdr[:16] + struct.pack('>H', c) + hdr[18:]
        self.stack.send_ip(self.rip, 6, hdr + payload)

    def _syn(self, ack):
        self._seg(SYN | (ACK if ack else 0), self.iss, opts=struct.pack('>BBH', 2, 4, self.MSS))
        self.timer = time.time() + self.rto

    def _output(self, retransmit=False):
        """Send as much as the window allows (from snd_una when retransmitting)."""
        if self.state in ("SYN_SENT", "SYN_RCVD"):
            if retransmit:
                self._syn(self.state == "SYN_RCVD")
            return
        if self.state not in ("ESTABLISHED", "CLOSE_WAIT", "FIN_WAIT_1", "CLOSING", "LAST_ACK"):
            return
        if retransmit:
            self.snd_nxt = self.snd_una
            if self.fin_sent:
                self.fin_sent = False
        mss = min(self.MSS, self.peer_mss)
        probe = retransmit
        while True:
            off = (self.snd_nxt - self.snd_una) & 0xFFFFFFFF
            inflight = off
            room = max(0, min(self.peer_win, self.MAX_INFLIGHT) - inflight)
            if room == 0 and inflight == 0 and off < len(self.txbuf):
                if probe:                      # zero-window probe: one byte
                    room, probe = 1, False
                elif self.timer is None:       # persist timer
                    self.timer = time.time() + self.rto
            chunk = bytes(self.txbuf[off:off + min(mss, room)])
            if chunk:
                last = off + len(chunk) >= len(self.txbuf)
                self._seg(ACK | (PSH if last else 0), self.snd_nxt, chunk)
                self.snd_nxt = (self.snd_nxt + len(chunk)) & 0xFFFFFFFF
                if self.timer is None:
                    self.timer = time.time() + self.rto
                continue
            if self.fin_queued and not self.fin_sent and off >= len(self.txbuf):
                self._seg(FIN | ACK, self.snd_nxt)
                self.snd_nxt = (self.snd_nxt + 1) & 0xFFFFFFFF
                self.fin_sent = True
                if self.timer is None:
                    self.timer = time.time() + self.rto
            break

    def tick(self, now):
        if self.timer is not None and now >= self.timer:
            self.retries += 1
            if self.retries > 12:
                self._abort("connection timed out")
                return
            self.rto = min(self.rto * 2, 8.0)
            self.timer = None
            self._output(retransmit=True)
            if self.timer is None:
                self.timer = now + self.rto

    def _abort(self, why):
        self.error = why
        self.state = "CLOSED"
        self.timer = None
        self.stack.conns.pop((self.lport, self.rip, self.rport), None)
        self.cv.notify_all()

    # ------------------------------------------------------------ input
    def segment(self, seq, ack, flags, win, opts, payload):
        if flags & RST:
            self._abort("connection reset by peer")
            return
        self.peer_win = win
        if self.state == "SYN_SENT":
            if flags & SYN and flags & ACK and ack == (self.iss + 1) & 0xFFFFFFFF:
                self._mss(opts)
                self.rcv_nxt = (seq + 1) & 0xFFFFFFFF
                self.snd_una = self.snd_nxt = ack
                self.state = "ESTABLISHED"
                self.timer, self.retries, self.rto = None, 0, 1.5
                self._seg(ACK, self.snd_nxt)
                self.cv.notify_all()
            return
        if self.state == "SYN_RCVD":
            if flags & SYN:                       # retransmitted SYN
                self._syn(True)
                return
            if flags & ACK and ack == (self.iss + 1) & 0xFFFFFFFF:
                self.snd_una = self.snd_nxt = ack
                self.state = "ESTABLISHED"
                self.timer, self.retries, self.rto = None, 0, 1.5
                if self.accepted_by:
                    self.accepted_by.ready.append(self)
                    self.accepted_by.cv.notify_all()
            else:
                return
        if flags & SYN:                           # duplicate SYN-ACK: re-ack
            self._seg(ACK, self.snd_nxt)
            return
        # ACK processing
        if flags & ACK:
            acked = (ack - self.snd_una) & 0xFFFFFFFF
            inflight = (self.snd_nxt - self.snd_una) & 0xFFFFFFFF
            if 0 < acked <= inflight:
                data_acked = min(acked, len(self.txbuf))
                del self.txbuf[:data_acked]
                self.snd_una = ack
                self.retries, self.rto = 0, 1.5
                self.timer = time.time() + self.rto if self.snd_una != self.snd_nxt else None
                if self.fin_sent and self.snd_una == self.snd_nxt:
                    if self.state == "FIN_WAIT_1":
                        self.state = "FIN_WAIT_2"
                    elif self.state in ("CLOSING", "LAST_ACK"):
                        self.state = "CLOSED" if self.state == "LAST_ACK" else "TIME_WAIT"
                        self.stack.conns.pop((self.lport, self.rip, self.rport), None)
                self.cv.notify_all()
        # data
        if payload or flags & FIN:
            if seq == self.rcv_nxt:
                if payload:
                    self.rxbuf += payload
                    self.rcv_nxt = (self.rcv_nxt + len(payload)) & 0xFFFFFFFF
                if flags & FIN and not self.peer_fin:
                    self.peer_fin = True
                    self.rcv_nxt = (self.rcv_nxt + 1) & 0xFFFFFFFF
                    if self.state == "ESTABLISHED":
                        self.state = "CLOSE_WAIT"
                    elif self.state == "FIN_WAIT_1":
                        self.state = "CLOSING"
                    elif self.state == "FIN_WAIT_2":
                        self.state = "TIME_WAIT"
                        self.stack.conns.pop((self.lport, self.rip, self.rport), None)
                self.cv.notify_all()
            self._seg(ACK, self.snd_nxt)          # (re-)acknowledge
        self._output()

    def _mss(self, opts):
        i = 0
        while i < len(opts):
            k = opts[i]
            if k == 0:
                break
            if k == 1:
                i += 1
                continue
            if i + 1 >= len(opts):
                break
            ln = opts[i + 1]
            if k == 2 and ln == 4:
                self.peer_mss = struct.unpack('>H', opts[i + 2:i + 4])[0]
            i += max(ln, 2)

    # ------------------------------------------------------------ user API
    def send(self, data, timeout=120):
        end = time.time() + timeout
        with self.cv:
            if self.state not in ("ESTABLISHED", "CLOSE_WAIT"):
                raise NetError(self.error or "not connected")
            self.txbuf += data
            self._output()
            # flow control: keep at most 16 KB queued
            while len(self.txbuf) > 16384:
                if self.error:
                    raise NetError(self.error)
                if not self.cv.wait(max(0.0, end - time.time())):
                    raise NetError("send timed out")

    def drain(self, timeout=120):
        """Wait until all sent data is acknowledged."""
        end = time.time() + timeout
        with self.cv:
            while self.txbuf:
                if self.error:
                    raise NetError(self.error)
                if not self.cv.wait(max(0.0, end - time.time())) and time.time() >= end:
                    raise NetError("timed out waiting for acknowledgement")

    def recv(self, n=65536, timeout=30):
        """Return up to n bytes; b'' at end of stream."""
        end = time.time() + timeout
        with self.cv:
            while not self.rxbuf:
                if self.peer_fin or self.state == "CLOSED":
                    if self.error and not self.peer_fin:
                        raise NetError(self.error)
                    return b""
                if not self.cv.wait(max(0.0, end - time.time())) and time.time() >= end:
                    raise NetError("receive timed out")
            d = bytes(self.rxbuf[:n])
            del self.rxbuf[:n]
            if len(self.rxbuf) + len(d) >= self.WINDOW // 2:       # window update
                self._seg(ACK, self.snd_nxt)
            return d

    def readline(self, timeout=30):
        end = time.time() + timeout
        with self.cv:
            while b"\n" not in self.rxbuf:
                if self.peer_fin or self.state == "CLOSED":
                    break
                if not self.cv.wait(max(0.0, end - time.time())) and time.time() >= end:
                    raise NetError("timed out waiting for a reply line")
            i = self.rxbuf.find(b"\n")
            if i < 0:
                if not self.rxbuf:
                    raise NetError(self.error or "connection closed")
                i = len(self.rxbuf) - 1
            line = bytes(self.rxbuf[:i + 1])
            del self.rxbuf[:i + 1]
            return line.decode('latin1').rstrip("\r\n")

    def close(self, timeout=20):
        end = time.time() + timeout
        with self.cv:
            if self.state in ("ESTABLISHED", "SYN_RCVD"):
                self.state = "FIN_WAIT_1"
            elif self.state == "CLOSE_WAIT":
                self.state = "LAST_ACK"
            else:
                return
            self.fin_queued = True
            self._output()
            while self.state not in ("CLOSED", "TIME_WAIT", "FIN_WAIT_2"):
                if self.error or not self.cv.wait(max(0.0, end - time.time())) and time.time() >= end:
                    break
            if self.state in ("CLOSED", "TIME_WAIT") or self.error:
                self.stack.conns.pop((self.lport, self.rip, self.rport), None)
            # FIN_WAIT_2: stay registered so the peer can still send until its FIN


class Listener:
    def __init__(self, stack, port):
        self.stack, self.port = stack, port
        self.ready = []
        self.cv = threading.Condition(stack.lock)

    def accept(self, timeout=10):
        end = time.time() + timeout
        with self.cv:
            while not self.ready:
                if not self.cv.wait(max(0.0, end - time.time())) and time.time() >= end:
                    raise NetError("no connection on port %d within %d s" % (self.port, timeout))
            return self.ready.pop(0)

    def close(self):
        with self.cv:
            self.stack.listeners.pop(self.port, None)


class Stack:
    """`port` needs read(n) (returning b'' on timeout) and write(bytes)."""

    # The Quantar boot loader (pSOS, NTL68K) holds each packet it generates until it next
    # receives a byte on the serial line: a ping is answered only when the next one arrives,
    # an FTP reply only when we send something else.  Writing a lone SLIP END (an empty frame,
    # ignored by every SLIP receiver) whenever the line has been quiet for `kick` seconds
    # releases it.  Found on R020.14.015 boot2, 2026-11-05.
    # Do not kick much faster: kicking every 5 ms while waiting for a reply flooded the boot
    # loader with empty frames and it started dropping packets (2026-11-05, real station:
    # 43 resent segments in the FTP command exchanges, +50 s; the 50 ms kick had none).
    # The port is looked at every POLL seconds instead of read(1024): pyserial's read(n) only
    # returns early once n bytes are in, so it sat out the whole 50 ms port timeout for every
    # 40-byte ACK - ~18 s of an 89 s sc.o transfer at 115200 went that way.
    KICK = 0.05
    POLL = 0.002

    def __init__(self, port, ip, log=None, kick=KICK):
        self.port, self.ip = port, ip
        self.kick = kick
        self._iw = None                     # port has in_waiting (None = not tried yet)
        self.wlock = threading.Lock()
        self.last_tx = 0.0
        self.log = log or (lambda msg: None)
        self.lock = threading.RLock()
        self.conns = {}
        self.listeners = {}
        self.pings = {}
        self.ident = random.randrange(65536)
        self.dec = SlipDecoder()
        self.running = True
        self.stats = {"rx": 0, "tx": 0, "bad": 0}
        self.next_port = 2000 + random.randrange(2000)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        self.thread.join(2)
        with self.lock:
            for c in list(self.conns.values()):
                c._abort("link closed")

    # ------------------------------------------------------------ IP layer
    def send_ip(self, dst, proto, payload):
        self.ident = (self.ident + 1) & 0xFFFF
        hdr = struct.pack('>BBHHHBBH4s4s', 0x45, 0, 20 + len(payload), self.ident, 0, 64, proto, 0,
                          ip2b(self.ip), ip2b(dst))
        hdr = hdr[:10] + struct.pack('>H', csum(hdr)) + hdr[12:]
        self.stats["tx"] += 1
        frame = slip_encode(hdr + payload)
        with self.wlock:
            self.port.write(frame)
            self.last_tx = time.time()

    def _kick(self):
        if self.kick and time.time() - self.last_tx >= self.kick:
            with self.wlock:
                self.port.write(bytes([END]))
                self.last_tx = time.time()

    def _read(self):
        """Whatever has arrived, without sitting out the port's read timeout."""
        if self._iw is not False:
            try:
                n = self.port.in_waiting
            except Exception:
                self._iw = False
            else:
                self._iw = True
                if n:
                    return self.port.read(n)
                time.sleep(self.POLL)
                return b""
        return self.port.read(1024)

    def _run(self):
        while self.running:
            try:
                self._kick()
                data = self._read()
            except Exception as e:
                self.log("serial read error: %s" % e)
                time.sleep(0.2)
                data = b""
            with self.lock:
                for pkt in self.dec.feed(data):
                    try:
                        self._ip_in(pkt)
                    except Exception as e:           # never let a bad packet kill the stack
                        self.stats["bad"] += 1
                        self.log("bad packet (%s): %s" % (e, pkt[:40].hex()))
                now = time.time()
                for c in list(self.conns.values()):
                    c.tick(now)

    def _ip_in(self, pkt):
        if len(pkt) < 20 or pkt[0] >> 4 != 4:
            self.stats["bad"] += 1
            return
        ihl = (pkt[0] & 15) * 4
        tot = struct.unpack('>H', pkt[2:4])[0]
        if csum(pkt[:ihl]) != 0:
            self.stats["bad"] += 1
            return
        proto, src, dst = pkt[9], b2ip(pkt[12:16]), b2ip(pkt[16:20])
        if dst != self.ip:
            return
        self.stats["rx"] += 1
        body = pkt[ihl:tot]
        if proto == 1:
            self._icmp(src, body)
        elif proto == 6:
            self._tcp(src, body)

    def _icmp(self, src, b):
        if csum(b) != 0:
            self.stats["bad"] += 1
            return
        if b[0] == 8:                               # echo request -> reply
            r = bytearray(b)
            r[0] = 0
            r[2:4] = b"\0\0"
            r[2:4] = struct.pack('>H', csum(bytes(r)))
            self.send_ip(src, 1, bytes(r))
        elif b[0] == 0:
            ident, seq = struct.unpack('>HH', b[4:8])
            ev = self.pings.get((ident, seq))
            if ev:
                ev.set()

    def ping(self, dst, timeout=2.0, count=1):
        """True if any of `count` echo requests is answered."""
        for i in range(count):
            ident, seq = 0x5C05, random.randrange(65536)
            ev = threading.Event()
            with self.lock:
                self.pings[(ident, seq)] = ev
            b = bytearray(struct.pack('>BBHHH', 8, 0, 0, ident, seq) + b"abcdefghijklmnopqrstuvwabcdefghi")
            b[2:4] = struct.pack('>H', csum(bytes(b)))
            with self.lock:
                self.send_ip(dst, 1, bytes(b))
            ok = ev.wait(timeout)
            with self.lock:
                self.pings.pop((ident, seq), None)
            if ok:
                return True
        return False

    # ------------------------------------------------------------ TCP layer
    def _tcp(self, src, b):
        ph = ip2b(src) + ip2b(self.ip) + struct.pack('>BBH', 0, 6, len(b))
        if csum(ph + b) != 0:
            self.stats["bad"] += 1
            return
        sport, dport, seq, ack, off, flags, win = struct.unpack('>HHIIBBH', b[:16])
        hl = (off >> 4) * 4
        opts, payload = b[20:hl], b[hl:]
        c = self.conns.get((dport, src, sport))
        if c is None:
            lst = self.listeners.get(dport)
            if lst and flags & SYN and not flags & ACK:
                c = TcpConn(self, dport, src, sport)
                c.accepted_by = lst
                c._mss(opts)
                c.rcv_nxt = (seq + 1) & 0xFFFFFFFF
                c.peer_win = win
                c.state = "SYN_RCVD"
                c.snd_nxt = (c.iss + 1) & 0xFFFFFFFF
                self.conns[(dport, src, sport)] = c
                c._syn(True)
            elif not flags & RST:                          # nobody here: reset
                self._rst(src, sport, dport, seq, ack, flags, len(payload))
            return
        c.segment(seq, ack, flags, win, opts, payload)

    def _rst(self, dst, dport, sport, seq, ack, flags, plen):
        tmp = TcpConn(self, sport, dst, dport)
        if flags & ACK:
            tmp._seg(RST, ack)
        else:
            tmp.rcv_nxt = (seq + plen + (1 if flags & SYN else 0) + (1 if flags & FIN else 0)) & 0xFFFFFFFF
            tmp._seg(RST | ACK, 0)

    def alloc_port(self):
        with self.lock:
            self.next_port = 2000 + (self.next_port - 1999) % 3000
            return self.next_port

    def listen(self, port):
        with self.lock:
            lst = Listener(self, port)
            self.listeners[port] = lst
            return lst

    def connect(self, dst, dport, timeout=20):
        with self.lock:
            c = TcpConn(self, self.alloc_port(), dst, dport)
            self.conns[(c.lport, dst, dport)] = c
            c.state = "SYN_SENT"
            c.snd_nxt = (c.iss + 1) & 0xFFFFFFFF
            c._syn(False)
            end = time.time() + timeout
            while c.state == "SYN_SENT":
                if c.error or not c.cv.wait(max(0.0, end - time.time())) and time.time() >= end:
                    self.conns.pop((c.lport, dst, dport), None)
                    raise NetError(c.error or "no answer from %s port %d" % (dst, dport))
            if c.state != "ESTABLISHED":
                raise NetError(c.error or "connection failed")
            return c


# ====================================================================== FTP client
class FtpError(Exception):
    pass


class FtpClient:
    """Active-mode FTP client as used by WinRSS (anonymous login, PORT, TYPE, STOR/RETR)."""

    def __init__(self, stack, host, user="anonymous", password="anonymous", log=None, timeout=30,
                 port=21):
        self.stack, self.host, self.timeout = stack, host, timeout
        self.log = log or (lambda m: None)
        self.ctl = stack.connect(host, port, timeout=timeout)
        self._expect((220,))
        r = self.cmd("USER " + user)
        if r[0] == 331:
            r = self.cmd("PASS " + password)
        if r[0] != 230:
            raise FtpError("login failed: %s" % r[1])

    def _reply(self):
        line = self.ctl.readline(self.timeout)
        text = [line]
        if len(line) >= 4 and line[3] == '-':
            code = line[:3]
            while True:
                line = self.ctl.readline(self.timeout)
                text.append(line)
                if line.startswith(code + " "):
                    break
        self.log("< " + " | ".join(text))
        try:
            return int(line[:3]), "\n".join(text)
        except ValueError:
            raise FtpError("bad reply %r" % line)

    def _expect(self, codes):
        r = self._reply()
        if r[0] not in codes:
            raise FtpError(r[1])
        return r

    def cmd(self, text):
        self.log("> " + text)
        self.ctl.send((text + "\r\n").encode('latin1'))
        return self._reply()

    def _data(self, verb, path, binary):
        lport = self.stack.alloc_port()
        lst = self.stack.listen(lport)
        try:
            ip = self.stack.ip.replace('.', ',')
            r = self.cmd("PORT %s,%d,%d" % (ip, lport >> 8, lport & 255))
            if r[0] != 200:
                raise FtpError("PORT: %s" % r[1])
            r = self.cmd("TYPE " + ("I" if binary else "A"))
            if r[0] != 200:
                raise FtpError("TYPE: %s" % r[1])
            r = self.cmd("%s %s" % (verb, path))
            if r[0] > 299:
                raise FtpError("%s %s: %s" % (verb, path, r[1]))
            return lst.accept(self.timeout)
        finally:
            lst.close()

    def put(self, data, path, binary=True, progress=None):
        dc = self._data("STOR", path, binary)
        sent = 0
        for i in range(0, len(data), 2048):
            dc.send(data[i:i + 2048])
            sent += len(data[i:i + 2048])
            if progress:
                progress(sent, len(data))
        dc.drain(max(60, len(data) / 200))
        dc.close()
        r = self._reply()
        while 100 <= r[0] < 200:
            r = self._reply()
        if r[0] not in (226, 250):
            raise FtpError("STOR %s: %s" % (path, r[1]))

    def get(self, path, binary=True):
        dc = self._data("RETR", path, binary)
        out = bytearray()
        while True:
            d = dc.recv(65536, self.timeout)
            if not d:
                break
            out += d
        dc.close()
        r = self._reply()
        while 100 <= r[0] < 200:
            r = self._reply()
        if r[0] not in (226, 250):
            raise FtpError("RETR %s: %s" % (path, r[1]))
        return bytes(out)

    def quit(self):
        try:
            self.cmd("QUIT")
        except Exception:
            pass
        try:
            self.ctl.close(5)
        except Exception:
            pass


# ====================================================================== real TCP (Ethernet)
class SocketStack:
    """The same interface as Stack, over real TCP sockets: a station reached by Ethernet instead
    of one at the end of a serial cable (RSS manual 11-35, "Ethernet Connections").  Only what
    the FTP client and the virtual SCM's boot-loader FTP server use is implemented.

    `ports` maps the port a caller asks for to the one actually bound, so a virtual SCM can
    serve the boot loader's port 21 as an unprivileged port in a test."""

    def __init__(self, ip=None, log=None, ports=None, dst=None):
        self.log = log or (lambda m: None)
        self.ip = ip or self.local_ip(dst or "10.255.255.255")
        self.ports = ports or {}
        self.conns = []

    @staticmethod
    def local_ip(dst="10.255.255.255"):
        """The address of the interface that routes to `dst` - what the station must connect
        back to for the FTP data connection (the PORT command)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((dst, 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    def connect(self, dst, dport, timeout=20):
        s = socket.socket()
        s.settimeout(timeout)
        try:
            s.connect((dst, dport))
        except OSError as e:
            s.close()
            raise NetError("cannot reach %s port %d: %s" % (dst, dport, e))
        c = SocketConn(s, "%s:%d" % (dst, dport))
        self.conns.append(c)
        return c

    def alloc_port(self):
        s = socket.socket()
        try:
            s.bind(("", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def listen(self, port):
        return SocketListener(self.ports.get(port, port))

    def ping(self, dst, timeout=2.0, count=1, port=None):
        """Reachability without raw sockets: the FTP port is what a download needs anyway."""
        port = port or self.ports.get(21, 21)
        for _ in range(max(1, count)):
            try:
                self.connect(dst, port, timeout).close(1)
                return True
            except NetError:
                pass
        return False

    def stop(self):
        for c in list(self.conns):
            try:
                c.close(1)
            except Exception:
                pass
        self.conns = []


class SocketConn:
    """A TCP connection with TcpConn's send/recv/readline/drain/close interface."""

    def __init__(self, sock, name="socket"):
        self.sock, self.name = sock, name
        self.rxbuf = bytearray()

    def send(self, data, timeout=120):
        self.sock.settimeout(timeout)
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise NetError(str(e))

    def drain(self, timeout=120):
        pass                                   # sendall() has already handed everything to TCP

    def recv(self, n=65536, timeout=30):
        self.sock.settimeout(timeout)
        try:
            return self.sock.recv(n)
        except socket.timeout:
            raise NetError("receive timed out")
        except OSError as e:
            raise NetError(str(e))

    def readline(self, timeout=30):
        end = time.time() + timeout
        while b"\n" not in self.rxbuf:
            self.sock.settimeout(max(0.1, end - time.time()))
            try:
                d = self.sock.recv(4096)
            except socket.timeout:
                raise NetError("timed out waiting for a reply line")
            except OSError as e:
                raise NetError(str(e))
            if not d:
                if not self.rxbuf:
                    raise NetError("connection closed")
                break
            self.rxbuf += d
        i = self.rxbuf.find(b"\n")
        if i < 0:
            i = len(self.rxbuf) - 1
        line = bytes(self.rxbuf[:i + 1])
        del self.rxbuf[:i + 1]
        return line.decode('latin1').rstrip("\r\n")

    def close(self, timeout=20):
        try:
            self.sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class SocketListener:
    def __init__(self, port):
        self.port = port
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("", port))
        self.sock.listen(4)

    def accept(self, timeout=10):
        self.sock.settimeout(timeout)
        try:
            c, addr = self.sock.accept()
        except socket.timeout:
            raise NetError("no connection on port %d within %d s" % (self.port, timeout))
        except OSError as e:
            raise NetError(str(e))
        return SocketConn(c, "%s:%d" % addr)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ====================================================================== in-memory wire (tests, virtual SCM)
class Wire:
    """One end of an in-memory serial cable; `loss` drops that fraction of written chunks."""

    def __init__(self, loss=0.0, baud=None):
        self.q = []
        self.cv = threading.Condition()
        self.peer = None
        self.loss = loss
        self.baud = baud
        self.closed = False

    @staticmethod
    def pair(loss=0.0, baud=None):
        a, b = Wire(loss, baud), Wire(loss, baud)
        a.peer, b.peer = b, a
        return a, b

    def write(self, data):
        if self.baud:
            time.sleep(len(data) * 10.0 / self.baud)
        if self.loss and random.random() < self.loss:
            return len(data)
        p = self.peer
        with p.cv:
            p.q.append(bytes(data))
            p.cv.notify_all()
        return len(data)

    @property
    def in_waiting(self):
        with self.cv:
            return sum(len(d) for d in self.q)

    def read(self, n=1024, timeout=0.05):
        with self.cv:
            if not self.q:
                self.cv.wait(timeout)
            if not self.q:
                return b""
            d = self.q.pop(0)
            if len(d) > n:
                self.q.insert(0, d[n:])
                d = d[:n]
            return d

    def close(self):
        self.closed = True
