"""Send ArtDmx packets to the bridge for testing without SoundSwitch.

Example:
    # ramp the dimmer (ch 1) and set program 3 (ch 8) on universe 0
    python tools/send_test_dmx.py --universe 0 --host 127.0.0.1 \
        --set 1=128 --set 8=27
"""

from __future__ import annotations

import argparse
import socket
import time

ART_NET_ID = b"Art-Net\x00"
OP_DMX = 0x5000


def build_artdmx(universe: int, data: bytes, sequence: int = 0) -> bytes:
    length = len(data)
    return (
        ART_NET_ID
        + bytes([OP_DMX & 0xFF, (OP_DMX >> 8) & 0xFF])  # opcode, little-endian
        + bytes([0x00, 14])  # protocol version, big-endian
        + bytes([sequence, 0])  # sequence, physical
        + bytes([universe & 0xFF, (universe >> 8) & 0xFF])  # SubUni, Net
        + bytes([(length >> 8) & 0xFF, length & 0xFF])  # length, big-endian
        + data
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6454)
    ap.add_argument("--universe", type=int, default=0)
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="CH=VAL",
        help="Set 1-based DMX channel to value (repeatable).",
    )
    ap.add_argument("--repeat", type=int, default=20, help="Packets to send.")
    ap.add_argument("--fps", type=float, default=40.0)
    args = ap.parse_args()

    dmx = bytearray(512)
    for item in args.set:
        ch, _, val = item.partition("=")
        dmx[int(ch) - 1] = int(val) & 0xFF

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    packet = build_artdmx(args.universe, bytes(dmx))
    interval = 1.0 / args.fps
    for i in range(args.repeat):
        sock.sendto(build_artdmx(args.universe, bytes(dmx), i & 0xFF), (args.host, args.port))
        time.sleep(interval)
    print(f"Sent {args.repeat} ArtDmx packets to {args.host}:{args.port} (universe {args.universe})")


if __name__ == "__main__":
    main()
