#!/usr/bin/env python3
"""
Curses TUI for the maxstar IAX2 client, styled after a ham radio rig's
display (Icom IC-705-ish): dark panel, big VFO-style node readout,
segmented green/yellow/red level meters with dB scale ticks and a
peak-hold marker, PTT lamp.

Three views:
- monitor: live RX/TX audio-level meters and key state
- config: edit every MAXSTAR_* value and save back to .env
- nodes: currently-connected nodes (from stats.allstarlink.org) and a
  favorites list you can connect/disconnect from directly

No new dependency -- curses is stdlib.

Note on the meters: IAX2 carries digitized audio, not an RF signal
report, so there's no real S-meter (RSSI) data to show. The meters
below are audio level relative to full scale (dBFS-style: 0 dB = max),
just drawn in the same segmented/zoned style a rig's meter uses.
"""

import curses
import json
import math
import os
import subprocess
import sys
import threading
import time

from iax_client import IaxCall, load_dotenv, send_dtmf_function
from allstarlink_stats import (fetch_node_summary, fetch_connected_nodes,
                                fetch_link_count)

ENV_PATH = ".env"
FAVORITES_PATH = "favorites.json"
CONNECTED_REFRESH_SECONDS = 15
LINK_GRACE_SECONDS = 30  # > the API's observed worst-case connect/disconnect lag

# Safety net for the spacebar PTT toggle: unlike a real hold-to-talk
# button, a toggle can be left keyed by accident with nothing to force
# an unkey (no release event). Auto-unkey after this long continuously
# keyed, same idea as a repeater's own time-out timer.
TX_TIMEOUT_SECONDS = 90

# Link history is logged on the node's own Pi (see pi_logger/), not by
# this client -- so it keeps recording even when maxstar isn't running.
# This just SSHes in to read (or prune) that log. Relies on an SSH key
# already loaded in the agent, same as the manual rpt.conf edits earlier
# in this project -- never runs anything with sudo (see the standing
# no-sudo-over-ssh rule).
HISTORY_REFRESH_SECONDS = 20
REMOTE_LOG_TAIL_LINES = 500  # bounds what's pulled over SSH each refresh
SSH_TIMEOUT = 8

# (env key, display label, mask value on screen)
FIELDS = [
    ("MAXSTAR_HOST", "Host", False),
    ("MAXSTAR_PORT", "Port", False),
    ("MAXSTAR_USER", "Username", False),
    ("MAXSTAR_SECRET", "Secret", True),
    ("MAXSTAR_NODE", "Node", False),
    ("MAXSTAR_CONTEXT", "Context", False),
    ("MAXSTAR_SSH_USER", "SSH User", False),
    ("MAXSTAR_SSH_LOGGER_DIR", "SSH Logger Dir", False),
]

MIN_WIDTH = 50
MIN_HEIGHT = 20
METER_MAX_WIDTH = 100  # cap so an ultra-wide terminal doesn't look silly
METER_DECAY = 0.80    # per-tick decay of the main (fast) meter fill
PEAK_DECAY = 0.985     # per-tick decay of the slow peak-hold marker
FULL_SCALE = 32767
RX_ACTIVE_THRESHOLD = 200  # above the digital noise floor, below real speech

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
CP_RX_ON = 8   # filled badge (background fill), not just colored text
CP_TX_ON = 9

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
    curses.init_pair(CP_RX_ON, curses.COLOR_BLACK, curses.COLOR_GREEN)
    curses.init_pair(CP_TX_ON, curses.COLOR_BLACK, curses.COLOR_RED)


def read_config():
    load_dotenv(ENV_PATH)
    config = {key: os.environ.get(key, "") for key, _, _ in FIELDS}
    if not config["MAXSTAR_CONTEXT"]:
        config["MAXSTAR_CONTEXT"] = "iax-client"
    if not config["MAXSTAR_PORT"]:
        config["MAXSTAR_PORT"] = "4569"
    if not config["MAXSTAR_SSH_USER"]:
        config["MAXSTAR_SSH_USER"] = "asl"
    if not config["MAXSTAR_SSH_LOGGER_DIR"]:
        config["MAXSTAR_SSH_LOGGER_DIR"] = "maxstar-logger"
    return config


