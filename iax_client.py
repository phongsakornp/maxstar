#!/usr/bin/env python3
"""
Minimal IAX2 client for testing an AllStarLink node from a Mac.

Connects to a node's [iaxclient] IAX2 account, performs the MD5 auth
handshake, and streams bidirectional u-law audio via the system mic/
speakers.

Keying is voice-frame-based, not DTMF: on connect, app_rpt sends a
"!NEWKEY1!" text frame which this client answers to unlock keying: once
unlocked, simply sending voice frames (gated by the `k`/`u` commands)
registers as a key-up, per app_rpt's own source comments in
apps/app_rpt/rpt_channel.h.

Protocol constants below were verified against Asterisk's own source
(channels/iax2/include/iax2.h, channels/chan_iax2.c) rather than assumed
from memory, since getting the handshake/codec encoding wrong silently
breaks everything.
"""

import argparse
import audioop
import hashlib
import os
import queue
import socket
import struct
import sys
import threading
import time

import numpy as np
import sounddevice as sd


def load_dotenv(path=".env"):
    """Load KEY=VALUE lines from .env into os.environ (existing env wins)."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# --------------------------------------------------------------- protocol

FRAME_DTMF_END = 1
FRAME_VOICE = 2
FRAME_CONTROL = 4
FRAME_IAX = 6
FRAME_TEXT = 7

NEWKEY1STR = b"!NEWKEY1!"

IAX_NEW = 1
IAX_ACK = 4
IAX_HANGUP = 5
IAX_REJECT = 6
IAX_ACCEPT = 7
IAX_AUTHREQ = 8
IAX_AUTHREP = 9
IAX_LAGRQ = 11
IAX_LAGRP = 12
IAX_VNAK = 18

IE_CALLED_NUMBER = 1
IE_CALLED_CONTEXT = 5
IE_USERNAME = 6
IE_CAPABILITY = 8
IE_FORMAT = 9
IE_VERSION = 11
IE_CHALLENGE = 15
IE_MD5_RESULT = 16
IE_CAUSE = 22
IE_CAPABILITY2 = 55
IE_FORMAT2 = 56

CONTROL_HANGUP = 1
CONTROL_ANSWER = 4

FORMAT_ULAW = 1 << 2  # 4 -- matches AST_FORMAT_ULAW
IAX_PROTO_VERSION = 2

SC_LOG_FLAG = 0x80

SAMPLE_RATE = 8000
FRAME_SAMPLES = 160  # 20ms @ 8kHz -- standard G.711 packetization


def compress_subclass(value: int) -> int:
    if value < SC_LOG_FLAG:
        return value
    for bit in range(32):
        if value == (1 << bit):
            return bit | SC_LOG_FLAG
    raise ValueError(f"cannot compress subclass {value}")


def uncompress_subclass(byte: int) -> int:
    if byte & SC_LOG_FLAG:
        if byte == 0xFF:
            return -1
        return 1 << (byte & 0x7F)
    return byte


def encode_ies(ies):
    out = b""
    for ie_type, value in ies:
        out += struct.pack("!BB", ie_type, len(value)) + value
    return out


def decode_ies(data: bytes):
    result = {}
    i = 0
    while i + 2 <= len(data):
        ie_type, ie_len = data[i], data[i + 1]
        i += 2
        result[ie_type] = data[i:i + ie_len]
        i += ie_len
    return result


def ie_str(s: str) -> bytes:
    return s.encode("ascii")


def ie_u32(v: int) -> bytes:
    return struct.pack("!I", v)


def ie_u16(v: int) -> bytes:
    return struct.pack("!H", v)


def ie_versioned_u64(v: int, version: int = 0) -> bytes:
    return struct.pack("!B", version) + struct.pack("!Q", v)


class FullFrame:
    __slots__ = ("scallno", "dcallno", "retrans", "ts", "oseqno", "iseqno",
                 "ftype", "csub", "payload")

    def __init__(self, scallno, dcallno, retrans, ts, oseqno, iseqno,
                 ftype, csub, payload=b""):
        self.scallno = scallno
        self.dcallno = dcallno
        self.retrans = retrans
        self.ts = ts
        self.oseqno = oseqno
        self.iseqno = iseqno
        self.ftype = ftype
        self.csub = csub
        self.payload = payload

    def pack(self) -> bytes:
        scall = 0x8000 | self.scallno
        dcall = (0x8000 if self.retrans else 0) | self.dcallno
        header = struct.pack("!HHIBBBB", scall, dcall, self.ts & 0xFFFFFFFF,
                              self.oseqno & 0xFF, self.iseqno & 0xFF,
                              self.ftype, self.csub)
        return header + self.payload

    @classmethod
    def unpack(cls, data: bytes):
        scall, dcall, ts, oseqno, iseqno, ftype, csub = struct.unpack(
            "!HHIBBBB", data[:12])
        retrans = bool(dcall & 0x8000)
        return cls(scall & 0x7FFF, dcall & 0x7FFF, retrans, ts, oseqno,
                    iseqno, ftype, csub, data[12:])


class MiniFrame:
    __slots__ = ("callno", "ts", "payload")

    def __init__(self, callno, ts, payload):
        self.callno = callno
        self.ts = ts
        self.payload = payload

    def pack(self) -> bytes:
        return struct.pack("!HH", self.callno & 0x7FFF,
                            self.ts & 0xFFFF) + self.payload

    @classmethod
    def unpack(cls, data: bytes):
        callno, ts = struct.unpack("!HH", data[:4])
        return cls(callno & 0x7FFF, ts, data[4:])


# --------------------------------------------------------------- call

class IaxCall:
    def __init__(self, host, port, username, secret, called_number,
                 called_context=None):
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.called_number = called_number
        self.called_context = called_context

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(0.5)

        self.scallno = 1   # our call number (arbitrary, nonzero)
        self.dcallno = 0    # peer's call number, learned from first reply
        self.oseqno = 0     # next outbound sequence number
        self.iseqno = 0     # next expected inbound sequence number
        self.start_ts = time.time()

        self.accepted = False
        self.answered = False
        self.hungup = threading.Event()

        self.tx_queue: "queue.Queue[bytes]" = queue.Queue()
        self.keyed = threading.Event()

        self._out_queue: "queue.Queue[bytes]" = queue.Queue()
        self._out_buf = b""
        self._voice_anchor_sent = False

        self._recv_thread = None
        self._tx_thread = None
        self._out_stream = None
        self._in_stream = None

        self.selftest = False
        self.verbose = False
        self.rx_audio_bytes = 0
        self.rx_audio_frames = 0
        self.rx_audio_peak = 0

        # Instantaneous (per-frame) peak levels for live VU-style metering,
        # as opposed to rx_audio_peak above which is a cumulative high-water
        # mark used by --selftest/--listen reporting.
        self.rx_level = 0
        self.tx_level = 0

    def _trace(self, msg):
        if self.verbose:
            print(msg)

    # ---- low level ------------------------------------------------

    def _now_ts(self) -> int:
        return int((time.time() - self.start_ts) * 1000)

    def _send_full(self, ftype, csub, payload=b""):
        fr = FullFrame(self.scallno, self.dcallno, False, self._now_ts(),
                        self.oseqno, self.iseqno, ftype, csub, payload)
        self._trace(f"[TX] type={ftype} csub={csub} o={self.oseqno} "
                    f"i={self.iseqno} dcall={self.dcallno} len={len(payload)}")
        self.sock.sendto(fr.pack(), (self.host, self.port))
        self.oseqno = (self.oseqno + 1) & 0xFF

    def _send_ack(self):
        # ACK does not consume our own outbound sequence number.
        fr = FullFrame(self.scallno, self.dcallno, False, self._now_ts(),
                        self.oseqno, self.iseqno, FRAME_IAX, IAX_ACK)
        self._trace(f"[TX] ACK o={self.oseqno} i={self.iseqno} dcall={self.dcallno}")
        self.sock.sendto(fr.pack(), (self.host, self.port))

    def _send_voice(self, ulaw_bytes: bytes):
        if not self._voice_anchor_sent:
            fr = FullFrame(self.scallno, self.dcallno, False, self._now_ts(),
                            self.oseqno, self.iseqno, FRAME_VOICE,
                            compress_subclass(FORMAT_ULAW), ulaw_bytes)
            self.sock.sendto(fr.pack(), (self.host, self.port))
            self.oseqno = (self.oseqno + 1) & 0xFF
            self._voice_anchor_sent = True
        else:
            mf = MiniFrame(self.scallno, self._now_ts() & 0xFFFF, ulaw_bytes)
            self.sock.sendto(mf.pack(), (self.host, self.port))

    def send_dtmf(self, digit: str):
        self._send_full(FRAME_DTMF_END, ord(digit))

    # ---- handshake --------------------------------------------------

    def connect(self, timeout=8.0) -> bool:
        ies = encode_ies([
            (IE_VERSION, ie_u16(IAX_PROTO_VERSION)),
            (IE_CALLED_NUMBER, ie_str(self.called_number)),
            (IE_USERNAME, ie_str(self.username)),
        ] + ([(IE_CALLED_CONTEXT, ie_str(self.called_context))]
             if self.called_context else []) + [
            (IE_FORMAT, ie_u32(FORMAT_ULAW)),
            (IE_FORMAT2, ie_versioned_u64(FORMAT_ULAW)),
            (IE_CAPABILITY, ie_u32(FORMAT_ULAW)),
            (IE_CAPABILITY2, ie_versioned_u64(FORMAT_ULAW)),
        ])
        self._send_full(FRAME_IAX, IAX_NEW, ies)

        self._recv_thread = threading.Thread(target=self._recv_loop,
                                              daemon=True)
        self._recv_thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.accepted or self.hungup.is_set():
                return self.accepted
            time.sleep(0.05)
        return False

    def _recv_loop(self):
        while not self.hungup.is_set():
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                continue
            if data[0] & 0x80:
                self._handle_full(FullFrame.unpack(data))
            else:
                self._handle_mini(MiniFrame.unpack(data))

    def _handle_full(self, fr: FullFrame):
        if fr.scallno:
            self.dcallno = fr.scallno
        self.iseqno = (fr.oseqno + 1) & 0xFF
        self._trace(f"[RX] type={fr.ftype} csub={fr.csub} o={fr.oseqno} "
                    f"i={fr.iseqno} scall={fr.scallno} len={len(fr.payload)}")

        if fr.ftype == FRAME_IAX:
            if fr.csub == IAX_AUTHREQ:
                self._send_ack()
                ies = decode_ies(fr.payload)
                self._trace(f"    AUTHREQ ies={ {k: v for k, v in ies.items()} }")
                challenge = ies.get(IE_CHALLENGE, b"").decode("ascii", "ignore")
                digest = hashlib.md5(
                    (challenge + self.secret).encode("ascii")).hexdigest()
                self._trace(f"    challenge={challenge!r} secret={self.secret!r} "
                            f"digest={digest}")
                rep = encode_ies([(IE_MD5_RESULT, ie_str(digest))])
                self._send_full(FRAME_IAX, IAX_AUTHREP, rep)
            elif fr.csub == IAX_ACCEPT:
                self._send_ack()
                self.accepted = True
                print("[*] ACCEPT received -- call is up")
            elif fr.csub == IAX_REJECT:
                ies = decode_ies(fr.payload)
                cause = ies.get(IE_CAUSE, b"unknown").decode("ascii", "ignore")
                print(f"[!] Call rejected by node: {cause}")
                self._send_ack()
                self.hungup.set()
            elif fr.csub == IAX_HANGUP:
                self._send_ack()
                print("[*] Node sent HANGUP")
                self.hungup.set()
            elif fr.csub == IAX_LAGRQ:
                self._send_full(FRAME_IAX, IAX_LAGRP)
            elif fr.csub in (IAX_ACK, IAX_VNAK):
                pass
            else:
                self._send_ack()
        elif fr.ftype == FRAME_CONTROL:
            self._send_ack()
            if fr.csub == CONTROL_ANSWER:
                self.answered = True
                print("[*] Call answered")
            elif fr.csub == CONTROL_HANGUP:
                self.hungup.set()
        elif fr.ftype == FRAME_VOICE:
            self._send_ack()
            self._play(fr.payload)
        elif fr.ftype == FRAME_TEXT:
            self._send_ack()
            self._trace(f"    TEXT: {fr.payload!r}")
            if fr.payload.rstrip(b"\x00") == NEWKEY1STR:
                # Complete the app_rpt "newkey" handshake so it treats our
                # voice frames as a key-up instead of ignoring them for
                # NEWKEYTIME (2s) or requiring DTMF (which doesn't apply to
                # this non-phone-mode endpoint).
                print("    -> replying !NEWKEY1! to unlock voice-keyed audio")
                self._send_full(FRAME_TEXT, 0, NEWKEY1STR)
        else:
            # Any other full frame type still needs an ACK or the server
            # will keep retransmitting it.
            self._send_ack()

    def _handle_mini(self, mf: MiniFrame):
        self._play(mf.payload)

    # ---- audio ------------------------------------------------------

    def _play(self, ulaw_bytes: bytes):
        if not ulaw_bytes:
            return
        self.rx_audio_bytes += len(ulaw_bytes)
        self.rx_audio_frames += 1
        pcm16 = audioop.ulaw2lin(ulaw_bytes, 2)
        peak = max(abs(s) for s in struct.unpack(f"!{len(pcm16)//2}h", pcm16)) if pcm16 else 0
        self.rx_audio_peak = max(self.rx_audio_peak, peak)
        self.rx_level = peak
        if self.selftest:
            print(f"    [RX-AUDIO] frame#{self.rx_audio_frames} "
                  f"bytes={len(ulaw_bytes)} peak={peak}")
        self._out_queue.put(pcm16)

    def start_audio(self):
        def out_callback(outdata, frames, time_info, status):
            needed = frames * 2  # bytes needed for 16-bit mono
            while len(self._out_buf) < needed:
                try:
                    self._out_buf += self._out_queue.get_nowait()
                except queue.Empty:
                    self._out_buf += b"\x00" * (needed - len(self._out_buf))
                    break
            chunk, self._out_buf = (self._out_buf[:needed],
                                     self._out_buf[needed:])
            outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 1)

        def in_callback(indata, frames, time_info, status):
            samples = indata[:, 0]
            self.tx_level = int(np.abs(samples).max()) if len(samples) else 0
            if not self.keyed.is_set():
                return
            pcm16 = samples.tobytes()
            self.tx_queue.put(audioop.lin2ulaw(pcm16, 2))

        self._out_stream = sd.OutputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=FRAME_SAMPLES, callback=out_callback)
        self._in_stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=FRAME_SAMPLES, callback=in_callback)
        self._out_stream.start()
        self._in_stream.start()

        self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True)
        self._tx_thread.start()

    def _tx_loop(self):
        while not self.hungup.is_set():
            try:
                ulaw = self.tx_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._send_voice(ulaw)

    def key(self):
        # This endpoint type (app_rpt "X"/normal-endpoint mode, used by the
        # iax-client context) keys up simply by the presence of voice
        # frames once the newkey handshake has unlocked it -- no DTMF
        # needed (that convention is for "P"/phone-control-mode endpoints).
        print("[*] Keyed up -- mic audio will now be sent")
        self.keyed.set()

    def unkey(self):
        self.keyed.clear()
        print("[*] Unkeyed -- mic audio stopped")

    def hangup(self):
        self._send_full(FRAME_IAX, IAX_HANGUP,
                         encode_ies([(IE_CAUSE, ie_str("Normal"))]))
        self.hungup.set()


def gen_tone(seconds: float, freq: int = 1000, amplitude: int = 12000):
    """Generate a synthetic sine tone as a list of 160-sample u-law chunks,
    for self-test injection that doesn't depend on mic/ambient audio."""
    n_samples = int(SAMPLE_RATE * seconds)
    t = np.arange(n_samples)
    pcm = (amplitude * np.sin(2 * np.pi * freq * t / SAMPLE_RATE)).astype(np.int16)
    pcm_bytes = pcm.tobytes()
    chunk_bytes = FRAME_SAMPLES * 2
    chunks = [pcm_bytes[i:i + chunk_bytes]
              for i in range(0, len(pcm_bytes), chunk_bytes)]
    return [audioop.lin2ulaw(c, 2) for c in chunks if len(c) == chunk_bytes]


