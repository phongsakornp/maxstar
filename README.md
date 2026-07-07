# maxstar

A minimal, hand-rolled IAX2 client (`iax_client.py`) for connecting to an
AllStarLink node from a Mac without any radio hardware or third-party
softphone. Built because Zoiper Classic is discontinued, Zoiper 5 gates
setup behind a cloud-account wizard with no Thailand provider, and
droidstar-enhanced's PTT doesn't speak app_rpt's actual keying protocol.

Named after the Pi hosting the node + AllStar. A UI is planned, and a
radio module may be attached to the Pi later; this client is meant to
keep working as the control surface either way.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install sounddevice numpy
brew install portaudio   # if not already installed
```

## Node-side configuration

ASL3 ships a ready-made template for this in `/etc/asterisk/iax.conf`:

```
[iaxclient](!)                   ; Connect from iax client (Zoiper, DVSwitch Mobile, etc.)
type = friend
context = iax-client
auth = md5
host = dynamic
disallow = all
allow = ulaw
allow = adpcm
allow = gsm
transfer = no
#tryinclude custom/iax/iaxclient-users.conf
```

The account lives in `/etc/asterisk/custom/iax/iaxclient-users.conf`:
```
[<YOUR_USERNAME>](iaxclient)
secret = <set on the Pi — never commit this>
requirecalltoken = no
callerid = "LISTENER"
```
(`requirecalltoken = no` because most IAX clients — this one included —
don't implement the call-token challenge that `type=friend` peers
normally require.)

The dial context in `/etc/asterisk/extensions.conf`:
```
[iax-client]
exten => <YOUR_NODE>,1,rpt(<YOUR_NODE>,X)
```
Dialing your own node number (`<YOUR_NODE>`) connects you as the node's
radio input — see "Single-seat connections" below.

## Usage

```bash
cp .env.example .env
# edit .env: set MAXSTAR_HOST, MAXSTAR_USER, MAXSTAR_SECRET, MAXSTAR_NODE
.venv/bin/python3 iax_client.py
```

`.env` is gitignored and read automatically on startup (falls back to
real env vars of the same name if no `.env` is present). `--host`,
`--port`, `--user`, `--secret`, `--node`, `--context` all override
their `MAXSTAR_*` env var if you'd rather pass them inline. `--host`,
`--user`, `--secret`, and `--node` are required one way or
another — the client refuses to guess at your node's identity. Never
hardcode any of these as defaults in source — they get picked up by
git otherwise.

Interactive commands once connected:
- `k` — key up, start sending mic audio
- `u` — unkey, stop sending
- `q` — hang up and quit

## TUI

```bash
.venv/bin/python3 tui.py
```

Curses-based UI (stdlib `curses`, no new dependency), styled like a
rig's control panel (Icom IC-705-ish): bordered panel, a big block-digit
VFO-style readout for the node number, a PTT lamp (`○ rx` / `● TX`),
and segmented green/yellow/red level meters with a peak-hold marker and
a dB scale row. The scale reads dBFS (audio level relative to full
scale), not S-units — IAX2 carries digitized audio, not an RF signal
report, so there's no real RSSI to show; it borrows the meter's visual
language while staying honest about what's actually being measured.

If any required `.env` value is missing it opens straight into a config
screen instead of connecting blind:
- **Config screen**: Up/Down to select a field, Enter to edit,
  `s` to save + (re)connect, Esc to go back once connected, `q` to quit.
  Edits write straight back to `.env`.
- **Monitor screen**: `k`/`u` to key/unkey, `c` to reopen the config
  screen, `q` to quit.

`iax_client.py`'s own `print()` diagnostics are silenced while the TUI
is running (they'd otherwise corrupt curses' control of the screen) —
use the plain CLI with `--verbose` if you need the raw frame trace.

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

### Single-seat connections

This connection type is also **single-seat** — dialing your own node
number as the exact extension takes over the node's `rxchannel` (radio
substitute). A second simultaneous connection gets the first one hung up.
Confirmed by connecting two test accounts simultaneously and watching the
server hang up the first the instant the second authenticated. Hearing
another real participant requires a genuine node-to-node link (a
different mechanism), not a second client connection to the same
extension. This is also why AllStarLink's built-in Parrot Mode (echo
test) never plays anything back here — parrot's playback goes to other
links, not back to the one occupying the radio seat.

## Confirmed working

The node was linked to another real, busy node, and its live traffic
was heard through this client — connecting to and hearing another
node, entirely without radio hardware.

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
