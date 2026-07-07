#!/usr/bin/env python3
"""
Curses TUI for the maxstar IAX2 client, styled after a ham radio rig's
display (Icom IC-705-ish): dark panel, big VFO-style node readout,
segmented green/yellow/red level meters with dB scale ticks and a
peak-hold marker, PTT lamp.

Two views:
- monitor: live RX/TX audio-level meters and key state
- config: edit every MAXSTAR_* value and save back to .env

No new dependency -- curses is stdlib.

Note on the meters: IAX2 carries digitized audio, not an RF signal
report, so there's no real S-meter (RSSI) data to show. The meters
below are audio level relative to full scale (dBFS-style: 0 dB = max),
just drawn in the same segmented/zoned style a rig's meter uses.
"""

import curses
import math
import os
import sys
import threading

from iax_client import IaxCall, load_dotenv, send_dtmf_function

ENV_PATH = ".env"

# (env key, display label, mask value on screen)
FIELDS = [
    ("MAXSTAR_HOST", "Host", False),
    ("MAXSTAR_PORT", "Port", False),
    ("MAXSTAR_USER", "Username", False),
    ("MAXSTAR_SECRET", "Secret", True),
    ("MAXSTAR_NODE", "Node", False),
    ("MAXSTAR_CONTEXT", "Context", False),
]

PANEL_WIDTH = 62
METER_WIDTH = 48
METER_DECAY = 0.80    # per-tick decay of the main (fast) meter fill
PEAK_DECAY = 0.985     # per-tick decay of the slow peak-hold marker
FULL_SCALE = 32767

# Zone boundaries as a fraction of the meter width (green/yellow/red),
# same idea as a rig's S-meter having a "hot" red zone near the top.
ZONE_YELLOW = 0.70
ZONE_RED = 0.90

# dB-ish scale ticks (dBFS: 0 = full scale) spaced across the meter.
SCALE_TICKS = [(-40, 0.0), (-20, 0.35), (-10, 0.60), (-6, 0.75),
               (-3, 0.88), (0, 1.0)]

CP_TEXT = 1
CP_DIM = 2
CP_CYAN = 3
CP_GREEN = 4
CP_YELLOW = 5
CP_RED = 6
CP_FRAME = 7

# 5-row-tall block digits for the VFO-style node number readout.
BIG_DIGITS = {
    "0": ["███", "█ █", "█ █", "█ █", "███"],
    "1": ["  █", "  █", "  █", "  █", "  █"],
    "2": ["███", "  █", "███", "█  ", "███"],
    "3": ["███", "  █", "███", "  █", "███"],
    "4": ["█ █", "█ █", "███", "  █", "  █"],
    "5": ["███", "█  ", "███", "  █", "███"],
    "6": ["███", "█  ", "███", "█ █", "███"],
    "7": ["███", "  █", "  █", "  █", "  █"],
    "8": ["███", "█ █", "███", "█ █", "███"],
    "9": ["███", "█ █", "███", "  █", "███"],
    " ": ["   ", "   ", "   ", "   ", "   "],
}


def init_colors():
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(CP_TEXT, curses.COLOR_WHITE, bg)
    curses.init_pair(CP_DIM, curses.COLOR_WHITE, bg)
    curses.init_pair(CP_CYAN, curses.COLOR_CYAN, bg)
    curses.init_pair(CP_GREEN, curses.COLOR_GREEN, bg)
    curses.init_pair(CP_YELLOW, curses.COLOR_YELLOW, bg)
    curses.init_pair(CP_RED, curses.COLOR_RED, bg)
    curses.init_pair(CP_FRAME, curses.COLOR_CYAN, bg)


def read_config():
    load_dotenv(ENV_PATH)
    config = {key: os.environ.get(key, "") for key, _, _ in FIELDS}
    if not config["MAXSTAR_CONTEXT"]:
        config["MAXSTAR_CONTEXT"] = "iax-client"
    if not config["MAXSTAR_PORT"]:
        config["MAXSTAR_PORT"] = "4569"
    return config


def write_config(config):
    lines = [f"{key}={config.get(key, '')}" for key, _, _ in FIELDS]
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    for key, value in config.items():
        os.environ[key] = value


def level_fraction(level):
    """Log-scaled 0..1 fill fraction -- matches how a real meter gives
    more resolution to quiet audio than a linear PCM-magnitude bar."""
    if level <= 0:
        return 0.0
    frac = math.log10(1 + level) / math.log10(1 + FULL_SCALE)
    return max(0.0, min(1.0, frac))


