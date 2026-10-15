"""KWP2000 auf der K-Line (ISO 14230-2) - Rahmen fuer OBD-II.

Auf der K-Line sprechen Steuergeraete nach der 5-Baud-Adressierung entweder
den KWP1281-Blockdialog oder ISO-14230-Rahmen. Ein Rahmen besteht aus

    FMT  Zielquelladresse  Quelladresse  Daten...  CS

``FMT`` traegt in den unteren sechs Bit die Anzahl der folgenden Bytes
(Adressen + Daten + Pruefsumme). Die Pruefsumme ist die Summe aller Bytes
ausser ihr selbst, modulo 256. Fuer OBD-II (SAE J1979) gelten die Dienste
``01`` bis ``0A`` unveraendert.

Das Modul liefert das Rahmenformat sowie die Testerseite; die Steuergeraeteseite
sitzt im KWP1281-Server (:mod:`vagdiag.proto.kw1281`), weil sie den Dialog nach
der 5-Baud-Adressierung in derselben Sitzung weiterfuehrt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..proto.bus import KLineBus, KLineNode

__all__ = ["Iso14230Frame", "Iso14230Error", "encode_frame", "decode_frame", "Kwp2000KLineTester"]

#: Adresse des Testers auf der K-Line
TESTER_ADDRESS = 0xF1


class Iso14230Error(Exception):
    """Fehler im ISO-14230-Rahmen (Pruefsumme, Laenge, Adressierung)."""


@dataclass
class Iso14230Frame:
    """Ein ISO-14230-Rahmen mit Ziel- und Quelladresse."""

    target: int
    source: int
    data: bytes

    @property
    def fmt(self) -> int:
        return 0x80 | (len(self.data) + 3)

    def to_bytes(self) -> bytes:
        header = bytes([self.fmt, self.target, self.source]) + self.data
        return header + bytes([sum(header) & 0xFF])

    @classmethod
    def from_bytes(cls, fmt: int, rest: bytes) -> "Iso14230Frame":
        if len(rest) < 3:
            raise Iso14230Error("Rahmen zu kurz")
        body, checksum = rest[:-1], rest[-1]
        if (sum(bytes([fmt]) + body) & 0xFF) != checksum:
            raise Iso14230Error("Pruefsumme stimmt nicht")
        return cls(target=body[0], source=body[1], data=body[2:])


def encode_frame(target: int, source: int, data: bytes) -> bytes:
    return Iso14230Frame(target=target, source=source, data=bytes(data)).to_bytes()


def decode_frame(raw: bytes) -> Iso14230Frame:
    """Zerlegt einen vollstaendigen Rahmen (mit FMT als erstem Byte)."""
    if not raw:
        raise Iso14230Error("leerer Rahmen")
    fmt = raw[0]
    if not fmt & 0x80:
        raise Iso14230Error("kein ISO-14230-Rahmen (FMT ohne Bit 7)")
    length = fmt & 0x3F
    rest = raw[1 : 1 + length]
    if len(rest) != length:
        raise Iso14230Error(f"Rahmen unvollstaendig ({len(rest)} von {length} Bytes)")
    return Iso14230Frame.from_bytes(fmt, rest)


class Kwp2000KLineTester(KLineNode):
    """Tester fuer KWP2000-Rahmen auf der K-Line (OBD-II)."""

    def __init__(
        self,
        name: str,
        bus: KLineBus,
        *,
        log: Callable[[str], None] | None = None,
        tester_address: int = TESTER_ADDRESS,
    ) -> None:
        super().__init__(name, bus)
        self.log = log or (lambda text: None)
        self.tester_address = tester_address
        #: Der Bus fragt hierueber die Testeradresse fuer die 5-Baud-Adressierung ab
        self.address = tester_address
        self.ecu_address = 0
        self.protocol_version = 0
        self.transcript: list[str] = []

    # -- Dialog --------------------------------------------------------------- #
    def connect(self, ecu_address: int, *, timeout: float = 1.0) -> int:
        """5-Baud-Adressierung und Keyword-Austausch (wie beim KWP1281-Start)."""
        self.ecu_address = int(ecu_address)
        self.flush()
        self.protocol_version = self.bus.wakeup(self, self.ecu_address)
        sync = self.read_byte(timeout=timeout)
        kw_lsb = self.read_byte(timeout=timeout)
        kw_msb = self.read_byte(timeout=timeout)
        if sync != 0x55:
            raise Iso14230Error(f"Unerwartetes Syncbyte 0x{sync:02X} (erwartet 0x55)")
        self.send_bytes(bytes([(~kw_msb) & 0xFF]))
        self.expect_echo((~kw_msb) & 0xFF, timeout=timeout)
        protocol = ((kw_msb & 0x7F) << 7) + (kw_lsb & 0x7F)
        self.protocol_version = protocol
        self.log(f"K-Line: Steuergeraet 0x{self.ecu_address:02X} meldet KW{protocol}")
        # Nach der Adressierung schickt das Steuergeraet seine Identbloecke;
        # VCDS zeigt sie an, der OBD-Dialog arbeitet danach weiter.
        self.ident_blocks = self._skip_ident_blocks(timeout=timeout)
        return protocol

    def _skip_ident_blocks(self, *, timeout: float) -> list[bytes]:
        """Liest und quittiert die Identbloecke am Dialoganfang.

        Die Steuergeraete schicken nach der 5-Baud-Adressierung zuerst ihre
        Identifikation im KWP1281-Blockformat (so macht es VAG auch am echten
        Fahrzeug). Der Tester quittiert jeden Block; danach beginnt der
        ISO-14230-Rahmenwechsel.
        """
        from .kw1281 import Block, BlockTitle  # lokaler Import vermeidet den Zyklus

        blocks: list[bytes] = []
        self.counter: int | None = None
        while True:
            try:
                first = self.read_byte(timeout=timeout)
            except TimeoutError:
                return blocks
            if first & 0x80:
                # Rahmenanfang - gehoert schon zur Antwort, zuruecklegen
                self._pushback(first)
                return blocks
            try:
                raw = bytes([first]) + bytes(
                    self.read_byte(timeout=timeout) for _ in range(first)
                )
            except TimeoutError:  # pragma: no cover - unvollstaendiger Block
                return blocks
            blocks.append(raw)
            try:
                block = Block.from_bytes(raw)
            except Exception:  # pragma: no cover - ungueltiger Block
                continue
            self.counter = (block.counter + 1) & 0xFF
            if block.title == BlockTitle.END:
                return blocks
            # Quittung des Testers (gleiche Blockzaehlung wie bei KWP1281)
            ack = Block(counter=self.counter & 0xFF, title=int(BlockTitle.ACK), body=b"")
            self.counter = (self.counter + 1) & 0xFF
            for byte in ack.to_bytes():
                self.send_bytes(bytes([byte]))
                self.expect_echo(byte, timeout=timeout)

    def request(self, data: bytes, *, timeout: float = 1.0) -> bytes:
        """Sendet einen Dienst und liefert die Antwortdaten (ohne Adressen)."""
        if self.ecu_address == 0:
            raise Iso14230Error("Kein Dialog aufgebaut")
        self.flush()
        frame = Iso14230Frame(
            target=self.ecu_address, source=self.tester_address, data=bytes(data)
        ).to_bytes()
        for byte in frame:
            self.send_bytes(bytes([byte]))
            self.expect_echo(byte, timeout=timeout)
        self.transcript.append(f"T {frame.hex(' ')}")
        return self._read_answer(timeout=timeout)

    def _read_answer(self, *, timeout: float) -> bytes:
        fmt = self.read_byte(timeout=timeout)
        if not fmt & 0x80:
            raise Iso14230Error(f"kein Rahmenanfang (0x{fmt:02X})")
        length = fmt & 0x3F
        rest = bytes(self.read_byte(timeout=timeout) for _ in range(length))
        answer = Iso14230Frame.from_bytes(fmt, rest)
        self.transcript.append(f"E {bytes([fmt]).hex(' ')} {rest.hex(' ')}")
        return answer.data
