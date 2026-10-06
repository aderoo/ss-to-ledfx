"""Minimal Art-Net receiver with discovery.

We receive ArtDmx for one configured universe and hand the raw DMX buffer to a
callback. We also answer ArtPoll with ArtPollReply so controllers (SoundSwitch)
that auto-discover nodes will see this bridge and offer it as an Art-Net output
for our universe. An unsolicited ArtPollReply is also broadcast at startup.

ArtDmx packet layout:
    [0:8]   ID        b"Art-Net\\0"
    [8:10]  OpCode    0x5000, little-endian  -> bytes 0x00, 0x50
    [10:12] ProtVer   big-endian (>= 14)
    [12]    Sequence
    [13]    Physical
    [14]    SubUni    low byte of the 15-bit port address
    [15]    Net       high byte of the 15-bit port address
    [16:18] Length    big-endian, number of DMX data bytes (1..512)
    [18:..] Data      DMX channel values
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

ART_NET_ID = b"Art-Net\x00"
OP_DMX = 0x5000
OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
HEADER_LEN = 18
ART_NET_PORT = 0x1936  # 6454

SHORT_NAME = "ss-to-ledfx"
LONG_NAME = "SoundSwitch -> LedFx bridge"

# Signature: (port_address, dmx_bytes) -> None
DmxCallback = Callable[[int, bytes], None]


def opcode_of(packet: bytes) -> int | None:
    """Return the little-endian OpCode of an Art-Net packet, else None."""
    if len(packet) < 10 or packet[0:8] != ART_NET_ID:
        return None
    return packet[8] | (packet[9] << 8)


def local_ip_for(peer_host: str) -> str:
    """Best-effort local IP on the interface that reaches peer_host."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer_host, ART_NET_PORT))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def build_artpollreply(ip: str, universe: int, bind_index: int = 1) -> bytes:
    """Build a 239-byte ArtPollReply advertising one DMX output for `universe`.

    We present as a node with a single Art-Net output port bound to the
    configured 15-bit port address, which is how a controller learns it may
    send our universe here.
    """
    p = bytearray(239)
    p[0:8] = ART_NET_ID
    p[8] = OP_POLL_REPLY & 0xFF
    p[9] = (OP_POLL_REPLY >> 8) & 0xFF
    try:
        p[10:14] = socket.inet_aton(ip)
    except OSError:
        p[10:14] = b"\x00\x00\x00\x00"
    p[14] = ART_NET_PORT & 0xFF  # Port, little-endian
    p[15] = (ART_NET_PORT >> 8) & 0xFF
    p[16] = 0  # VersInfoH
    p[17] = 1  # VersInfoL
    p[18] = (universe >> 8) & 0x7F  # NetSwitch
    p[19] = (universe >> 4) & 0x0F  # SubSwitch
    p[20] = 0x00  # OemHi
    p[21] = 0xFF  # OemLo (unknown OEM)
    p[23] = 0xD0  # Status1: indicators normal, address set by network
    # EstaMan (p[24:26]) left 0.
    name = SHORT_NAME.encode()[:17]
    p[26 : 26 + len(name)] = name  # ShortName[18]
    lname = LONG_NAME.encode()[:63]
    p[44 : 44 + len(lname)] = lname  # LongName[64]
    report = f"#0001 [0000] {LONG_NAME}".encode()[:63]
    p[108 : 108 + len(report)] = report  # NodeReport[64]
    p[172] = 0  # NumPortsHi
    p[173] = 1  # NumPortsLo
    p[174] = 0x80  # PortTypes[0]: output, Art-Net/DMX512
    p[182] = 0x80  # GoodOutput[0]: data is being transmitted
    p[190] = universe & 0x0F  # SwOut[0]
    p[200] = 0x00  # Style = StNode
    p[201:207] = uuid.getnode().to_bytes(6, "big")  # MAC
    try:
        p[207:211] = socket.inet_aton(ip)  # BindIp
    except OSError:
        pass
    p[211] = bind_index & 0xFF  # BindIndex
    p[212] = 0x08  # Status2: supports 15-bit port address
    return bytes(p)