def safe_addstr(stdscr, y, x, text, attr=0):
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass  # off-screen -- just skip this piece


def draw_box(stdscr, y0, x0, height, width, title=""):
    attr = curses.color_pair(CP_FRAME)
    safe_addstr(stdscr, y0, x0, "┌" + "─" * (width - 2) + "┐", attr)
    for row in range(1, height - 1):
        safe_addstr(stdscr, y0 + row, x0, "│", attr)
        safe_addstr(stdscr, y0 + row, x0 + width - 1, "│", attr)
    safe_addstr(stdscr, y0 + height - 1, x0,
                "└" + "─" * (width - 2) + "┘", attr)
    if title:
        safe_addstr(stdscr, y0, x0 + 2, f" {title} ",
                    curses.color_pair(CP_CYAN) | curses.A_BOLD)


def draw_big_number(stdscr, y, x, text):
    rows = ["", "", "", "", ""]
    for ch in text:
        glyph = BIG_DIGITS.get(ch, BIG_DIGITS[" "])
        for i in range(5):
            rows[i] += glyph[i] + " "
    for i, row in enumerate(rows):
        safe_addstr(stdscr, y + i, x, row,
                    curses.color_pair(CP_GREEN) | curses.A_BOLD)


def draw_meter(stdscr, y, x, width, display_level, peak_level, label):
    frac = level_fraction(display_level)
    peak_frac = level_fraction(peak_level)
    filled = int(frac * width)
    peak_pos = min(width - 1, int(peak_frac * width))

    safe_addstr(stdscr, y, x, f"{label} ", curses.color_pair(CP_CYAN) |
                curses.A_BOLD)
    bar_x = x + len(label) + 1
    safe_addstr(stdscr, y, bar_x - 1, "▕", curses.color_pair(CP_FRAME))
    for i in range(width):
        zone = i / width
        if zone < ZONE_YELLOW:
            pair = CP_GREEN
        elif zone < ZONE_RED:
            pair = CP_YELLOW
        else:
            pair = CP_RED
        if i < filled:
            ch, attr = "█", curses.color_pair(pair) | curses.A_BOLD
        elif peak_frac > 0 and i == peak_pos and peak_pos >= filled:
            ch, attr = "▏", curses.color_pair(CP_TEXT) | curses.A_BOLD
        else:
            ch, attr = "░", curses.color_pair(CP_DIM) | curses.A_DIM
        safe_addstr(stdscr, y, bar_x + i, ch, attr)
    safe_addstr(stdscr, y, bar_x + width, "▏", curses.color_pair(CP_FRAME))

    # dB scale ticks under the bar, aligned to their fractional position.
    scale = [" "] * width
    for db, pos in SCALE_TICKS:
        i = min(width - 1, int(pos * width))
        text = f"{db:+d}" if db != 0 else "0"
        for k, c in enumerate(text):
            if i + k < width:
                scale[i + k] = c
    safe_addstr(stdscr, y + 1, bar_x, "".join(scale),
                curses.color_pair(CP_DIM) | curses.A_DIM)


