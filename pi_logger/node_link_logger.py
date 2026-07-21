#!/usr/bin/env python3
"""
Standalone link-history logger meant to run on the node's own Pi, not
the Mac client -- so history keeps recording connect/disconnect events
even when maxstar itself isn't running. See ../README.md for how to
deploy this under a user-level systemd service.

Polls stats.allstarlink.org for this node's own directly-linked nodes
(the API only ever reports our own node's own "linkedNodes" -- never
nodes-of-nodes, so a multi-hop node linked through one of our direct
links is never logged here) and appends one JSON line per connect/
disconnect to a log file.

Deliberately self-contained (stdlib only, no import of the sibling
allstarlink_stats.py) so only this one file needs to be deployed to
the Pi.
"""

import argparse
import json
import os
import time
import urllib.request

STATS_URL = "https://stats.allstarlink.org/api/stats/{node}"
POLL_SECONDS = 15  # matches maxstar's own CONNECTED_REFRESH_SECONDS


def fetch_linked_nodes(node, timeout=5):
    """None on any fetch failure (network hiccup, timeout, bad JSON) --
    callers must treat that as "unknown this cycle", not "zero links",
    or a transient blip would log a false mass-disconnect followed by a
    false mass-reconnect. Otherwise a dict of number -> summary for
    whatever's currently linked directly to `node`."""
    url = STATS_URL.format(node=node)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if raw.get("stats") is None:
        # A valid response with no "stats" section -- observed even for
        # a node that's actively linked -- must be treated the same as
        # a fetch failure (see watch()'s seen/current diff below), not
        # as "confirmed zero links", or a transient blip logs a false
        # mass-disconnect followed by a false mass-reconnect.
        return None
    data = raw["stats"].get("data") or {}
    linked = data.get("linkedNodes") or []
    result = {}
    for n in linked:
        number = str(n.get("name") or "")
        if not number:
            continue
        server = n.get("server") or {}
        result[number] = {
            "callsign": n.get("callsign") or "",
            "location": server.get("Location") or "",
        }
    return result


def append_entry(path, entry):
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_entries(path):
    if not os.path.exists(path):
        return []
    entries = []
    with open(path) as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def clean(path, keep_days):
    """Prune entries older than keep_days, rewriting the file. Returns
    the number of entries kept."""
    cutoff = time.time() - keep_days * 86400
    kept = [e for e in load_entries(path) if e.get("ts", 0) >= cutoff]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for e in kept:
            f.write(json.dumps(e) + "\n")
    os.replace(tmp, path)
    return len(kept)


def watch(node, path, interval):
    if not os.path.exists(path):
        # Create it empty up front -- otherwise a viewer (e.g. maxstar's
        # history screen) tailing this file over SSH sees a confusing
        # "no such file" error for as long as no link event has fired
        # yet, rather than a clean "0 events".
        open(path, "a").close()
    seen = None  # None until the first successful fetch -- that first
                 # snapshot is a baseline (nodes already linked before
                 # this logger started watching), not logged as a wave
                 # of "connects".
    while True:
        current = fetch_linked_nodes(node)
        if current is not None:
            if seen is not None:
                now = time.time()
                for number, info in current.items():
                    if number not in seen:
                        append_entry(path, dict(
                            ts=now, event="connect", number=number,
                            callsign=info["callsign"],
                            location=info["location"]))
                for number, info in seen.items():
                    if number not in current:
                        append_entry(path, dict(
                            ts=now, event="disconnect", number=number,
                            callsign=info["callsign"],
                            location=info["location"]))
            seen = current
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", help="Node number to watch (required "
                        "unless --clean)")
    parser.add_argument("--log", default="link_history.jsonl",
                        help="Path to the JSONL log file")
    parser.add_argument("--interval", type=int, default=POLL_SECONDS,
                        help="Poll interval in seconds")
    parser.add_argument("--clean", action="store_true",
                        help="Prune entries older than --keep-days and "
                             "exit -- does not start the watch loop")
    parser.add_argument("--keep-days", type=int, default=30,
                        help="With --clean, drop entries older than this "
                             "many days")
    args = parser.parse_args()

    if args.clean:
        kept = clean(args.log, args.keep_days)
        print(f"kept {kept} entries from the last {args.keep_days} days")
        return

    if not args.node:
        parser.error("--node is required unless --clean")
    watch(args.node, args.log, args.interval)


if __name__ == "__main__":
    main()
