# maxstar

A minimal, hand-rolled IAX2 client (`iax_client.py`) for connecting to an
AllStarLink node from a Mac without any radio hardware or third-party
softphone. Built because Zoiper Classic is discontinued, Zoiper 5 gates
setup behind a cloud-account wizard with no Thailand provider, and
droidstar-enhanced's PTT doesn't speak app_rpt's actual keying protocol.

Named after `maxwell` (the Pi hosting the node) + AllStar. A UI is
planned, and a radio module may be attached to the Pi later; this client
is meant to keep working as the control surface either way.

See `../docs/allstarlink-listen-without-radio.md` for the full story
(dead ends tried, node-side config, why keying works the way it does).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install sounddevice numpy
brew install portaudio   # if not already installed
```

## Usage

```bash
.venv/bin/python3 iax_client.py
```

Defaults connect to `192.168.1.156:4569` as user `macbook` → node `42865`.
Override with `--host`, `--port`, `--user`, `--secret`, `--node`,
`--context` as needed.

Interactive commands once connected:
- `k` — key up, start sending mic audio
- `u` — unkey, stop sending
- `q` — hang up and quit

## Diagnostic flags (no mic/speakers needed)

- `--selftest` — inject a synthetic 1kHz tone, then report what echoes
  back (frame count, bytes, peak amplitude)
- `--send-tone N` — connect, key up, send N seconds of test tone, hang up
- `--listen N` — connect and just report received audio for N seconds
- `--verbose` — print the raw `[TX]`/`[RX]` IAX2 frame trace (noisy)

These were built to debug the keying protocol without needing a human to
listen — e.g. `--listen` on one account while `--send-tone` runs on
another proves whether audio crosses between two connections.

## Protocol notes (for future changes)

All constants were pulled from Asterisk's own source rather than assumed,
since a wrong handshake/codec value fails silently:
- Frame header layout: `channels/iax2/include/iax2.h` —
  `struct ast_iax2_full_hdr` / `ast_iax2_mini_hdr`
- `IAX_COMMAND_*` / `IAX_IE_*` values: same file
- `AST_CONTROL_*` / `AST_FRAME_*` values: `include/asterisk/frame.h`
- Codec bitmask (ulaw = `1<<2`): `include/asterisk/format_compatibility.h`
- MD5 auth order (`md5(challenge + secret)`, not the reverse): confirmed
  in `channels/chan_iax2.c`
- Subclass "compression" (power-of-two encoding for large codec bitmasks,
  flagged by the `0x80` high bit): `compress_subclass()` /
  `uncompress_subclass()` in `channels/chan_iax2.c`. Not relevant for
  ulaw specifically (value 4, well under the 128 compression threshold).

**Keying is voice-frame-based, not DTMF** for this connection type ("X" /
normal-endpoint mode). On connect, app_rpt sends a `!NEWKEY1!` text frame;
the client must echo it back once, or incoming voice frames are ignored
until a 2-second (`NEWKEYTIME`) fallback timer expires. Source:
`AllStarLink/app_rpt`, `apps/app_rpt/rpt_channel.h` and `rpt_channel.c`
(`send_newkey`, `NEWKEY1STR`, `NEWKEYTIME`).

This connection type is also **single-seat** — dialing your own node
number as the exact extension takes over the node's `rxchannel` (radio
substitute). A second simultaneous connection gets the first one hung up.
Confirmed by connecting two test accounts (`macbook`/`macbook2`)
simultaneously and watching the server hang up the first the instant the
second authenticated. Hearing another real participant requires a genuine
node-to-node link (a different mechanism), not a second client
connection to the same extension.

## Known rough edges

- `audioop` is deprecated (removed in Python 3.13) — fine on the 3.12
  currently installed, will need replacing (manual µ-law codec) if
  upgrading Python later.
- No retransmission/reliability logic for full frames beyond basic
  ACK/seqno tracking — fine on a local LAN, would need work for use over
  the public internet with real packet loss.
- `scallno` is hardcoded to `1` — fine for one connection at a time; would
  need to be randomized/configurable to run multiple instances from the
  same process.