class App:
    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.config = read_config()
        self.call = None
        self.status = "disconnected"
        self.selected = 0
        self.editing = False
        self.edit_buf = ""
        self.rx_display = 0.0
        self.tx_display = 0.0
        self.rx_peak = 0.0
        self.tx_peak = 0.0

        self.link_mode = None   # None, "connect", or "disconnect"
        self.link_buf = ""
        self.link_status = ""
        self.link_busy = False

        required = ("MAXSTAR_HOST", "MAXSTAR_USER", "MAXSTAR_SECRET",
                    "MAXSTAR_NODE")
        self.view = "monitor" if all(self.config[k] for k in required) \
            else "config"
        if self.view == "monitor":
            self.connect()

    def connect(self):
        """Kick off connect() in the background so the UI (and 'q' to
        quit) stays responsive instead of freezing for up to 8s."""
        cfg = self.config
        self.status = "connecting"
        call = IaxCall(cfg["MAXSTAR_HOST"], int(cfg["MAXSTAR_PORT"]),
                       cfg["MAXSTAR_USER"], cfg["MAXSTAR_SECRET"],
                       cfg["MAXSTAR_NODE"], cfg["MAXSTAR_CONTEXT"])
        self.call = call

        def worker():
            if call.connect(timeout=8.0):
                # Guard against quit/reconnect racing this thread: don't
                # spin up audio streams for a call that's already being
                # (or has been) torn down, or those PortAudio threads
                # never get closed and the process hangs on exit.
                if self.call is call and not call.hungup.is_set():
                    self.status = "connected"
                    call.start_audio()
            else:
                if self.call is call:
                    self.status = "failed"

        threading.Thread(target=worker, daemon=True).start()

    def reconnect(self):
        self._shutdown_call(self.call)
        self.connect()

    def run(self):
        curses.set_escdelay(25)  # default ~1000ms makes Esc feel unresponsive
        init_colors()
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.timeout(50)
        running = True
        try:
            while running:
                ch = self.stdscr.getch()
                running = self.handle_key(ch)
                self.draw()
        finally:
            self._shutdown_call(self.call)

    @staticmethod
    def _shutdown_call(call):
        if not call:
            return
        try:
            call.hangup()
        except OSError:
            pass
        # sounddevice streams run on real PortAudio threads (not Python
        # daemon threads) -- close them explicitly or the process hangs
        # around after curses exits instead of returning to the shell.
        for stream in (call._out_stream, call._in_stream):
            if stream:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    # ---- input ------------------------------------------------------

    def handle_key(self, ch):
        if ch == -1:
            return True
        if self.view == "config":
            return self.handle_config_key(ch)
        return self.handle_monitor_key(ch)

    def handle_monitor_key(self, ch):
        if self.link_mode is not None:
            return self.handle_link_key(ch)

        if ch in (ord("q"), ord("Q")):
            return False
        elif ch in (ord("k"), ord("K")):
            if self.call:
                self.call.key()
        elif ch in (ord("u"), ord("U")):
            if self.call:
                self.call.unkey()
        elif ch in (ord("c"), ord("C")):
            self.view = "config"
            self.selected = 0
        elif ch in (ord("l"), ord("L")):
            if self.call and not self.link_busy:
                self.link_mode = "connect"
                self.link_buf = ""
        elif ch in (ord("d"), ord("D")):
            if self.call and not self.link_busy:
                self.link_mode = "disconnect"
                self.link_buf = ""
        return True

    def handle_link_key(self, ch):
        if ch == 27:  # Esc cancels
            self.link_mode = None
            self.link_buf = ""
        elif ch in (curses.KEY_ENTER, 10, 13):
            if self.link_buf:
                self.start_link(self.link_mode, self.link_buf)
            self.link_mode = None
            self.link_buf = ""
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.link_buf = self.link_buf[:-1]
        elif ord("0") <= ch <= ord("9"):
            self.link_buf += chr(ch)
        return True

    def start_link(self, mode, node):
        """Dial the app_rpt function code for connect/disconnect as real
        DTMF tone audio, in the background so the UI stays responsive."""
        call = self.call
        func = "3" if mode == "connect" else "1"
        digits = f"*{func}{node}"
        self.link_busy = True
        self.link_status = f"sending {digits} ..."

        def worker():
            try:
                send_dtmf_function(call, digits)
                self.link_status = (
                    f"{'connected to' if mode == 'connect' else 'disconnect sent for'} "
                    f"{node}")
            finally:
                self.link_busy = False

        threading.Thread(target=worker, daemon=True).start()

    def handle_config_key(self, ch):
        if self.editing:
            if ch in (curses.KEY_ENTER, 10, 13):
                key = FIELDS[self.selected][0]
                self.config[key] = self.edit_buf
                self.editing = False
            elif ch == 27:  # Esc cancels this field's edit
                self.editing = False
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.edit_buf = self.edit_buf[:-1]
            elif 32 <= ch < 127:
                self.edit_buf += chr(ch)
            return True

        if ch == curses.KEY_UP:
            self.selected = (self.selected - 1) % len(FIELDS)
        elif ch == curses.KEY_DOWN:
            self.selected = (self.selected + 1) % len(FIELDS)
        elif ch in (curses.KEY_ENTER, 10, 13):
            key = FIELDS[self.selected][0]
            self.edit_buf = self.config.get(key, "")
            self.editing = True
        elif ch in (ord("s"), ord("S")):
            write_config(self.config)
            self.view = "monitor"
            self.reconnect()
        elif ch == 27 and self.call:  # Esc: back without saving
            self.view = "monitor"
        elif ch in (ord("q"), ord("Q")):
            return False
        return True

    # ---- drawing ------------------------------------------------------

    def draw(self):
        self.stdscr.erase()
        try:
            if self.view == "config":
                self.draw_config()
            else:
                self.draw_monitor()
        except curses.error:
            pass  # window too small -- ignore this frame
        self.stdscr.refresh()

    def draw_config(self):
        stdscr = self.stdscr
        draw_box(stdscr, 0, 0, 12, PANEL_WIDTH, title="MAXSTAR CONFIG")
        safe_addstr(stdscr, 1, 2,
                    "↑/↓ select  Enter edit  s save+connect  "
                    "Esc back  q quit",
                    curses.color_pair(CP_DIM) | curses.A_DIM)
        for i, (key, label, secret) in enumerate(FIELDS):
            value = self.config.get(key, "")
            if self.editing and self.selected == i:
                display = self.edit_buf
            elif secret and value:
                display = "•" * len(value)
            else:
                display = value
            row = 3 + i
            marker = "▸" if i == self.selected else " "
            label_attr = (curses.color_pair(CP_CYAN) | curses.A_BOLD
                          if i == self.selected
                          else curses.color_pair(CP_TEXT))
            safe_addstr(stdscr, row, 2, f"{marker} {label:10s}: ",
                        label_attr)
            value_attr = (curses.color_pair(CP_YELLOW) | curses.A_BOLD
                          if self.editing and self.selected == i
                          else curses.color_pair(CP_TEXT))
            safe_addstr(stdscr, row, 2 + 14, display, value_attr)

    def draw_monitor(self):
        stdscr = self.stdscr
        cfg = self.config
        height = 19
        draw_box(stdscr, 0, 0, height, PANEL_WIDTH, title="MAXSTAR")

        status_colors = {"connected": CP_GREEN, "connecting": CP_YELLOW,
                          "failed": CP_RED, "disconnected": CP_DIM}
        status_attr = curses.color_pair(
            status_colors.get(self.status, CP_DIM)) | curses.A_BOLD
        safe_addstr(stdscr, 1, PANEL_WIDTH - len(self.status) - 4,
                    f"[{self.status.upper()}]", status_attr)

        safe_addstr(stdscr, 1, 2,
                    f"{cfg['MAXSTAR_USER']}@{cfg['MAXSTAR_HOST']}:"
                    f"{cfg['MAXSTAR_PORT']}",
                    curses.color_pair(CP_TEXT))

        safe_addstr(stdscr, 3, 2, "NODE", curses.color_pair(CP_DIM) |
                    curses.A_DIM)
        draw_big_number(stdscr, 4, 2, cfg["MAXSTAR_NODE"])

        keyed = bool(self.call and self.call.keyed.is_set())
        lamp_attr = (curses.color_pair(CP_RED) | curses.A_BOLD if keyed
                     else curses.color_pair(CP_DIM) | curses.A_DIM)
        safe_addstr(stdscr, 4, 28, "● TX" if keyed else "○ rx",
                    lamp_attr)

        rx_instant = self.call.rx_level if self.call else 0
        tx_instant = self.call.tx_level if self.call else 0
        self.rx_display = max(rx_instant, self.rx_display * METER_DECAY)
        self.tx_display = max(tx_instant, self.tx_display * METER_DECAY)
        self.rx_peak = max(rx_instant, self.rx_peak * PEAK_DECAY)
        self.tx_peak = max(tx_instant, self.tx_peak * PEAK_DECAY)

        draw_meter(stdscr, 10, 2, METER_WIDTH, self.rx_display,
                   self.rx_peak, "RX")
        draw_meter(stdscr, 13, 2, METER_WIDTH, self.tx_display,
                   self.tx_peak, "TX")

        if self.link_mode is not None:
            label = ("CONNECT to node: " if self.link_mode == "connect"
                      else "DISCONNECT node: ")
            safe_addstr(stdscr, 16, 2, f"{label}{self.link_buf}_",
                        curses.color_pair(CP_YELLOW) | curses.A_BOLD)
        elif self.link_busy:
            safe_addstr(stdscr, 16, 2, f"» {self.link_status}",
                        curses.color_pair(CP_YELLOW) | curses.A_BOLD)
        elif self.link_status:
            safe_addstr(stdscr, 16, 2, f"» {self.link_status}",
                        curses.color_pair(CP_GREEN))

        safe_addstr(stdscr, height - 2, 2,
                    "k key  u unkey  l link  d disconnect  "
                    "c config  q quit",
                    curses.color_pair(CP_DIM) | curses.A_DIM)


def main():
    # iax_client.py has stray print() calls (connection status, protocol
    # traces) that would otherwise corrupt curses' exclusive control of
    # the terminal -- silence stdout/stderr for the duration of the UI.
    devnull = open(os.devnull, "w")
    real_stdout, real_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = devnull
    try:
        curses.wrapper(lambda stdscr: App(stdscr).run())
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr
        devnull.close()


if __name__ == "__main__":
    main()
