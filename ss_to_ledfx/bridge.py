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
CH_PROGRAM = 8

MAX_PROGRAM = 25


def program_from_value(value: int) -> int | None:
    """Freedom Flex program chart: ranges are 8 wide from value 11.

    Returns the 1-based program number, or None for "no function" (<= 10).
    """
    if value <= 10:
        return None
    return min(MAX_PROGRAM, (value - 11) // 8 + 1)


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

    def start(self) -> None:
        self._running = True
        self._tasks = [
            asyncio.create_task(self._scene_worker(), name="scene_worker"),
            asyncio.create_task(self._brightness_worker(), name="brightness_worker"),
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
            if not self._config.control_scenes:
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
            except LedFxError as err:
                _LOGGER.warning("Failed to activate scene '%s': %s", scene_id, err)
                # Allow a retry on the next change.
                self._applied_program = None

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

    # --- status for the web UI ------------------------------------------

    def status(self) -> dict:
        return {
            "channels": self.channels,
            "program": self._applied_program,
            "scene": self._applied_scene,
            "brightness": self._applied_brightness,
            "known_scenes": sorted(self._known_scenes),
            "control_scenes": self._config.control_scenes,
            "control_brightness": self._config.control_brightness,
        }
