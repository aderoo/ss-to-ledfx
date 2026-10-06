"""Entry point: python -m ss_to_ledfx"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .app import App
from .config import Config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ss-to-ledfx",
        description="Bridge SoundSwitch Art-Net DMX to LedFx scenes and brightness.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="Path to config.json (default: next to the package).",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    config = Config.load(args.config)
    app = App(config)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
