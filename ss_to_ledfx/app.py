"""Wires the receiver, LedFx client, bridge and web UI into one asyncio app."""

from __future__ import annotations

import asyncio
import logging

from .artnet import ArtNetReceiver
from .bridge import Bridge
from .config import Config
from .ledfx_client import LedFxClient
from .web import WebServer

_LOGGER = logging.getLogger(__name__)


class App:
    def __init__(self, config: Config):
        self.config = config
        self.ledfx = LedFxClient(config.ledfx.base_url)
        self.bridge = Bridge(config, self.ledfx)
        self.web = WebServer(self)
        self.receiver: ArtNetReceiver | None = None
        self._ledfx_watch_task: asyncio.Task | None = None

    async def _build_receiver(self) -> ArtNetReceiver:
        recv = ArtNetReceiver(
            self.config.artnet.bind_host,
            self.config.artnet.bind_port,
            self.config.artnet.universe,
            self.bridge.update_dmx,
        )
        await recv.start()
        return recv

    async def restart_receiver(self) -> None:
        if self.receiver is not None:
            self.receiver.stop()
        self.receiver = await self._build_receiver()

    async def _ledfx_watchdog(self) -> None:
        """Periodically ping LedFx so the UI shows a current reachable state."""
        while True:
            await self.ledfx.ping()
            await asyncio.sleep(5.0)

    async def run(self) -> None:
        await self.ledfx.start()
        await self.ledfx.ping()
        await self.bridge.refresh_known_scenes()
        self.bridge.start()
        self.receiver = await self._build_receiver()
        await self.web.start()
        self._ledfx_watch_task = asyncio.create_task(self._ledfx_watchdog())

        _LOGGER.info("ss-to-ledfx bridge running. Press Ctrl+C to stop.")
        try:
            await asyncio.Event().wait()  # run until cancelled
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._ledfx_watch_task:
            self._ledfx_watch_task.cancel()
        if self.receiver:
            self.receiver.stop()
        await self.bridge.stop()
        await self.web.stop()
        await self.ledfx.close()
        _LOGGER.info("Shut down cleanly.")
