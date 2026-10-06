# ss-to-ledfx

A bridge that lets **SoundSwitch** control individually-addressable LED strips
driven by **LedFx**.

SoundSwitch animates a stand-in fixture (Chauvet **Freedom Flex Stick, 15CH mode**)
and outputs it over **Art-Net**. This bridge receives that DMX, reads the 15
fixture channels, and translates them into LedFx REST API calls.

```
SoundSwitch ──Art-Net (UDP 6454)──▶ ss-to-ledfx ──HTTP REST──▶ LedFx ──▶ LED strips
   (Freedom Flex, 15CH)              (reads channels,          (scenes, brightness)
                                      maps to API calls)
```

## Phase 1 (implemented)

| Channel | Fixture function | Bridge action |
|---------|------------------|---------------|
| 1 | Dimmer | LedFx global brightness (`PUT /api/config` `global_brightness`), rate-limited |
| 8 | Auto program | Activates the Nth configured scene (`PUT /api/scenes` activate), debounced |

- **Scenes:** auto-program N (channel 8) activates `scenes[N-1]` from your config.
  Program chart ranges are 8 DMX values wide from value 11 (prog 1 = 11–18, …,
  prog 25 = 203–255). Values ≤ 10 = "no function" → hold the current scene (or
  switch to an optional idle scene).
- **Brightness:** channel 1 (0–255) scales to `global_brightness` (0.0–1.0).
  Value 0 is a true blackout in LedFx.
- **Debounce / rate-limit:** scenes only fire after channel 8 holds steady
  (~100 ms) so crossfades don't flicker through wrong scenes; brightness is
  capped at ~25 updates/sec.

### Per-program RGBW colour override

Each program → scene mapping has an **RGBW override** checkbox. When ticked, the
SoundSwitch colour channels drive the effect colour while that scene is running:

| Channel | Function | Bridge action |
|---------|----------|---------------|
| 2 | Red | combined into the effect `color` |
| 3 | Green | " |
| 4 | Blue | " |
| 5 | White | folded additively into R, G, B |

- Only effects that expose a single `color` setting (e.g. **Single Color**)
  follow the override; the bridge discovers which virtuals qualify from
  `GET /api/virtuals` and pushes `PUT /api/virtuals/{id}/effects`
  `{"config":{"color":"#rrggbb"}}` to each.
- If R, G, B and W are all 0, the scene's saved colours are left untouched.
- The colour is re-sent right after a scene activates (the scene reloads its
  saved colours), and updates are rate-limited (default ~25/sec).
- Leave the box unticked to let the scene keep full control of its colours.

Strobe (ch 7), auto-program speed (ch 9) and dimmer-smoothing (ch 14) remain
phase-2 TODOs.

## Requirements

- Python 3.11+
- LedFx running and reachable (default `http://localhost:8888`)
- SoundSwitch set to output **Art-Net**, with the Freedom Flex fixture patched
  to a known universe + start address

## Install & run

```bash
# from the project root
python3 -m venv .venv
.venv/bin/pip install -e .          # or: .venv/bin/pip install aiohttp

# start the bridge
.venv/bin/python -m ss_to_ledfx          # normal
.venv/bin/python -m ss_to_ledfx -v       # with debug logging
.venv/bin/python -m ss_to_ledfx -c /path/to/config.json
```

Then open the web UI at **http://localhost:8890** to:
- see Art-Net / LedFx status and live channel values,
- map each auto-program to a LedFx scene (dropdowns populated from LedFx),
- edit universe, start address, LedFx URL, debounce and rate limits.

Settings are saved to `config.json` next to the package and survive restarts.

## Configuration (`config.json`)

```json
{
  "artnet":   { "bind_host": "0.0.0.0", "bind_port": 6454, "universe": 0 },
  "ledfx":    { "base_url": "http://localhost:8888" },
  "web":      { "host": "0.0.0.0", "port": 8890 },
  "start_address": 1,
  "scenes": ["living-room", "slow", "party-time-1"],
  "no_function_scene": null,
  "scene_debounce_ms": 100,
  "brightness_max_rate_hz": 25.0,
  "control_scenes": true,
  "control_brightness": true
}
```

- `artnet.universe` is the 15-bit Art-Net port address (Net << 8 | SubNet << 4 |
  Universe). Must match what SoundSwitch is patched to.
- `start_address` is the 1-based DMX address of fixture channel 1.
- `scenes[0]` = auto-program 1. Use the LedFx scene **IDs** (the dropdowns in the
  UI show them). Unknown IDs are logged as warnings at startup.

## Testing without SoundSwitch

A helper sends ArtDmx packets so you can exercise the bridge locally:

```bash
# set dimmer (ch1) = 200 and auto-program (ch8) = 27 -> program 3
.venv/bin/python tools/send_test_dmx.py --universe 0 --set 1=200 --set 8=27
```

## Being discovered by SoundSwitch

Art-Net has no mDNS/Bonjour; discovery uses Art-Net's own **ArtPoll /
ArtPollReply**. The bridge:

- answers every **ArtPoll** with an **ArtPollReply** that advertises one Art-Net
  **output** port for your configured universe, and
- broadcasts an unsolicited ArtPollReply at startup.

So a controller that auto-discovers nodes will list this bridge (short name
`ss-to-ledfx`) and offer it as an output for the universe.

Two ways to point SoundSwitch at the bridge:

1. **Auto-discovery** — enable Art-Net output in SoundSwitch and pick the
   discovered `ss-to-ledfx` node.
2. **Manual** — set SoundSwitch's Art-Net output to this Mac's IP (or the subnet
   broadcast) on UDP **6454** and the matching **universe**. The bridge listens
   on `0.0.0.0:6454` and accepts broadcast, so this needs no node to be found.

For discovery to work both machines must be on the same subnet, and the port
(6454) must be open in the macOS firewall. Confirm it worked on the bridge's
**Status** card: "Art-Net receiving" turns on once SoundSwitch streams, and the
log shows `ArtPoll from <ip> - replying`.

## SoundSwitch side

Create one custom attribute cue per LedFx scene, each setting **channel 8** to a
value in the middle of that program's range (`11 + 8·(N−1) + 3`, e.g. 14, 22, 30…).
Dropping that cue on the timeline switches LedFx to the mapped scene. Channel 1
(dimmer) drives master brightness.
```
