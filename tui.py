#!/usr/bin/env python3
"""
Curses TUI for the maxstar IAX2 client.

Two views:
- monitor: live RX/TX signal meters and key state
- config: edit every MAXSTAR_* value and save back to .env

No new dependency -- curses is stdlib.
"""

import curses
import os
import sys
import threading

from iax_client import IaxCall, load_dotenv

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

METER_WIDTH = 40
METER_DECAY = 0.85  # per redraw tick, gives peak-hold-with-decay ballistics


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


def meter_bar(level, label):
    filled = max(0, min(METER_WIDTH, int((level / 32767) * METER_WIDTH)))
    bar = "#" * filled + "-" * (METER_WIDTH - filled)
    return f"{label} [{bar}] {int(level):5d}"


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
        self.status = "connecting..."
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
                    self.status = "failed to connect"

        threading.Thread(target=worker, daemon=True).start()

    def reconnect(self):
        self._shutdown_call(self.call)
        self.connect()

    def run(self):
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
        return True

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
            pass  # window too small for a line -- ignore this frame
        self.stdscr.refresh()

    def draw_config(self):
        stdscr = self.stdscr
        stdscr.addstr(0, 0, "maxstar config")
        stdscr.addstr(1, 0, "Up/Down select * Enter edit * s save+connect "
                             "* Esc back * q quit")
        for i, (key, label, secret) in enumerate(FIELDS):
            value = self.config.get(key, "")
            if self.editing and self.selected == i:
                display = self.edit_buf
            elif secret and value:
                display = "*" * len(value)
            else:
                display = value
            marker = ">" if i == self.selected else " "
            stdscr.addstr(3 + i, 0, f"{marker} {label:10s}: {display}")

    def draw_monitor(self):
        stdscr = self.stdscr
        cfg = self.config
        stdscr.addstr(
            0, 0,
            f"maxstar -- {cfg['MAXSTAR_USER']}@{cfg['MAXSTAR_HOST']}:"
            f"{cfg['MAXSTAR_PORT']} -> node {cfg['MAXSTAR_NODE']}")
        keyed = bool(self.call and self.call.keyed.is_set())
        stdscr.addstr(1, 0, f"status: {self.status}   "
                            f"keyed: {'YES' if keyed else 'no'}")

        rx_instant = self.call.rx_level if self.call else 0
        tx_instant = self.call.tx_level if self.call else 0
        self.rx_display = max(rx_instant, self.rx_display * METER_DECAY)
        self.tx_display = max(tx_instant, self.tx_display * METER_DECAY)

        stdscr.addstr(3, 0, meter_bar(self.rx_display, "RX"))
        stdscr.addstr(4, 0, meter_bar(self.tx_display, "TX"))
        stdscr.addstr(6, 0, "k=key  u=unkey  c=config  q=quit")


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