def run_listen_report(call: IaxCall, seconds: float):
    """Just listen and report received audio -- used as the 'B' side of a
    two-connection cross-link test."""
    call.selftest = True
    print(f"[*] Listening for {seconds}s...")
    time.sleep(seconds)
    print("\n===== LISTEN RESULT =====")
    print(f"Voice frames received: {call.rx_audio_frames}")
    print(f"Total audio bytes received: {call.rx_audio_bytes}")
    print(f"Peak PCM sample magnitude seen: {call.rx_audio_peak}")
    if call.rx_audio_peak > 500:
        print("=> Non-silent audio WAS received.")
    elif call.rx_audio_frames > 0:
        print("=> Frames arrived but appear to be silence.")
    else:
        print("=> NO voice frames received at all.")
    print("==========================\n")


def run_send_tone(call: IaxCall, seconds: float):
    print("[*] Waiting 2.5s for the newkey handshake to settle...")
    time.sleep(2.5)
    print(f"[*] Sending {seconds}s synthetic 1kHz test tone (keyed)...")
    call.keyed.set()
    for chunk in gen_tone(seconds):
        call._send_voice(chunk)
        time.sleep(FRAME_SAMPLES / SAMPLE_RATE)
    call.keyed.clear()
    print("[*] Done sending.")