def parse_artdmx(packet: bytes) -> tuple[int, bytes] | None:
    """Return (port_address, dmx_data) for an ArtDmx packet, else None."""
    if len(packet) < HEADER_LEN:
        return None
    if packet[0:8] != ART_NET_ID:
        return None
    opcode = packet[8] | (packet[9] << 8)  # little-endian
    if opcode != OP_DMX:
        return None
    port_address = packet[14] | (packet[15] << 8)  # SubUni | Net << 8
    length = (packet[16] << 8) | packet[17]  # big-endian
    data = packet[HEADER_LEN : HEADER_LEN + length]
    return port_address, data


class _ArtNetProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        universe: int,
        on_dmx: DmxCallback,
        on_stats: Callable[[], None],
        on_poll: Callable[[tuple[str, int]], None],
    ):
        self._universe = universe
        self._on_dmx = on_dmx
        self._on_stats = on_stats
        self._on_poll = on_poll
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._on_stats()
        opcode = opcode_of(data)
        if opcode == OP_POLL:
            self._on_poll(addr)
            return
        parsed = parse_artdmx(data)
        if parsed is None:
            return
        port_address, dmx = parsed
        if port_address != self._universe:
            return
        self._on_dmx(port_address, dmx)

    def error_received(self, exc: Exception) -> None:  # pragma: no cover
        _LOGGER.warning("Art-Net socket error: %s", exc)


class ArtNetReceiver:
    """Binds a UDP socket and forwards ArtDmx for one universe to a callback."""

    def __init__(
        self,
        bind_host: str,
        bind_port: int,
        universe: int,
        on_dmx: DmxCallback,
    ):
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.universe = universe
        self._on_dmx = on_dmx
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _ArtNetProtocol | None = None

        # Stats, read by the web UI.
        self.packets_received = 0
        self.last_packet_time: float | None = None
        self.polls_received = 0

    def _bump_stats(self) -> None:
        self.packets_received += 1
        self.last_packet_time = asyncio.get_running_loop().time()

    def _reply_to_poll(self, addr: tuple[str, int]) -> None:
        self.polls_received += 1
        _LOGGER.info("ArtPoll from %s - replying with ArtPollReply", addr[0])
        # Report the IP on the interface that reaches the poller.
        reply = build_artpollreply(local_ip_for(addr[0]), self.universe)
        self._send(reply, addr)
        # Also broadcast, as some controllers expect a broadcast reply.
        self._send(reply, ("255.255.255.255", self.bind_port))

    def _send(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._transport is not None:
            try:
                self._transport.sendto(data, addr)
            except OSError as err:  # pragma: no cover
                _LOGGER.debug("Art-Net send to %s failed: %s", addr, err)

    def announce(self) -> None:
        """Broadcast an unsolicited ArtPollReply so idle controllers learn us."""
        reply = build_artpollreply(local_ip_for("255.255.255.255"), self.universe)
        self._send(reply, ("255.255.255.255", self.bind_port))

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        # Create the socket ourselves so we can set SO_REUSEADDR/SO_REUSEPORT,
        # letting other Art-Net software on the Mac share the port.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:  # pragma: no cover - platform dependent
                pass
        # Allow receiving broadcast ArtDmx.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((self.bind_host, self.bind_port))
        sock.setblocking(False)

        self._transport, self._protocol = await loop.create_datagram_endpoint(
            lambda: _ArtNetProtocol(
                self.universe, self._on_dmx, self._bump_stats, self._reply_to_poll
            ),
            sock=sock,
        )
        _LOGGER.info(
            "Listening for Art-Net on %s:%d (universe %d)",
            self.bind_host,
            self.bind_port,
            self.universe,
        )
        self.announce()

    def stop(self) -> None:
        if self._transport is not None:
            self._transport.close()
            self._transport = None
