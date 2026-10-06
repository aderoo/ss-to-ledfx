"""Core bridge logic: turn Freedom Flex (15CH) DMX into LedFx API calls.

Phase 1:
  - Channel 1 (Dimmer)       -> LedFx global_brightness   (rate limited)
  - Channel 8 (Auto program) -> activate the Nth configured scene (debounced)

The UDP receiver only stores the latest channel values (never blocks on HTTP).
Two independent worker loops read that state: one debounces the scene, one
rate-limits brightness, so a slow scene activation never stalls brightness.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import FIXTURE_CHANNELS, Config
from .ledfx_client import LedFxClient, LedFxError

_LOGGER = logging.getLogger(__name__)

CH_DIMMER = 1  # 1-based fixture channel numbers
CH_RED = 2
CH_GREEN = 3
CH_BLUE = 4
CH_WHITE = 5
CH_PROGRAM = 8

MAX_PROGRAM = 25


def program_from_value(value: int) -> int | None:
    """Freedom Flex program chart: ranges are 8 wide from value 11.

    Returns the 1-based program number, or None for "no function" (<= 10).
    """
    if value <= 10:
        return None
    return min(MAX_PROGRAM, (value - 11) // 8 + 1)


def rgbw_to_hex(r: int, g: int, b: int, w: int) -> str:
    """Fold the white channel into RGB (additive) and return a #rrggbb string."""
    return "#{:02x}{:02x}{:02x}".format(
        min(255, r + w), min(255, g + w), min(255, b + w)
    )


