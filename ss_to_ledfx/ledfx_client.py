"""Thin async client for the LedFx REST API.

Only the endpoints the bridge needs, all verified against the LedFx source:

  GET  /api/scenes          -> {"scenes": {scene_id: {...}}}
  PUT  /api/scenes          {"id": <id>, "action": "activate"}
  PUT  /api/config          {"global_brightness": <0.0-1.0>}   (partial patch)
  GET  /api/info            -> liveness check

LedFx on localhost needs no auth token; access is gated only by an
origin/host check that server-to-server requests pass.
"""

from __future__ import annotations

import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)


class LedFxError(Exception):
    pass


class LedFxClient:
    def __init__(self, base_url: str, timeout: float = 4.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self.reachable = False

    @property
    def base_url(self) -> str:
        return self._base_url

    def set_base_url(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _request(self, method: str, path: str, **kwargs):
        if self._session is None:
            raise LedFxError("client not started")
        try:
            async with self._session.request(method, self._url(path), **kwargs) as resp:
                body = await resp.json(content_type=None)
                self.reachable = True
                if resp.status >= 400:
                    raise LedFxError(f"{method} {path} -> HTTP {resp.status}: {body}")
                return body
        except aiohttp.ClientError as err:
            self.reachable = False
            raise LedFxError(f"{method} {path} failed: {err}") from err

    async def ping(self) -> bool:
        """Best-effort liveness check; updates self.reachable."""
        try:
            await self._request("GET", "/api/info")
            return True
        except LedFxError:
            return False

    async def get_scenes(self) -> dict[str, dict]:
        """Return the scenes mapping {scene_id: payload}."""
        body = await self._request("GET", "/api/scenes")
        scenes = body.get("scenes", {})
        if not isinstance(scenes, dict):
            raise LedFxError(f"Unexpected /api/scenes response: {body}")
        return scenes

    async def activate_scene(self, scene_id: str) -> None:
        body = await self._request(
            "PUT", "/api/scenes", json={"id": scene_id, "action": "activate"}
        )
        # LedFx reports domain failures with HTTP 200 and status "failed".
        if isinstance(body, dict) and body.get("status") == "failed":
            reason = body.get("payload", {}).get("reason", body)
            raise LedFxError(f"activate_scene({scene_id}): {reason}")

    async def set_global_brightness(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        body = await self._request(
            "PUT", "/api/config", json={"global_brightness": value}
        )
        if isinstance(body, dict) and body.get("status") == "failed":
            reason = body.get("payload", {}).get("reason", body)
            raise LedFxError(f"set_global_brightness({value}): {reason}")
