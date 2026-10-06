"""Web UI + JSON API served by aiohttp, in the same process as the bridge.

Routes:
  GET  /                 -> the single-page UI
  GET  /api/status       -> live status (Art-Net rate, LedFx health, channels)
  GET  /api/settings     -> current config
  POST /api/settings     -> update config (saved to disk; Art-Net rebound if needed)
  GET  /api/ledfx/scenes -> scene IDs + names, for the mapping dropdowns
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .ledfx_client import LedFxError

if TYPE_CHECKING:
    from .app import App

_LOGGER = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "web"


class WebServer:
    def __init__(self, app: "App"):
        self._app = app
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        aio = web.Application()
        aio.add_routes(
            [
                web.get("/", self._index),
                web.get("/api/status", self._status),
                web.get("/api/settings", self._get_settings),
                web.post("/api/settings", self._post_settings),
                web.get("/api/ledfx/scenes", self._ledfx_scenes),
            ]
        )
        self._runner = web.AppRunner(aio)
        await self._runner.setup()
        cfg = self._app.config.web
        site = web.TCPSite(self._runner, cfg.host, cfg.port)
        await site.start()
        shown = "localhost" if cfg.host == "0.0.0.0" else cfg.host
        _LOGGER.info("Web UI at http://%s:%d", shown, cfg.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- handlers --------------------------------------------------------

    async def _index(self, _request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _status(self, _request: web.Request) -> web.Response:
        recv = self._app.receiver
        now = time.monotonic() if recv else 0.0
        last = recv.last_packet_time if recv else None
        seconds_since = (now - last) if last is not None else None
        return web.json_response(
            {
                "artnet": {
                    "packets_received": recv.packets_received if recv else 0,
                    "seconds_since_last_packet": seconds_since,
                    "receiving": seconds_since is not None and seconds_since < 3.0,
                    "polls_received": recv.polls_received if recv else 0,
                    "universe": self._app.config.artnet.universe,
                    "bind": f"{self._app.config.artnet.bind_host}:"
                    f"{self._app.config.artnet.bind_port}",
                },
                "ledfx": {
                    "base_url": self._app.ledfx.base_url,
                    "reachable": self._app.ledfx.reachable,
                },
                "bridge": self._app.bridge.status(),
            }
        )

    async def _get_settings(self, _request: web.Request) -> web.Response:
        return web.json_response(self._app.config.to_dict())

    async def _post_settings(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid JSON"}, status=400)

        cfg = self._app.config
        old_artnet = (cfg.artnet.bind_host, cfg.artnet.bind_port, cfg.artnet.universe)
        old_ledfx_url = cfg.ledfx.base_url

        cfg.apply(data)
        cfg.save()

        # Apply live changes.
        if cfg.ledfx.base_url != old_ledfx_url:
            self._app.ledfx.set_base_url(cfg.ledfx.base_url)
        new_artnet = (cfg.artnet.bind_host, cfg.artnet.bind_port, cfg.artnet.universe)
        if new_artnet != old_artnet:
            await self._app.restart_receiver()
        # Re-validate the mapping against LedFx and re-apply the held program
        # so a mapping edit takes effect without waiting for the next cue.
        self._app.bridge.reset_scene_state()
        await self._app.bridge.refresh_known_scenes()

        return web.json_response({"status": "ok", "settings": cfg.to_dict()})

    async def _ledfx_scenes(self, _request: web.Request) -> web.Response:
        try:
            scenes = await self._app.ledfx.get_scenes()
        except LedFxError as err:
            return web.json_response({"error": str(err), "scenes": []})
        out = [
            {"id": scene_id, "name": payload.get("name", scene_id)}
            for scene_id, payload in sorted(scenes.items())
        ]
        return web.json_response({"scenes": out})