class Bridge:
    def __init__(self, config: Config, ledfx: LedFxClient):
        self._config = config
        self._ledfx = ledfx

        # Latest fixture channels (1-based index 1..15), written by the UDP
        # callback, read by the workers. Index 0 is unused/padding.
        self._channels: list[int] = [0] * (FIXTURE_CHANNELS + 1)

        self._tasks: list[asyncio.Task] = []
        self._running = False

        # Scene state.
        self._applied_program: int | None = None
        self._applied_scene: str | None = None
        self._candidate_program: int | None = None
        self._candidate_since: float = 0.0

        # Brightness state.
        self._applied_brightness: float | None = None
        self._last_brightness_send: float = 0.0

        # Colour-override state.
        self._applied_color: str | None = None
        # Virtuals whose active effect can take a colour, with the effect type,
        # base config, and which config key carries colour:
        #   {virtual_id: {"type": str, "config": dict, "key": "color"|"gradient"}}
        # `color` is a solid-colour setting (e.g. Single Color); `gradient` is
        # a palette (e.g. Fire) that also accepts a solid hex to recolour it.
        self._color_targets: dict[str, dict] = {}
        # Set after a scene activates: its saved colours are reloaded, so the
        # override colour must be pushed again even if it hasn't changed.
        self._force_color_resend = False

        # White-out state (global override): activates white_out_program's scene
        # while ch1-5 are all at full, then resumes normal program selection.
        self._whiteout_active = False
        self._whiteout_last_state: str | None = None

        # Known scene IDs fetched from LedFx, for validation / warnings.
        self._known_scenes: set[str] = set()

    # --- data in from Art-Net -------------------------------------------

    def update_dmx(self, _port_address: int, dmx: bytes) -> None:
        """Store the fixture's 15 channels from a DMX buffer. Non-blocking."""
        start = self._config.start_address - 1  # 0-based into the DMX buffer
        for ch in range(1, FIXTURE_CHANNELS + 1):
            idx = start + (ch - 1)
            self._channels[ch] = dmx[idx] if 0 <= idx < len(dmx) else 0

    @property
    def channels(self) -> list[int]:
        """Channels 1..15 (list of 15 ints) for the web UI."""
        return self._channels[1:]

    # --- startup ---------------------------------------------------------

    def reset_scene_state(self) -> None:
        """Forget the applied program so a config change re-applies the held one."""
        self._applied_program = None
        self._candidate_program = None

    async def refresh_known_scenes(self) -> None:
        try:
            scenes = await self._ledfx.get_scenes()
        except LedFxError as err:
            _LOGGER.warning("Could not fetch scenes from LedFx: %s", err)
            return
        self._known_scenes = set(scenes.keys())
        for i, scene_id in enumerate(self._config.scenes, start=1):
            if scene_id and scene_id not in self._known_scenes:
                _LOGGER.warning(
                    "Program %d -> scene '%s' does not exist in LedFx", i, scene_id
                )

    async def refresh_color_targets(self) -> None:
        """Cache virtuals whose active effect can take a colour (`color` or
        `gradient`), with which key to set."""
        try:
            virtuals = await self._ledfx.get_virtuals()
        except LedFxError as err:
            _LOGGER.debug("Could not fetch virtuals: %s", err)
            return
        targets: dict[str, dict] = {}
        for vid, info in virtuals.items():
            effect = info.get("effect") or {}
            config = effect.get("config") or {}
            effect_type = effect.get("type")
            if not effect_type:
                continue
            # Prefer a solid `color`; fall back to recolouring a `gradient`.
            key = (
                "color"
                if "color" in config
                else ("gradient" if "gradient" in config else None)
            )
            if key is not None:
                targets[vid] = {"type": effect_type, "config": dict(config), "key": key}
                # Force the virtual's transition so colour changes snap with the
                # DMX (a colour change restarts the effect, which otherwise
                # crossfades over the virtual's transition_time).
                want = info.get("config", {}).get("transition_time")
                if want != self._config.color_transition_time:
                    try:
                        await self._ledfx.set_virtual_config(
                            vid,
                            {"transition_time": self._config.color_transition_time},
                        )
                    except LedFxError as err:
                        _LOGGER.debug("Set transition on %s failed: %s", vid, err)
        self._color_targets = targets

    def start(self) -> None:
        self._running = True
        if self._config.white_out:
            scene = self._scene_for_program(self._config.white_out_program)
            _LOGGER.info(
                "White-out ENABLED: program %d -> scene %r, threshold %d "
                "(triggers when ch1-5 all >= threshold)",
                self._config.white_out_program,
                scene,
                self._config.white_out_threshold,
            )
        else:
            _LOGGER.info("White-out disabled")
        self._tasks = [
            asyncio.create_task(self._scene_worker(), name="scene_worker"),
            asyncio.create_task(self._brightness_worker(), name="brightness_worker"),
            asyncio.create_task(self._color_worker(), name="color_worker"),
            asyncio.create_task(self._whiteout_worker(), name="whiteout_worker"),
        ]

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []

    # --- mapping ---------------------------------------------------------

    def _scene_for_program(self, program: int | None) -> str | None:
        """Resolve the scene ID a program should activate (or None to hold)."""
        if program is None:
            # "No function": hold current scene, unless an idle scene is set.
            return self._config.no_function_scene or None
        idx = program - 1
        if 0 <= idx < len(self._config.scenes):
            scene_id = self._config.scenes[idx]
            return scene_id or None
        return None

    async def _scene_worker(self) -> None:
        """Debounce channel 8 and activate the mapped scene on a stable change."""
        tick = 0.01  # 10 ms
        while self._running:
            await asyncio.sleep(tick)
            if not self._config.control_scenes or self._whiteout_active:
                continue

            program = program_from_value(self._channels[CH_PROGRAM])
            if program == self._applied_program:
                self._candidate_program = program
                continue

            now = time.monotonic()
            if program != self._candidate_program:
                # New candidate; (re)start the debounce timer.
                self._candidate_program = program
                self._candidate_since = now
                continue

            held_ms = (now - self._candidate_since) * 1000.0
            if held_ms < self._config.scene_debounce_ms:
                continue

            # Candidate has held steady long enough: apply it.
            scene_id = self._scene_for_program(program)
            self._applied_program = program
            if scene_id is None:
                # No scene mapped (or "no function" with no idle scene): hold.
                continue
            try:
                await self._ledfx.activate_scene(scene_id)
                self._applied_scene = scene_id
                _LOGGER.info("Program %s -> activated scene '%s'", program, scene_id)
                # The scene reloaded its saved effects/colours: refresh which
                # virtuals are colour-capable and force a colour re-push.
                await self.refresh_color_targets()
                self._force_color_resend = True
            except LedFxError as err:
                _LOGGER.warning("Failed to activate scene '%s': %s", scene_id, err)
                # Allow a retry on the next change.
                self._applied_program = None

    def _color_override_active(self) -> bool:
        """Whether the current program allows RGBW colour override."""
        program = self._applied_program
        if program is None:
            return False
        idx = program - 1
        return 0 <= idx < len(self._config.color_override) and bool(
            self._config.color_override[idx]
        )

    async def _brightness_worker(self) -> None:
        """Scale channel 1 to global_brightness, rate limited, skip no-ops."""
        while self._running:
            rate = max(1.0, float(self._config.brightness_max_rate_hz))
            min_interval = 1.0 / rate
            await asyncio.sleep(min_interval)
            if not self._config.control_brightness:
                continue

            target = round(self._channels[CH_DIMMER] / 255.0, 4)
            if target == self._applied_brightness:
                continue

            try:
                await self._ledfx.set_global_brightness(target)
                self._applied_brightness = target
            except LedFxError as err:
                _LOGGER.debug("Brightness update failed: %s", err)

    async def _color_worker(self) -> None:
        """Push RGBW (ch 2-5) to colour-capable effects while override is on."""
        while self._running:
            rate = max(1.0, float(self._config.color_max_rate_hz))
            await asyncio.sleep(1.0 / rate)
            if (
                not self._config.control_color
                or not self._color_override_active()
                or self._whiteout_active
            ):
                # Reset so re-enabling (or a new scene) re-sends the colour.
                self._applied_color = None
                continue

            r = self._channels[CH_RED]
            g = self._channels[CH_GREEN]
            b = self._channels[CH_BLUE]
            w = self._channels[CH_WHITE]
            if r == g == b == w == 0:
                # All-zero: leave the scene's saved colours untouched.
                continue

            target = rgbw_to_hex(r, g, b, w)
            if target == self._applied_color and not self._force_color_resend:
                continue

            self._force_color_resend = False
            if not self._color_targets:
                await self.refresh_color_targets()
            for vid, meta in self._color_targets.items():
                merged = {**meta["config"], meta["key"]: target}
                try:
                    await self._ledfx.set_effect(vid, meta["type"], merged)
                except LedFxError as err:
                    _LOGGER.debug("Colour update on %s failed: %s", vid, err)
            self._applied_color = target

    def _whiteout_state(self, full: bool) -> str:
        """Human-readable white-out mode state, for logging and the UI."""
        if not self._config.white_out:
            return "disabled"
        prog = self._config.white_out_program
        if self._whiteout_active:
            return f"ACTIVE -> program {prog}"
        scene = self._scene_for_program(prog)
        if scene is None:
            return f"armed, but program {prog} has no scene configured"
        if full:
            return "condition met (triggering)"
        return (
            f"armed (program {prog} = {scene!r}, fires when ch1-5 all >= "
            f"{self._config.white_out_threshold})"
        )

    async def _whiteout_worker(self) -> None:
        """Jump to the white-out program's scene while ch1-5 are all at full."""
        last_log = 0.0
        while self._running:
            await asyncio.sleep(0.02)  # 50 Hz
            # Guard: never let an unexpected error kill the worker.
            try:
                enabled = self._config.white_out
                full = self._whiteout_condition() if enabled else False
                channels = [self._channels[c] for c in (1, 2, 3, 4, 5)]

                # Read out the white-out mode state whenever it changes (INFO,
                # so it shows without -v).
                state = self._whiteout_state(full)
                if state != self._whiteout_last_state:
                    self._whiteout_last_state = state
                    _LOGGER.info(
                        "White-out mode: %s | ch1-5=%s threshold=%s",
                        state,
                        channels,
                        self._config.white_out_threshold,
                    )

                # Continuous ground-truth values once a second (DEBUG, -v).
                now = time.monotonic()
                if enabled and now - last_log >= 1.0:
                    last_log = now
                    _LOGGER.debug(
                        "white-out watch: ch1-5=%s threshold=%s condition=%s active=%s",
                        channels,
                        self._config.white_out_threshold,
                        full,
                        self._whiteout_active,
                    )

                if not enabled:
                    if self._whiteout_active:
                        await self._exit_whiteout()
                    continue

                if full and not self._whiteout_active:
                    await self._enter_whiteout()
                elif not full and self._whiteout_active:
                    await self._exit_whiteout()
            except Exception as err:  # noqa: BLE001 - keep the worker alive
                _LOGGER.error("white-out worker error (continuing): %s", err)

    def _whiteout_condition(self) -> bool:
        """True when ch1 (dimmer) and ch2-5 (RGBW) are all at/above the level."""
        thr = self._config.white_out_threshold
        return all(
            self._channels[c] >= thr
            for c in (CH_DIMMER, CH_RED, CH_GREEN, CH_BLUE, CH_WHITE)
        )

    async def _enter_whiteout(self) -> None:
        self._whiteout_active = True
        scene_id = self._scene_for_program(self._config.white_out_program)
        if scene_id is None:
            _LOGGER.warning(
                "White-out program %d has no scene configured",
                self._config.white_out_program,
            )
            return
        try:
            await self._ledfx.activate_scene(scene_id)
            self._applied_scene = scene_id
            _LOGGER.info(
                "White-out ON -> program %d scene '%s'",
                self._config.white_out_program,
                scene_id,
            )
        except LedFxError as err:
            _LOGGER.warning("White-out scene '%s' failed: %s", scene_id, err)

    async def _exit_whiteout(self) -> None:
        self._whiteout_active = False
        # Resume: forget the applied program so the scene worker re-activates
        # whatever channel 8 currently selects, and re-apply colour override.
        self._applied_program = None
        self._candidate_program = None
        self._applied_color = None
        self._force_color_resend = True
        _LOGGER.info("White-out OFF - resuming normal program selection")

    # --- status for the web UI ------------------------------------------

    def status(self) -> dict:
        return {
            "channels": self.channels,
            "program": self._applied_program,
            "scene": self._applied_scene,
            "brightness": self._applied_brightness,
            "color": self._applied_color if self._color_override_active() else None,
            "color_override_active": self._color_override_active(),
            "color_targets": sorted(self._color_targets),
            "known_scenes": sorted(self._known_scenes),
            "control_scenes": self._config.control_scenes,
            "control_brightness": self._config.control_brightness,
            "control_color": self._config.control_color,
            "white_out": self._config.white_out,
            "whiteout_active": self._whiteout_active,
            # Live diagnostics so the UI can show why white-out does/doesn't fire.
            "whiteout_condition": self._whiteout_condition(),
            "white_out_program": self._config.white_out_program,
            "white_out_scene": self._scene_for_program(
                self._config.white_out_program
            ),
            "white_out_threshold": self._config.white_out_threshold,
        }