def remote_log_path(cfg):
    return f"{cfg['MAXSTAR_SSH_LOGGER_DIR']}/link_history.jsonl"


def remote_logger_path(cfg):
    return f"{cfg['MAXSTAR_SSH_LOGGER_DIR']}/node_link_logger.py"


def run_ssh(cfg, remote_command, timeout=SSH_TIMEOUT):
    """Run one non-interactive, non-sudo command on the node's Pi over
    SSH. Returns (stdout_lines, error) -- error is None on success, or
    a short message that's safe to show directly in the UI. Never
    invokes sudo -- this is read/prune access to our own log file
    only, owned by the SSH user itself, never a privileged operation."""
    user, host = cfg["MAXSTAR_SSH_USER"], cfg["MAXSTAR_HOST"]
    if not host:
        return [], "no SSH host configured"
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             f"{user}@{host}", remote_command],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"ssh failed: {exc}"
    if result.returncode != 0:
        stderr_lines = (result.stderr or "").strip().splitlines()
        detail = stderr_lines[-1] if stderr_lines else \
            f"exit {result.returncode}"
        return [], f"ssh error: {detail}"
    return result.stdout.splitlines(), None


def write_config(config):
    lines = [f"{key}={config.get(key, '')}" for key, _, _ in FIELDS]
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(lines) + "\n")
    for key, value in config.items():
        os.environ[key] = value


def load_favorites():
    if not os.path.exists(FAVORITES_PATH):
        return []
    try:
        with open(FAVORITES_PATH) as f:
            data = json.load(f)
        return [str(n) for n in data] if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_favorites(favorites):
    with open(FAVORITES_PATH, "w") as f:
        json.dump(favorites, f, indent=2)


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