def run_selftest(call: IaxCall):
    """Protocol-level echo test: inject a synthetic tone directly (no mic,
    no speakers needed), then report exactly what comes back."""
    call.selftest = True
    print("[*] Waiting 2.5s for the newkey handshake to settle...")
    time.sleep(2.5)

    print("[*] Sending 2s synthetic 1kHz test tone (keyed)...")
    call.keyed.set()
    for chunk in gen_tone(2.0):
        call._send_voice(chunk)
        time.sleep(FRAME_SAMPLES / SAMPLE_RATE)  # real-time pacing, 20ms/frame
    call.keyed.clear()
    print("[*] Unkeyed. Listening for echoed audio for 6s...")

    before_frames = call.rx_audio_frames
    deadline = time.time() + 6.0
    while time.time() < deadline:
        time.sleep(0.2)
    after_frames = call.rx_audio_frames

    print("\n===== SELF-TEST RESULT =====")
    print(f"Voice frames received total: {call.rx_audio_frames}")
    print(f"Voice frames received during listen window: "
          f"{after_frames - before_frames}")
    print(f"Total audio bytes received: {call.rx_audio_bytes}")
    print(f"Peak PCM sample magnitude seen: {call.rx_audio_peak} "
          f"(0=pure silence, ~32767=max)")
    if call.rx_audio_peak > 500:
        print("=> Non-silent audio WAS received back over the wire.")
    elif call.rx_audio_frames > 0:
        print("=> Voice frames arrived but all appear to be silence.")
    else:
        print("=> NO voice frames were received back at all.")
    print("=============================\n")


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Minimal IAX2 test client")
    ap.add_argument("--host", default=os.environ.get("MAXSTAR_HOST"),
                     help="Node's IP/hostname. Falls back to MAXSTAR_HOST.")
    ap.add_argument("--port", type=int,
                     default=int(os.environ.get("MAXSTAR_PORT", 4569)))
    ap.add_argument("--user", default=os.environ.get("MAXSTAR_USER"),
                     help="IAX2 peer username. Falls back to MAXSTAR_USER.")
    ap.add_argument("--secret", default=os.environ.get("MAXSTAR_SECRET"),
                     help="IAX2 peer secret. Falls back to the "
                          "MAXSTAR_SECRET env var; never hardcode this.")
    ap.add_argument("--node", default=os.environ.get("MAXSTAR_NODE"),
                     help="Node number to dial. Falls back to MAXSTAR_NODE.")
    ap.add_argument("--context",
                     default=os.environ.get("MAXSTAR_CONTEXT", "iax-client"))
    ap.add_argument("--selftest", action="store_true",
                     help="Inject a synthetic tone and report what echoes "
                          "back, without needing mic/speakers.")
    ap.add_argument("--send-tone", type=float, default=None,
                     help="Connect, wait for handshake, send N seconds of "
                          "synthetic tone, then hang up.")
    ap.add_argument("--listen", type=float, default=None,
                     help="Connect and just listen/report for N seconds.")
    ap.add_argument("--verbose", action="store_true",
                     help="Print raw [TX]/[RX] IAX2 frame trace (noisy; "
                          "useful for protocol-level debugging only).")
    args = ap.parse_args()
    required = (
        ("--host", "MAXSTAR_HOST", args.host),
        ("--user", "MAXSTAR_USER", args.user),
        ("--secret", "MAXSTAR_SECRET", args.secret),
        ("--node", "MAXSTAR_NODE", args.node),
    )
    missing = [f"{flag}/{env}" for flag, env, val in required if not val]
    if missing:
        print(f"[!] Missing required value(s): {', '.join(missing)}")
        sys.exit(1)

    call = IaxCall(args.host, args.port, args.user, args.secret, args.node,
                   args.context)
    call.verbose = args.verbose
    print(f"[*] Connecting to {args.host}:{args.port} as {args.user} "
          f"-> node {args.node}...")
    if not call.connect():
        print("[!] Failed to connect/accept within timeout")
        sys.exit(1)

    if args.selftest:
        run_selftest(call)
        call.hangup()
        time.sleep(0.3)
        return
    if args.send_tone is not None:
        run_send_tone(call, args.send_tone)
        call.hangup()
        time.sleep(0.3)
        return
    if args.listen is not None:
        run_listen_report(call, args.listen)
        call.hangup()
        time.sleep(0.3)
        return

    print("[*] Starting audio...")
    call.start_audio()

    print("""
Commands:
  k <Enter>  key up and transmit mic audio
  u <Enter>  unkey / stop transmitting
  q <Enter>  hang up and quit
""")
    try:
        while not call.hungup.is_set():
            cmd = input("> ").strip().lower()
            if cmd == "k":
                call.key()
            elif cmd == "u":
                call.unkey()
            elif cmd == "q":
                call.hangup()
                break
    except (KeyboardInterrupt, EOFError):
        call.hangup()

    time.sleep(0.3)


if __name__ == "__main__":
    main()
