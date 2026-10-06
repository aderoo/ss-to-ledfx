"""Configuration for the SoundSwitch -> LedFx bridge.

Settings live in a JSON file next to the tool so they survive restarts and can
be edited by hand or from the web UI. Unknown keys in the file are ignored and
missing keys fall back to defaults, so a config written by an older/newer build
still loads.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# Freedom Flex Stick, 15CH mode.
FIXTURE_CHANNELS = 15


@dataclass
class ArtNetConfig:
    bind_host: str = "0.0.0.0"
    bind_port: int = 6454
    # The 15-bit Art-Net port address (Net << 8 | SubNet << 4 | Universe).
    # Most Art-Net software calls this simply the "universe".
    universe: int = 0


@dataclass
class LedFxConfig:
    base_url: str = "http://localhost:8888"


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8890


@dataclass
class Config:
    artnet: ArtNetConfig = field(default_factory=ArtNetConfig)
    ledfx: LedFxConfig = field(default_factory=LedFxConfig)
    web: WebConfig = field(default_factory=WebConfig)

    # 1-based DMX address of fixture channel 1.
    start_address: int = 1

    # Ordered list of LedFx scene IDs. Index 0 == Auto program 1.
    scenes: list[str] = field(default_factory=list)

    # Parallel to `scenes`: when True, program N lets the SoundSwitch RGBW
    # channels (2-5) override the active effect's colour while that scene runs.
    color_override: list[bool] = field(default_factory=list)

    # None -> keep the current scene when channel 8 reads "no function"
    # (<= 10). A scene ID switches to that scene instead.
    no_function_scene: str | None = None

    scene_debounce_ms: int = 100
    brightness_max_rate_hz: float = 25.0
    color_max_rate_hz: float = 40.0

    # Transition time (seconds) forced on colour-override target virtuals.
    # 0 = instant: colour snaps with the DMX instead of crossfading. Raise it
    # to deliberately smooth the colour.
    color_transition_time: float = 0.0

    # Master switches for the behaviours.
    control_brightness: bool = True
    control_scenes: bool = True
    control_color: bool = True

    # White-out (global override): when the dimmer (ch1) and all RGBW channels
    # (ch2-5) are at or above white_out_threshold, temporarily activate the
    # white_out_program's scene (your white-out look). When the condition
    # clears, normal program selection (channel 8) resumes.
    white_out: bool = False
    white_out_threshold: int = 255
    white_out_program: int = 1

    # --- persistence -----------------------------------------------------

    _path: Path = field(default=DEFAULT_CONFIG_PATH, repr=False, compare=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = Path(path) if path else DEFAULT_CONFIG_PATH
        cfg = cls()
        cfg._path = path
        if path.exists():
            try:
                cfg.apply(json.loads(path.read_text()))
                _LOGGER.info("Loaded config from %s", path)
            except (json.JSONDecodeError, OSError) as err:
                _LOGGER.error("Could not read config %s: %s - using defaults", path, err)
        else:
            _LOGGER.info("No config at %s - using defaults", path)
            cfg.save()
        return cfg

    def apply(self, data: dict[str, Any]) -> None:
        """Merge a (partial) dict into this config, in place."""
        nested = {
            "artnet": ArtNetConfig,
            "ledfx": LedFxConfig,
            "web": WebConfig,
        }
        for key, sub_cls in nested.items():
            if isinstance(data.get(key), dict):
                current = asdict(getattr(self, key))
                current.update(data[key])
                valid = {f.name for f in fields(sub_cls)}
                setattr(
                    self, key, sub_cls(**{k: v for k, v in current.items() if k in valid})
                )
        scalar = {
            "start_address",
            "scenes",
            "color_override",
            "no_function_scene",
            "scene_debounce_ms",
            "brightness_max_rate_hz",
            "color_max_rate_hz",
            "color_transition_time",
            "control_brightness",
            "control_scenes",
            "control_color",
            "white_out",
            "white_out_threshold",
            "white_out_program",
        }
        for key in scalar:
            if key in data:
                setattr(self, key, data[key])

    def to_dict(self) -> dict[str, Any]:
        return {
            "artnet": asdict(self.artnet),
            "ledfx": asdict(self.ledfx),
            "web": asdict(self.web),
            "start_address": self.start_address,
            "scenes": list(self.scenes),
            "color_override": list(self.color_override),
            "no_function_scene": self.no_function_scene,
            "scene_debounce_ms": self.scene_debounce_ms,
            "brightness_max_rate_hz": self.brightness_max_rate_hz,
            "color_max_rate_hz": self.color_max_rate_hz,
            "color_transition_time": self.color_transition_time,
            "control_brightness": self.control_brightness,
            "control_scenes": self.control_scenes,
            "control_color": self.control_color,
            "white_out": self.white_out,
            "white_out_threshold": self.white_out_threshold,
            "white_out_program": self.white_out_program,
        }

    def save(self) -> None:
        with self._lock:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=2))
            tmp.replace(self._path)
        _LOGGER.debug("Saved config to %s", self._path)
