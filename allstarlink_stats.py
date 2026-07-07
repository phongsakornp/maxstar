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
    """A single node's own registry info (callsign/location/etc)."""
    raw = fetch_node_stats(node, timeout)
    if not raw:
        return None
    return summarize_node(raw.get("node"))


def fetch_connected_nodes(node, timeout=5):
    """Nodes currently linked to `node`, per its own live stats."""
    raw = fetch_node_stats(node, timeout)
    if not raw:
        return []
    data = ((raw.get("stats") or {}).get("data") or {})
    linked = data.get("linkedNodes") or []
    return [s for s in (summarize_node(n) for n in linked) if s]