def draw_badge(stdscr, y, x, label, active, on_pair):
    """A status badge occupying a fixed 6-wide x 3-tall footprint in
    both states, so it's never actually bigger or smaller -- just a
    dim border outline when inactive vs. a solid color fill (plain
    blanks, no border glyphs mixed in) when active. A solid fill will
    always look visually heavier than a same-size thin-line outline --
    that's just how filled vs. outlined shapes read to the eye, not an
    actual size difference."""
    box_w = 6
    if active:
        attr = curses.color_pair(on_pair) | curses.A_BOLD
        safe_addstr(stdscr, y, x, " " * box_w, attr)
        safe_addstr(stdscr, y + 1, x, f"{label:^{box_w}}", attr)
        safe_addstr(stdscr, y + 2, x, " " * box_w, attr)
    else:
        attr = curses.color_pair(CP_DIM) | curses.A_DIM
        safe_addstr(stdscr, y, x, "┌" + "─" * (box_w - 2) + "┐", attr)
        safe_addstr(stdscr, y + 1, x,
                    "│" + f"{label:^{box_w - 2}}" + "│", attr)
        safe_addstr(stdscr, y + 2, x, "└" + "─" * (box_w - 2) + "┘", attr)


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

        self.keyed_since = None  # time.time() PTT was toggled on, else None

        self.favorites = load_favorites()
        self.node_info_cache = {}    # node number -> summary dict / "loading"
        self.fetching_nodes = set()  # node numbers with a fetch in flight
        self.link_count_cache = {}   # node number -> int link count / None
        self.fetching_link_counts = set()
        self.connected_nodes = []
        self.connected_refreshing = False
        self.last_connected_refresh = 0.0
        self.link_lock_until = 0.0  # suppress refreshes until this time
        self.monitor_selected = 0  # selection within the connected-nodes panel
        self.nodes_selected = 0
        self.nodes_add_mode = False
        self.nodes_add_buf = ""

        # Who connected to this node and when -- logged on the node's
        # own Pi (pi_logger/node_link_logger.py), not by this client,
        # so it keeps recording even when maxstar isn't running. This
        # just displays whatever that log currently has, fetched over
        # SSH. See refresh_link_history()/clean_link_history().
        self.link_history = []
        self.history_offset = 0
        self.history_refreshing = False
        self.last_history_refresh = 0.0
        self.history_error = None
        self.history_busy = False
        self.history_clean_mode = False
        self.history_clean_buf = ""

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
        self.keyed_since = None

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

    # ---- stats.allstarlink.org lookups -------------------------------

    def fetch_node_info(self, node):
        """Populate node_info_cache[node] in the background. Safe to call
        repeatedly -- skips if already cached or a fetch is in flight."""
        if node in self.node_info_cache or node in self.fetching_nodes:
            return
        self.fetching_nodes.add(node)

        def worker():
            try:
                self.node_info_cache[node] = fetch_node_summary(node)
            finally:
                self.fetching_nodes.discard(node)

        threading.Thread(target=worker, daemon=True).start()

    def fetch_link_count_for(self, node):
        """Populate link_count_cache[node] in the background -- how many
        nodes `node` itself is currently linked to. Safe to call
        repeatedly, same guard pattern as fetch_node_info()."""
        if node in self.link_count_cache or node in self.fetching_link_counts:
            return
        self.fetching_link_counts.add(node)

        def worker():
            try:
                self.link_count_cache[node] = fetch_link_count(node)
            finally:
                self.fetching_link_counts.discard(node)

        threading.Thread(target=worker, daemon=True).start()

    def refresh_link_history(self, force=False):
        """Pull the node-side log (see pi_logger/node_link_logger.py)
        over SSH. Only relevant while the history screen is open --
        unlike refresh_connected_nodes(), nothing else on screen needs
        this, so callers gate this on self.view == "history"."""
        due = time.time() - self.last_history_refresh > \
            HISTORY_REFRESH_SECONDS
        if self.history_refreshing or not (force or due):
            return
        self.history_refreshing = True
        cfg = self.config

        def worker():
            try:
                remote_cmd = f"tail -n {REMOTE_LOG_TAIL_LINES} " \
                             f"{remote_log_path(cfg)}"
                lines, error = run_ssh(cfg, remote_cmd)
                if error is None:
                    entries = []
                    for line in lines:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
                    self.link_history = entries
                    self.history_error = None
                else:
                    self.history_error = error
                self.last_history_refresh = time.time()
            finally:
                self.history_refreshing = False

        threading.Thread(target=worker, daemon=True).start()

    def clean_link_history(self, keep_days):
        """Trigger the node-side logger's own --clean mode over SSH --
        prunes entries older than keep_days and rewrites the log file
        in place, on the Pi. Never touched locally: this client has no
        local copy of the log to prune, only whatever's currently
        fetched for display."""
        self.history_busy = True
        self.link_status = f"cleaning history (keep {keep_days}d) ..."
        cfg = self.config

        def worker():
            try:
                remote_cmd = (f"python3 {remote_logger_path(cfg)} --clean "
                              f"--keep-days {keep_days} --log "
                              f"{remote_log_path(cfg)}")
                lines, error = run_ssh(cfg, remote_cmd, timeout=20)
                if error is None:
                    self.link_status = lines[-1] if lines else "cleaned"
                    self.refresh_link_history(force=True)
                else:
                    self.link_status = error
            finally:
                self.history_busy = False

        threading.Thread(target=worker, daemon=True).start()

    def refresh_connected_nodes(self, force=False):
        # Hard lock, not just a nudged timestamp: the public stats API
        # has been observed lagging up to ~25s behind a real disconnect
        # (and connect isn't guaranteed fast either) -- longer than our
        # own refresh interval. Any refresh at all (periodic or forced)
        # during this window would risk overwriting start_link()'s
        # correct optimistic update with the API's still-stale answer.
        if time.time() < self.link_lock_until:
            return
        due = time.time() - self.last_connected_refresh > \
            CONNECTED_REFRESH_SECONDS
        if self.connected_refreshing or not (force or due):
            return
        self.connected_refreshing = True
        node = self.config["MAXSTAR_NODE"]

        def worker():
            try:
                nodes = fetch_connected_nodes(node)
                # The lock guard above only stops a *new* fetch from
                # starting -- it doesn't stop one already in flight (kicked
                # off just before a connect/disconnect) from finishing
                # afterward and clobbering start_link()'s optimistic update
                # with a response that predates it. Re-check here too.
                if time.time() < self.link_lock_until:
                    return
                self.connected_nodes = nodes
                self.last_connected_refresh = time.time()
                for n in nodes:
                    self.fetch_link_count_for(n["number"])
            finally:
                self.connected_refreshing = False

        threading.Thread(target=worker, daemon=True).start()

    def run(self):
        curses.set_escdelay(50)  # default ~1000ms makes Esc feel unresponsive;
        # 50ms (vim's usual default) still leaves headroom to distinguish
        # a bare Esc from the start of an arrow-key escape sequence
        init_colors()
        curses.curs_set(0)
        self.stdscr.nodelay(True)
        self.stdscr.timeout(50)
        running = True
        try:
            while running:
                ch = self.stdscr.getch()
                running = self.handle_key(ch)
                self.check_tx_timeout()
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
        if ch == -1 or ch == curses.KEY_RESIZE:
            return True
        if self.view == "config":
            return self.handle_config_key(ch)
        if self.view == "nodes":
            return self.handle_nodes_key(ch)
        if self.view == "history":
            return self.handle_history_key(ch)
        return self.handle_monitor_key(ch)

    def handle_monitor_key(self, ch):
        if self.link_mode is not None:
            return self.handle_link_key(ch)

        if ch in (ord("q"), ord("Q")):
            return False
        elif ch == ord(" "):
            self.toggle_ptt()
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
        elif ch == curses.KEY_UP:
            if self.connected_nodes:
                self.monitor_selected = (self.monitor_selected - 1) % \
                    len(self.connected_nodes)
        elif ch == curses.KEY_DOWN:
            if self.connected_nodes:
                self.monitor_selected = (self.monitor_selected + 1) % \
                    len(self.connected_nodes)
        elif ch in (ord("x"), ord("X")):
            if self.connected_nodes and self.call and not self.link_busy:
                node = self.connected_nodes[
                    self.monitor_selected % len(self.connected_nodes)]
                self.start_link("disconnect", node["number"])
        elif ch in (ord("h"), ord("H")):
            self.view = "history"
            self.history_offset = 0
            self.refresh_link_history()
        elif ch in (ord("n"), ord("N")):
            self.view = "nodes"
            self.nodes_selected = 0
            # Not force=True: a forced refresh here would immediately
            # hit the public stats API, which can lag 10-25s behind a
            # just-sent connect/disconnect, undoing start_link()'s
            # optimistic update with stale data. The periodic background
            # refresh (already running regardless of view) keeps this
            # reasonably fresh without that race.
            self.refresh_connected_nodes()
            for node in self.favorites:
                self.fetch_node_info(node)
        return True

    def toggle_ptt(self):
        if not self.call:
            return
        if self.call.keyed.is_set():
            self.call.unkey()
            self.keyed_since = None
        else:
            self.call.key()
            self.keyed_since = time.time()

    def check_tx_timeout(self):
        """Safety net for the toggle-style PTT: a toggle has no release
        event to fall back on like a real hold-to-talk button would, so
        an accidental or forgotten keyup can leave the mic open
        indefinitely. Auto-unkey past TX_TIMEOUT_SECONDS, same idea as
        a repeater's own time-out timer."""
        if self.keyed_since is None or not self.call:
            return
        if time.time() - self.keyed_since > TX_TIMEOUT_SECONDS:
            self.call.unkey()
            self.keyed_since = None
            self.link_status = (
                f"TX timeout -- auto-unkeyed after {TX_TIMEOUT_SECONDS}s")

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
        """Dial the app_rpt function code for connect/disconnect via
        native IAX2 DTMF signaling, in the background so the UI stays
        responsive while it's happening."""
        call = self.call
        func = "3" if mode == "connect" else "1"
        digits = f"*{func}{node}"
        self.link_busy = True
        self.link_status = f"sending {digits} ..."

        def worker():
            try:
                send_dtmf_function(call, digits)
                # Update our own view of connected_nodes immediately --
                # the public stats API can lag 10-25s (observed) before
                # it reflects a disconnect, so waiting on the next
                # periodic refresh left a disconnected node visible for
                # a long time. Also push the refresh timer out so that
                # slow-to-update API response doesn't immediately
                # overwrite this with stale data.
                if mode == "disconnect":
                    self.connected_nodes = [n for n in self.connected_nodes
                                            if n["number"] != node]
                    self.link_status = f"disconnected {node}"
                else:
                    if not any(n["number"] == node
                               for n in self.connected_nodes):
                        info = (self.node_info_cache.get(node) or
                                fetch_node_summary(node) or
                                {"callsign": "", "location": "",
                                 "sitename": "", "affiliation": ""})
                        entry = dict(info, number=node)
                        self.node_info_cache[node] = info
                        self.connected_nodes = self.connected_nodes + [entry]
                    self.link_status = f"connected to {node}"
                # Suppress any refresh (periodic or forced) for a while
                # -- longer than the API's observed worst-case lag --
                # so it doesn't overwrite this optimistic update with a
                # still-stale response. Deliberately NOT touching
                # last_connected_refresh: once the lock lifts, the
                # normal due-check should fire an immediate re-sync
                # rather than waiting out another full interval.
                self.link_lock_until = time.time() + LINK_GRACE_SECONDS
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

    def _node_rows(self):
        """Flat, rebuilt-each-time list of (kind, node_number) for the
        nodes screen: currently-connected nodes, then favorites, then a
        sentinel "add favorite" row."""
        rows = [("connected", n["number"]) for n in self.connected_nodes]
        rows += [("favorite", n) for n in self.favorites]
        rows.append(("add", None))
        return rows

    def handle_nodes_key(self, ch):
        if self.nodes_add_mode:
            if ch == 27:
                self.nodes_add_mode = False
                self.nodes_add_buf = ""
            elif ch in (curses.KEY_ENTER, 10, 13):
                node = self.nodes_add_buf
                if node and node not in self.favorites:
                    self.favorites.append(node)
                    save_favorites(self.favorites)
                    self.fetch_node_info(node)
                self.nodes_add_mode = False
                self.nodes_add_buf = ""
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.nodes_add_buf = self.nodes_add_buf[:-1]
            elif ord("0") <= ch <= ord("9"):
                self.nodes_add_buf += chr(ch)
            return True

        rows = self._node_rows()
        if ch in (ord("q"), ord("Q")):
            return False
        elif ch in (27, ord("n"), ord("N")):
            self.view = "monitor"
        elif ch == curses.KEY_UP:
            self.nodes_selected = (self.nodes_selected - 1) % len(rows)
        elif ch == curses.KEY_DOWN:
            self.nodes_selected = (self.nodes_selected + 1) % len(rows)
        elif ch in (curses.KEY_ENTER, 10, 13):
            kind, node = rows[self.nodes_selected]
            if kind == "add":
                self.nodes_add_mode = True
                self.nodes_add_buf = ""
            elif node and self.call and not self.link_busy:
                self.start_link("connect", node)
        elif ch in (ord("x"), ord("X")):
            kind, node = rows[self.nodes_selected]
            if kind in ("connected", "favorite") and node and self.call \
                    and not self.link_busy:
                self.start_link("disconnect", node)
        elif ch in (ord("r"), ord("R")):
            kind, node = rows[self.nodes_selected]
            if kind == "favorite":
                self.favorites.remove(node)
                save_favorites(self.favorites)
                self.nodes_selected = min(self.nodes_selected,
                                          len(self._node_rows()) - 1)
        elif ch in (ord("a"), ord("A")):
            self.nodes_add_mode = True
            self.nodes_add_buf = ""
        return True

    def handle_history_key(self, ch):
        if self.history_clean_mode:
            if ch == 27:  # Esc cancels
                self.history_clean_mode = False
                self.history_clean_buf = ""
            elif ch in (curses.KEY_ENTER, 10, 13):
                if self.history_clean_buf:
                    self.clean_link_history(int(self.history_clean_buf))
                self.history_clean_mode = False
                self.history_clean_buf = ""
            elif ch in (curses.KEY_BACKSPACE, 127, 8):
                self.history_clean_buf = self.history_clean_buf[:-1]
            elif ord("0") <= ch <= ord("9"):
                self.history_clean_buf += chr(ch)
            return True

        if ch in (ord("q"), ord("Q")):
            return False
        elif ch in (27, ord("h"), ord("H")):
            self.view = "monitor"
        elif ch == curses.KEY_UP:
            self.history_offset = min(self.history_offset + 1,
                                      max(0, len(self.link_history) - 1))
        elif ch == curses.KEY_DOWN:
            self.history_offset = max(self.history_offset - 1, 0)
        elif ch in (ord("r"), ord("R")):
            self.refresh_link_history(force=True)
        elif ch in (ord("c"), ord("C")):
            if not self.history_busy:
                self.history_clean_mode = True
                self.history_clean_buf = ""
        return True

    # ---- drawing ------------------------------------------------------

    def draw(self):
        self.stdscr.erase()
        self.refresh_connected_nodes()  # kept fresh regardless of view
        height, width = self.stdscr.getmaxyx()
        try:
            if height < MIN_HEIGHT or width < MIN_WIDTH:
                safe_addstr(self.stdscr, 0, 0,
                            f"Terminal too small ({width}x{height}) -- "
                            f"resize to at least {MIN_WIDTH}x{MIN_HEIGHT}",
                            curses.color_pair(CP_YELLOW) | curses.A_BOLD)
            elif self.view == "config":
                self.draw_config()
            elif self.view == "nodes":
                self.draw_nodes()
            elif self.view == "history":
                self.draw_history()
            else:
                self.draw_monitor()
        except curses.error:
            pass  # window too small -- ignore this frame
        self.stdscr.refresh()

    def draw_config(self):
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        draw_box(stdscr, 0, 0, height, width, title="MAXSTAR CONFIG")
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

    @staticmethod
    def _format_node_summary(node, info, max_width=200, link_count=None):
        if info is None:
            base = f"{node:<8} (unknown -- lookup failed)"
        else:
            bits = [info["callsign"], info["location"], info["sitename"],
                    info["affiliation"]]
            detail = "  ".join(b for b in bits if b)
            base = f"{node:<8} {detail}"
        base = base[:max_width]
        if link_count is not None:
            base += f"  ({link_count} link{'s' if link_count != 1 else ''})"
        return base

    def _node_line(self, node, max_width=200):
        """For favorites: look up cached info fetched separately, since
        we only have the bare node number until fetch_node_info() runs."""
        if node in self.fetching_nodes and node not in self.node_info_cache:
            return f"{node:<8} loading..."
        info = self.node_info_cache.get(node)
        link_count = info.get("link_count") if info else None
        return self._format_node_summary(node, info, max_width, link_count)

    def draw_nodes(self):
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        rows = self._node_rows()
        # (text, attr, selectable_index or None)
        lines = []
        hint_attr = curses.color_pair(CP_DIM) | curses.A_DIM
        lines.append(("↑/↓ select  Enter connect  x disconnect", hint_attr,
                       None))
        lines.append(("a add  r remove  n/Esc back  q quit", hint_attr,
                       None))
        lines.append(("", 0, None))
        lines.append((f"CONNECTED NOW ({len(self.connected_nodes)})"
                       + ("  refreshing..." if self.connected_refreshing
                          else ""),
                       curses.color_pair(CP_CYAN) | curses.A_BOLD, None))
        line_width = max(20, width - 10)
        idx = 0
        if not self.connected_nodes:
            lines.append(("  (none)", curses.color_pair(CP_DIM) |
                          curses.A_DIM, None))
        for n in self.connected_nodes:
            link_count = self.link_count_cache.get(n["number"])
            text = self._format_node_summary(n["number"], n, line_width,
                                              link_count)
            lines.append((text, None, idx))
            idx += 1
        lines.append(("", 0, None))
        lines.append(("FAVORITES", curses.color_pair(CP_CYAN) |
                      curses.A_BOLD, None))
        if not self.favorites:
            lines.append(("  (none yet -- press 'a' to add a node)",
                          curses.color_pair(CP_DIM) | curses.A_DIM, None))
        for n in self.favorites:
            lines.append((self._node_line(n, line_width), None, idx))
            idx += 1
        if self.nodes_add_mode:
            lines.append((f"  add favorite, node #: {self.nodes_add_buf}_",
                          curses.color_pair(CP_YELLOW) | curses.A_BOLD,
                          None))
        else:
            lines.append(("+ Add favorite", None, idx))
        if self.link_busy or self.link_status:
            msg = self.link_status if not self.link_busy else \
                f"» {self.link_status}"
            lines.append((msg, curses.color_pair(CP_YELLOW) |
                          curses.A_BOLD, None))

        draw_box(stdscr, 0, 0, height, width, title="NODES")
        for i, (text, attr, sel_idx) in enumerate(lines):
            row = 1 + i
            if sel_idx is not None:
                selected = sel_idx == self.nodes_selected
                marker = "▸ " if selected else "  "
                base = (curses.color_pair(CP_CYAN) | curses.A_BOLD
                        if selected else curses.color_pair(CP_TEXT))
                safe_addstr(stdscr, row, 2, f"{marker}{text}", base)
            else:
                safe_addstr(stdscr, row, 2, text, attr or
                            curses.color_pair(CP_TEXT))

    def draw_history(self):
        """Log of who connected to/disconnected from this node and
        when -- logged on the node's own Pi (pi_logger/), fetched over
        SSH, so it covers link changes even while maxstar wasn't
        running to see them directly."""
        self.refresh_link_history()  # kept fresh while this screen is open
        stdscr = self.stdscr
        height, width = stdscr.getmaxyx()
        draw_box(stdscr, 0, 0, height, width, title="LINK HISTORY")
        hint_attr = curses.color_pair(CP_DIM) | curses.A_DIM
        safe_addstr(stdscr, 1, 2,
                    "↑/↓ scroll  r refresh  c clean  h/Esc back  q quit",
                    hint_attr)

        total = len(self.link_history)
        header = f"{total} event{'s' if total != 1 else ''} on the node"
        if self.history_refreshing:
            header += "  refreshing..."
        safe_addstr(stdscr, 2, 2, header,
                    curses.color_pair(CP_CYAN) | curses.A_BOLD)
        if self.history_error:
            safe_addstr(stdscr, 3, 2, f"! {self.history_error}"[:width - 4],
                        curses.color_pair(CP_RED) | curses.A_BOLD)

        if self.history_clean_mode:
            safe_addstr(stdscr, height - 2, 2,
                        f"clean: keep last how many days? "
                        f"{self.history_clean_buf}_",
                        curses.color_pair(CP_YELLOW) | curses.A_BOLD)
        elif self.history_busy or self.link_status:
            msg = self.link_status if not self.history_busy else \
                f"» {self.link_status}"
            safe_addstr(stdscr, height - 2, 2, msg[:width - 4],
                        curses.color_pair(CP_YELLOW) | curses.A_BOLD)

        top_row = 4
        bottom_row = height - 3
        if not self.link_history:
            safe_addstr(stdscr, top_row, 2, "(none yet)",
                        curses.color_pair(CP_DIM) | curses.A_DIM)
            return

        visible = max(0, bottom_row - top_row)
        ordered = list(reversed(self.link_history))  # newest first
        self.history_offset = min(self.history_offset,
                                  max(0, len(ordered) - 1))
        window = ordered[self.history_offset:self.history_offset + visible]
        line_width = max(20, width - 26)
        for i, entry in enumerate(window):
            ts = time.strftime("%m-%d %H:%M:%S",
                               time.localtime(entry.get("ts", 0)))
            number = entry.get("number", "")
            detail = "  ".join(b for b in (entry.get("callsign"),
                                           entry.get("location")) if b)
            if entry.get("event") == "connect":
                marker = "+"
                attr = curses.color_pair(CP_GREEN) | curses.A_BOLD
            else:
                marker = "-"
                attr = curses.color_pair(CP_RED) | curses.A_BOLD
            text = f"{marker} {ts}  {number:<8} {detail}"[:line_width]
            safe_addstr(stdscr, top_row + i, 2, text, attr)
        if len(ordered) > visible:
            paging = (f"{self.history_offset + 1}-"
                      f"{self.history_offset + len(window)} of "
                      f"{len(ordered)}")
            safe_addstr(stdscr, 2, max(2, width - len(paging) - 3),
                        paging, hint_attr)

    def draw_monitor(self):
        stdscr = self.stdscr
        cfg = self.config
        height, width = stdscr.getmaxyx()
        draw_box(stdscr, 0, 0, height, width, title="MAXSTAR")

        status_colors = {"connected": CP_GREEN, "connecting": CP_YELLOW,
                          "failed": CP_RED, "disconnected": CP_DIM}
        status_attr = curses.color_pair(
            status_colors.get(self.status, CP_DIM)) | curses.A_BOLD
        safe_addstr(stdscr, 1, width - len(self.status) - 4,
                    f"[{self.status.upper()}]", status_attr)

        safe_addstr(stdscr, 1, 2,
                    f"{cfg['MAXSTAR_USER']}@{cfg['MAXSTAR_HOST']}:"
                    f"{cfg['MAXSTAR_PORT']}",
                    curses.color_pair(CP_TEXT))

        safe_addstr(stdscr, 3, 2, "NODE", curses.color_pair(CP_DIM) |
                    curses.A_DIM)
        draw_big_number(stdscr, 4, 2, cfg["MAXSTAR_NODE"])

        keyed = bool(self.call and self.call.keyed.is_set())
        rx_instant = self.call.rx_level if self.call else 0
        tx_instant = self.call.tx_level if self.call else 0
        self.rx_display = max(rx_instant, self.rx_display * METER_DECAY)
        self.tx_display = max(tx_instant, self.tx_display * METER_DECAY)

        # Two real, filled badges instead of one small toggling label --
        # RX lights up on actual received audio (not just "not
        # transmitting"), TX on the real keyed state.
        rx_active = self.rx_display > RX_ACTIVE_THRESHOLD
        draw_badge(stdscr, 4, 28, "RX", rx_active, CP_RX_ON)
        draw_badge(stdscr, 4, 36, "TX", keyed, CP_TX_ON)
        if self.keyed_since is not None:
            remaining = max(
                0, TX_TIMEOUT_SECONDS - (time.time() - self.keyed_since))
            mins, secs = divmod(int(remaining), 60)
            countdown_attr = curses.color_pair(CP_RED) | curses.A_BOLD \
                if remaining <= 15 else curses.color_pair(CP_YELLOW)
            safe_addstr(stdscr, 5, 44, f"{mins}:{secs:02d}", countdown_attr)
        self.rx_peak = max(rx_instant, self.rx_peak * PEAK_DECAY)
        self.tx_peak = max(tx_instant, self.tx_peak * PEAK_DECAY)

        meter_width = max(20, min(width - 24, METER_MAX_WIDTH))
        draw_meter(stdscr, 10, 2, meter_width, self.rx_display,
                   self.rx_peak, "RX")
        draw_meter(stdscr, 13, 2, meter_width, self.tx_display,
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

        self.draw_connected_panel(18, height - 3, width)

        safe_addstr(stdscr, height - 2, 2,
                    "space ptt  l link  d disc  ↑/↓+x disconnect "
                    "selected  h history  n nodes  c cfg  q quit",
                    curses.color_pair(CP_DIM) | curses.A_DIM)

    def draw_connected_panel(self, top, bottom, width):
        """Always-visible list of nodes currently linked to ours, at the
        bottom of the main dashboard. Up/Down selects, 'x' disconnects
        the selected one -- so you don't have to switch screens or type
        a node number just to drop a link you can already see."""
        stdscr = self.stdscr
        if bottom <= top:
            return
        count = len(self.connected_nodes)
        if count:
            self.monitor_selected %= count
        # Multiple simultaneous links is the less-common, more-notable
        # state (a multi-way net rather than a single link) -- flag it
        # with a warmer color instead of the same green as "just one".
        count_attr = (curses.color_pair(CP_YELLOW) | curses.A_BOLD if count > 1
                      else curses.color_pair(CP_CYAN) | curses.A_BOLD)
        header = f"── CONNECTED NODES ({count})"
        if self.connected_refreshing:
            header += "  refreshing..."
        header += " " + "─" * max(0, width - len(header) - 4)
        safe_addstr(stdscr, top, 2, header, count_attr)

        row = top + 1
        if not self.connected_nodes:
            safe_addstr(stdscr, row, 2, "  (none)",
                        curses.color_pair(CP_DIM) | curses.A_DIM)
            return
        line_width = max(20, width - 14)
        shown = self.connected_nodes[:max(0, bottom - row)]
        for i, n in enumerate(shown):
            selected = i == self.monitor_selected
            if count > 1:
                marker = f"{'▸' if selected else ' '} {i + 1}."
            else:
                marker = "  ▸"
            link_count = self.link_count_cache.get(n["number"])
            text = self._format_node_summary(n["number"], n, line_width,
                                              link_count)
            row_attr = (curses.color_pair(CP_CYAN) | curses.A_BOLD if selected
                        else curses.color_pair(CP_TEXT))
            safe_addstr(stdscr, row + i, 2, f"{marker} {text}", row_attr)
        hidden = count - len(shown)
        if hidden > 0 and row + len(shown) <= bottom:
            safe_addstr(stdscr, row + len(shown), 2, f"  ... +{hidden} more",
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
