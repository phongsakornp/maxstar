#!/usr/bin/env python3
"""
Thin client for AllStarLink's public stats API -- used to show which
nodes are currently connected to ours, and to look up a node's
callsign/location/sitename/affiliation for the favorites list.

No new dependency: urllib is stdlib.
"""

import json
import urllib.request

STATS_URL = "https://stats.allstarlink.org/api/stats/{node}"


def fetch_node_stats(node, timeout=5):
    """Raw API response for a node, or None on any failure (network,
    timeout, bad JSON, node not found) -- callers should treat None as
    'unknown right now' rather than an error to surface loudly."""
    url = STATS_URL.format(node=node)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def summarize_node(node_obj):
    """Pull the fields worth displaying out of one raw node object."""
    if not node_obj:
        return None
    server = node_obj.get("server") or {}
    return {
        "number": str(node_obj.get("name") or ""),
        "node_id": node_obj.get("Node_ID"),
        "callsign": node_obj.get("callsign") or "",
        "status": node_obj.get("Status") or "",
        "location": server.get("Location") or "",
        "sitename": server.get("SiteName") or "",
        "affiliation": server.get("Affiliation") or "",
    }


def fetch_node_summary(node, timeout=5):
    """A single node's own registry info (callsign/location/etc), plus
    how many nodes it's currently linked to (link_count) -- same API
    call already returns both, no extra request needed. link_count is
    None (not 0) when the "stats" section itself is missing -- observed
    to happen even for an actively-linked node, so it must not be read
    as "confirmed zero links"."""
    raw = fetch_node_stats(node, timeout)
    if not raw:
        return None
    summary = summarize_node(raw.get("node"))
    if summary is None:
        return None
    stats = raw.get("stats")
    if stats is None:
        summary["link_count"] = None
    else:
        data = stats.get("data") or {}
        summary["link_count"] = len(data.get("links") or [])
    return summary


def fetch_link_count(node, timeout=5):
    """How many nodes `node` is currently linked to -- a lightweight
    lookup for nodes we only have registry info for (e.g. entries in
    our own linkedNodes list), where we haven't already fetched their
    own stats separately. None if that isn't knowable right now (fetch
    failed, or "stats" is missing from an otherwise-valid response --
    seen even for an actively-linked node) -- callers must not treat
    that the same as a confirmed 0."""
    raw = fetch_node_stats(node, timeout)
    if not raw or raw.get("stats") is None:
        return None
    data = raw["stats"].get("data") or {}
    return len(data.get("links") or [])


def fetch_connected_nodes(node, timeout=5):
    """Nodes currently linked to `node`, per its own live stats, or None
    if that isn't knowable right now (fetch failed, or the response has
    no "stats" section). The public API has been observed to return a
    null "stats" section for a node that is, per Asterisk itself (e.g.
    allmon), still actively linked -- so that case must NOT be read as
    "zero links", or a client relying solely on this (nothing else to
    fall back on right after a restart, for instance) would wrongly
    flash an active link to empty. Callers should keep their last-known
    state on None rather than overwrite it."""
    raw = fetch_node_stats(node, timeout)
    if not raw or raw.get("stats") is None:
        return None
    data = raw["stats"].get("data") or {}
    linked = data.get("linkedNodes") or []
    return [s for s in (summarize_node(n) for n in linked) if s]
