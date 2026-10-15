"""KWP1281 - das klassische VAG-Blockprotokoll auf der K-Line.

Blockaufbau (siehe ``kw1281test``, ``Blocks/Block.cs`` sowie ``docs/PROTOKOLLE.md``)::

    +--------+---------+-------+---------------+------+
    | Länge  | Zähler  | Titel | Daten ...     | 0x03 |
    +--------+---------+-------+---------------+------+

* ``Länge`` zählt Zähler, Titel, Daten und das Endbyte ``0x03``.
* ``Zähler`` ist ein gemeinsamer, fortlaufender Blockzähler beider Seiten.
* Jedes Byte wird vom Empfänger mit seinem Einerkomplement quittiert
  (erledigt in :mod:`vagdiag.proto.bus` über den virtuellen K-Line-Bus).
* Empfangene Blöcke werden zusätzlich mit einem Quittungsblock (Titel ``0x09``)
  bestätigt; ``0x0A`` ist eine negative Quittung.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Callable, Iterable, Sequence

from .bus import KLineBus, KLineEcu, KLineNode

__all__ = [
    "BlockTitle",
    "Block",
    "FaultEntry",
    "GroupReading",
    "IdentResult",
    "AdaptationResponse",
    "Kwp1281Error",
    "Kwp1281Tester",
    "Kwp1281EcuServer",
    "ModuleFunctions",
    "status_text",
]


class BlockTitle(IntEnum):
    """Blocktitel nach ``kw1281test``/``BlockTitle.cs``."""

    READ_IDENT = 0x00
    READ_RAM = 0x01
    GROUP_READ_RESPONSE_WITH_TEXT = 0x02
    READ_ROM_EEPROM = 0x03
    ACTUATOR_TEST = 0x04
    FAULT_CODES_DELETE = 0x05
    END = 0x06
    FAULT_CODES_READ = 0x07
    ACK = 0x09
    NAK = 0x0A
    SOFTWARE_CODING = 0x10
    BASIC_SETTING_RAW_DATA_READ = 0x11
    RAW_DATA_READ = 0x12
    READ_EEPROM = 0x19
    WRITE_EEPROM = 0x1A
    CUSTOM = 0x1B
    ADAPTATION_READ = 0x21
    ADAPTATION_TEST = 0x22
    BASIC_SETTING_READ = 0x28
    GROUP_READ = 0x29
    ADAPTATION_SAVE = 0x2A
    LOGIN = 0x2B
    SECURITY_ACCESS_MODE2 = 0x3D
    SECURITY_IMMO_ACCESS1 = 0x5C
    SECURITY_ACCESS_MODE1 = 0xD7
    ADAPTATION_RESPONSE = 0xE6
    GROUP_READ_RESPONSE = 0xE7
    READ_EEPROM_RESPONSE = 0xEF
    RAW_DATA_READ_RESPONSE = 0xF4
    ACTUATOR_TEST_RESPONSE = 0xF5
    ASCII_DATA = 0xF6
    WRITE_EEPROM_RESPONSE = 0xF9
    FAULT_CODES_RESPONSE = 0xFC
    READ_ROM_EEPROM_RESPONSE = 0xFD
    READ_RAM_RESPONSE = 0xFE


BLOCK_END = 0x03


class Kwp1281Error(Exception):
    """Fehler im KWP1281-Dialog."""


# --------------------------------------------------------------------------- #
# Blockcodec
# --------------------------------------------------------------------------- #
@dataclass
class Block:
    """Ein KWP1281-Block ohne Laengen- und Endbyte."""

    counter: int
    title: int
    body: bytes = b""

    @property
    def length(self) -> int:
        return len(self.body) + 3  # Zähler + Titel + Endbyte

    @property
    def is_ack(self) -> bool:
        return self.title == BlockTitle.ACK

    @property
    def is_nak(self) -> bool:
        return self.title == BlockTitle.NAK

    @property
    def is_ack_nak(self) -> bool:
        return self.is_ack or self.is_nak

    def to_bytes(self) -> bytes:
        return bytes([self.length, self.counter, self.title, *self.body, BLOCK_END])

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Block":
        if len(raw) < 4:
            raise Kwp1281Error(f"Block zu kurz: {raw.hex(' ')}")
        length, counter, title = raw[0], raw[1], raw[2]
        if raw[-1] != BLOCK_END:
            raise Kwp1281Error(f"Blockende 0x03 fehlt: {raw.hex(' ')}")
        body = raw[3:-1]
        if length != len(body) + 3:
            raise Kwp1281Error(
                f"Laengenfeld {length} passt nicht zum Block ({len(body) + 3}): {raw.hex(' ')}"
            )
        return cls(counter=counter, title=title, body=body)

    def title_name(self) -> str:
        try:
            return BlockTitle(self.title).name
        except ValueError:
            return f"0x{self.title:02X}"

    def __str__(self) -> str:
        text = f"[{self.counter:02X}] {self.title_name()} ({len(self.body)} B)"
        if self.body:
            text += f" {self.body.hex(' ')}"
        return text


def status_text(status: int) -> str:
    """VCDS-Schreibweise des Statusbytes: ``AA-BB`` (BB = 10 wenn sporadisch)."""
    status1 = status & 0x7F
    status2 = (status >> 7) * 10
    return f"{status1:02d}-{status2:02d}"


@dataclass
class FaultEntry:
    """Fehlerspeichereintrag: VAG-Code (5-stellig) und Statusbyte."""

    code: int
    status: int

    @property
    def is_terminator(self) -> bool:
        return self.code == 0xFFFF and self.status == 0x88

    @property
    def status_text(self) -> str:
        return status_text(self.status)

    @property
    def sporadic(self) -> bool:
        return bool(self.status & 0x80)

    def as_dict(self) -> dict:
        return {
            "code": f"{self.code:05d}",
            "status": f"0x{self.status:02X}",
            "status_text": self.status_text,
            "sporadic": self.sporadic,
        }


@dataclass
class GroupReading:
    """Antwort auf einen Messwerteblock: Liste von (Sensortyp, A, B)."""

    group: int
    values: list[tuple[int, int, int]] = field(default_factory=list)
    text: str = ""

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "values": [list(v) for v in self.values],
            "text": self.text,
        }


@dataclass
class IdentResult:
    """Ergebnis der Steuergeraete-Identifikation."""

    lines: list[str] = field(default_factory=list)
    coding: int | None = None
    workshop_code: int | None = None

    @property
    def text(self) -> str:
        parts = list(self.lines)
        if self.coding is not None:
            parts.append(
                f"Codierung: {self.coding:05d}"
                + (f"   WSC: {self.workshop_code:05d}" if self.workshop_code is not None else "")
            )
        return "\n".join(parts)

    def as_dict(self) -> dict:
        return {
            "lines": self.lines,
            "coding": self.coding,
            "workshop_code": self.workshop_code,
            "text": self.text,
        }


@dataclass
class AdaptationResponse:
    channel: int
    value: int
    raw: bytes = b""


# --------------------------------------------------------------------------- #
# Tester
# --------------------------------------------------------------------------- #
class Kwp1281Tester(KLineNode):
    """Tester-Seite des KWP1281-Dialogs."""

    tester_address = 0xF1

    def __init__(self, name: str, bus: KLineBus, *, log: Callable[[str], None] | None = None) -> None:
        super().__init__(name, bus)
        self.address = 0
        self.ecu_address = 0
        self.counter: int | None = None
        self.connected = False
        self.protocol_version = 0
        self.log = log or (lambda text: None)
        self.transcript: list[str] = []
        self.ident_result: IdentResult | None = None

    def on_wakeup(self, tester_address: int) -> tuple[int, int]:  # pragma: no cover
        return (0x00, 0x00)

    # -- Dialogaufbau -------------------------------------------------------- #
    def connect(self, ecu_address: int, *, timeout: float = 1.0) -> IdentResult:
        """5-Baud-Init, Keyword-Austausch und Identifikationsblock lesen."""
        self.counter = None
        self.ecu_address = ecu_address
        self.protocol_version = self.bus.wakeup(self, ecu_address)
        # Keywords pruefen: 0x55 + Keyword-LSB + Keyword-MSB (siehe kw1281test, WakeUpNoRetry)
        sync = self.read_byte(timeout=timeout)
        kw_lsb = self.read_byte(timeout=timeout)
        kw_msb = self.read_byte(timeout=timeout)
        if sync != 0x55:
            raise Kwp1281Error(f"Unerwartetes Syncbyte 0x{sync:02X} (erwartet 0x55)")
        protocol = ((kw_msb & 0x7F) << 7) + (kw_lsb & 0x7F)
        if protocol != self.protocol_version:
            self.log(
                f"Warnung: Protokollversion aus Keywords ({protocol}) weicht von der "
                f"Busantwort ({self.protocol_version}) ab"
            )
        # Quittung: Komplement des Keyword-MSB (siehe kw1281test, WakeUpNoRetry)
        self.send_bytes(bytes([(~kw_msb) & 0xFF]))
        self.log(f"KWP{protocol}: Steuergeraet 0x{ecu_address:02X} antwortet")
        if protocol not in (1281, 2000):
            self.log(f"Warnung: unerwartete Protokollversion KWP{protocol}")
        self.connected = True
        blocks, _ = self._receive_blocks(timeout=timeout)
        ident = self._parse_ident(blocks)
        # Fordert das Steuergeraet weitere Identdaten an, wird 0x00 nachgeschoben
        while any(b.title == BlockTitle.ASCII_DATA and b.body and b.body[0] > 0x7F for b in blocks):
            self.send_block(BlockTitle.READ_IDENT)
            more, _ = self._receive_blocks(timeout=timeout)
            ident.lines.extend(self._parse_ident(more).lines)
            blocks = more
        return ident

    def end(self) -> None:
        """Beendet die Kommunikation (Block 0x06, keine Antwort)."""
        if not self.connected:
            return
        try:
            self.send_block(BlockTitle.END)
        finally:
            self.connected = False
            self.counter = None

    # -- Blockebene ---------------------------------------------------------- #
    def send_block(self, title: int, body: Sequence[int] = ()) -> Block:
        if self.counter is None:
            self.counter = 0x01
        block = Block(counter=self.counter & 0xFF, title=int(title), body=bytes(body))
        self.counter = (self.counter + 1) & 0xFF
        raw = block.to_bytes()
        self.log(f"-> {block}")
        self.transcript.append(f"T {raw.hex(' ')}")
        for byte in raw:
            self.send_bytes(bytes([byte]))
            self.expect_echo(byte)
            self.bus.clock.r6()
        return block

    def receive_block(self, timeout: float = 1.0) -> Block:
        """Liest genau einen Block vom Steuergeraet (ohne Quittung zu senden)."""
        raw = bytearray()
        length = self._read_and_ack(timeout)
        raw.append(length)
        if length < 3:
            raise Kwp1281Error(f"Ungueltige Blocklaenge {length}")
        raw.append(self._read_and_ack(timeout))  # Zähler
        raw.append(self._read_and_ack(timeout))  # Titel
        # Laengenfeld zaehlt Zaehler, Titel, Daten und Endbyte: es folgen noch
        # (length - 2) Bytes bis einschliesslich 0x03.
        for _ in range(length - 2):
            raw.append(self._read_and_ack(timeout))
        block = Block.from_bytes(bytes(raw))
        if self.counter is None:
            self.counter = block.counter
        elif block.counter != self.counter:
            self.log(
                f"Warnung: Blockzaehler {block.counter:02X} erwartet {self.counter:02X}"
            )
            self.counter = block.counter
        self.counter = (self.counter + 1) & 0xFF
        self.log(f"<- {block}")
        self.transcript.append(f"E {raw.hex(' ')}")
        return block

    def _read_and_ack(self, timeout: float) -> int:
        """Liest ein Byte vom Steuergeraet.

        Die Komplementquittung erzeugt der virtuelle K-Line-Bus automatisch beim
        Empfaenger (:meth:`vagdiag.proto.bus.KLineNode._receive_byte`); die
        Tester-Seite muss das empfangene Byte daher *nicht* zusaetzlich
        bestaetigen. Nach dem *Senden* eines Bytes wartet die Gegenseite
        ihrerseits auf unsere Quittung - die liefert der Bus ebenfalls.
        """
        byte = self.read_byte(timeout=timeout)
        self.bus.clock.r6()
        return byte

    def _receive_blocks(self, timeout: float = 1.0) -> tuple[list[Block], list[Block]]:
        """Liest Blocke bis zur abschliessenden Quittung.

        Liefert ``(Datenbloecke, alle Bloecke)``. Jeder Datenblock wird mit einem
        Quittungsblock bestaetigt; ein Quittungs- oder NAK-Block beendet die
        Uebertragung (siehe ``kw1281test``, ``KW1281Dialog.ReceiveBlocks``).
        """
        data: list[Block] = []
        everything: list[Block] = []
        deadline = time.time() + timeout
        while True:
            remaining = max(0.05, deadline - time.time())
            block = self.receive_block(timeout=remaining)
            everything.append(block)
            if block.is_ack_nak:
                return data, everything
            data.append(block)
            self.send_block(BlockTitle.ACK)

    # -- Kommandos ----------------------------------------------------------- #
    def read_ident(self, timeout: float = 1.0) -> IdentResult:
        self.send_block(BlockTitle.READ_IDENT)
        blocks, _ = self._receive_blocks(timeout=timeout)
        return self._parse_ident(blocks)

    def read_fault_codes(self, timeout: float = 1.0) -> list[FaultEntry]:
        self.send_block(BlockTitle.FAULT_CODES_READ)
        blocks, _ = self._receive_blocks(timeout=timeout)
        entries: list[FaultEntry] = []
        for block in blocks:
            if block.title != BlockTitle.FAULT_CODES_RESPONSE:
                raise Kwp1281Error(f"Unerwarteter Block {block.title_name()} statt Fehlercodes")
            entries.extend(self._parse_fault_body(block.body))
        return [e for e in entries if not e.is_terminator]

    def clear_fault_codes(self, timeout: float = 1.0) -> list[FaultEntry]:
        self.send_block(BlockTitle.FAULT_CODES_DELETE)
        blocks, _ = self._receive_blocks(timeout=timeout)
        entries: list[FaultEntry] = []
        for block in blocks:
            if block.title == BlockTitle.FAULT_CODES_RESPONSE:
                entries.extend(self._parse_fault_body(block.body))
        return [e for e in entries if not e.is_terminator]

    def group_read(self, group: int, *, basic_setting: bool = False, timeout: float = 1.0) -> GroupReading:
        title = BlockTitle.BASIC_SETTING_READ if basic_setting else BlockTitle.GROUP_READ
        self.send_block(title, [group & 0xFF])
        block = self.receive_block(timeout=timeout)
        if block.is_nak:
            raise Kwp1281Error(f"Messwerteblock {group:03d} nicht verfuegbar (NAK)")
        # Quittung senden, restliche Bloecke bis zur Abschlussquittung lesen
        data = [block]
        self.send_block(BlockTitle.ACK)
        more, _ = self._receive_blocks(timeout=timeout)
        data.extend(more)
        values: list[tuple[int, int, int]] = []
        text = ""
        for item in data:
            if item.title == BlockTitle.GROUP_READ_RESPONSE:
                body = item.body
                for index in range(0, len(body) - 2, 3):
                    values.append((body[index], body[index + 1], body[index + 2]))
            elif item.title == BlockTitle.GROUP_READ_RESPONSE_WITH_TEXT:
                text += "".join(chr(b & 0x7F) for b in item.body)
            elif item.title == BlockTitle.RAW_DATA_READ_RESPONSE:
                values.append((0, item.body[0] if item.body else 0, 0))
        return GroupReading(group=group, values=values, text=text)

    def adaptation_read(self, channel: int, timeout: float = 1.0) -> AdaptationResponse:
        return self._adaptation(BlockTitle.ADAPTATION_READ, channel, None, None, timeout)

    def adaptation_test(self, channel: int, value: int, timeout: float = 1.0) -> AdaptationResponse:
        return self._adaptation(BlockTitle.ADAPTATION_TEST, channel, value, None, timeout)

    def adaptation_save(
        self, channel: int, value: int, workshop_code: int = 12345, timeout: float = 1.0
    ) -> AdaptationResponse:
        return self._adaptation(BlockTitle.ADAPTATION_SAVE, channel, value, workshop_code, timeout)

    def _adaptation(
        self,
        title: int,
        channel: int,
        value: int | None,
        workshop_code: int | None,
        timeout: float,
    ) -> AdaptationResponse:
        body: list[int] = [channel & 0xFF]
        if value is not None:
            body += [(value >> 8) & 0xFF, value & 0xFF]
        if workshop_code is not None:
            body += [
                (workshop_code >> 16) & 0xFF,
                (workshop_code >> 8) & 0xFF,
                workshop_code & 0xFF,
            ]
        self.send_block(title, body)
        block = self.receive_block(timeout=timeout)
        if block.is_nak:
            raise Kwp1281Error(f"Anpassungskanal {channel:03d} nicht verfuegbar (NAK)")
        self.send_block(BlockTitle.ACK)
        self._receive_blocks(timeout=timeout)
        if block.title != BlockTitle.ADAPTATION_RESPONSE or len(block.body) < 3:
            raise Kwp1281Error(f"Unerwartete Anpassungsantwort: {block}")
        return AdaptationResponse(
            channel=block.body[0], value=(block.body[1] << 8) | block.body[2], raw=block.body
        )

    def login(self, code: int, workshop_code: int = 12345, timeout: float = 1.0) -> bool:
        body = [
            (code >> 8) & 0xFF,
            code & 0xFF,
            (workshop_code >> 16) & 0xFF,
            (workshop_code >> 8) & 0xFF,
            workshop_code & 0xFF,
        ]
        self.send_block(BlockTitle.LOGIN, body)
        block = self.receive_block(timeout=timeout)
        if block.is_ack:
            return True
        if block.is_nak:
            return False
        self.send_block(BlockTitle.ACK)
        self._receive_blocks(timeout=timeout)
        return True

    def write_coding(self, coding: int, workshop_code: int = 12345, timeout: float = 1.0) -> IdentResult:
        doubled = coding * 2
        if workshop_code > 0xFFFF:
            doubled += 1
        body = [
            (doubled >> 8) & 0xFF,
            doubled & 0xFF,
            (workshop_code & 0xFFFF) >> 8,
            workshop_code & 0xFF,
        ]
        self.send_block(BlockTitle.SOFTWARE_CODING, body)
        blocks, _ = self._receive_blocks(timeout=timeout)
        return self._parse_ident(blocks)

    def actuator_test(self, value: int, timeout: float = 1.0) -> bytes:
        self.send_block(BlockTitle.ACTUATOR_TEST, [value & 0xFF])
        blocks, _ = self._receive_blocks(timeout=timeout)
        for block in blocks:
            if block.title == BlockTitle.ACTUATOR_TEST_RESPONSE:
                return block.body
            if block.is_nak:
                raise Kwp1281Error("Stellgliedtest wurde abgelehnt")
        return b""

    # -- Auswertung ---------------------------------------------------------- #
    @staticmethod
    def _parse_fault_body(body: bytes) -> list[FaultEntry]:
        entries: list[FaultEntry] = []
        for index in range(0, len(body) - 2, 3):
            entries.append(FaultEntry((body[index] << 8) | body[index + 1], body[index + 2]))
        return entries

    @staticmethod
    def _parse_ident(blocks: Iterable[Block]) -> IdentResult:
        ident = IdentResult()
        for block in blocks:
            if block.title == BlockTitle.ASCII_DATA:
                if block.body and block.body[0] == 0x00:
                    data = block.body[1:]
                    if len(data) >= 4:
                        coding_doubled = (data[0] << 8) | data[1]
                        wsc = (data[2] << 8) | data[3]
                        if data[1] & 1:
                            wsc += 65536
                        ident.coding = coding_doubled // 2
                        ident.workshop_code = wsc
                else:
                    text = "".join(
                        chr(byte & 0x7F) for byte in block.body if byte not in (0x00,)
                    ).rstrip()
                    if text:
                        ident.lines.append(text)
        return ident


# --------------------------------------------------------------------------- #
# Steuergeraeteseite
# --------------------------------------------------------------------------- #
class ModuleFunctions:
    """Schnittstelle, die ein simuliertes Steuergeraet fuer KWP1281 bereitstellt."""

    def ident_blocks(self) -> list[bytes]:  # pragma: no cover - Schnittstelle
        raise NotImplementedError

    def coding(self) -> tuple[int, int]:  # pragma: no cover - Schnittstelle
        raise NotImplementedError

    def set_coding(self, coding: int, workshop_code: int) -> bool:  # pragma: no cover
        raise NotImplementedError

    def fault_codes(self) -> list[FaultEntry]:  # pragma: no cover
        raise NotImplementedError

    def clear_fault_codes(self) -> list[FaultEntry]:  # pragma: no cover
        raise NotImplementedError

    def group_read(self, group: int, basic_setting: bool = False):  # pragma: no cover
        raise NotImplementedError

    def adaptation_read(self, channel: int) -> int | None:  # pragma: no cover
        raise NotImplementedError

    def adaptation_save(self, channel: int, value: int, workshop_code: int) -> int | None:
        raise NotImplementedError  # pragma: no cover

    def actuator_test(self, value: int) -> bytes | None:  # pragma: no cover
        raise NotImplementedError

    def login(self, code: int) -> bool:  # pragma: no cover
        raise NotImplementedError


class Kwp1281EcuServer(KLineEcu):
    """Steuergeraeteseite: bedient den KWP1281-Dialog in einem eigenen Thread."""

    def __init__(
        self,
        name: str,
        bus: KLineBus,
        address: int,
        module: ModuleFunctions,
        *,
        log: Callable[[str], None] | None = None,
        keyword_version: int = 1281,
    ) -> None:
        super().__init__(name, bus, address)
        self.module = module
        self.keyword_version = keyword_version
        self.log = log or (lambda text: None)
        self.counter: int | None = None
        self._wakeup = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._pending_request: Block | None = None
        self.transcript: list[str] = []
        self.dialogs = 0
        self.logged_in = False

    # -- Lebenszyklus -------------------------------------------------------- #
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name=f"kwp1281-{self.address:02X}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.bus.detach(self)

    def wakeup_keywords(self, tester_address: int) -> tuple[int, int]:
        """Keywords der 5-Baud-Initialisierung.

        Version = ``((msb & 0x7F) << 7) + (lsb & 0x7F)``:

        * ``(0x01, 0x8A)``  -> KWP1281
        * ``(0x50, 0x8F)``  -> KWP2000 ueber Blockdialog
        """
        self.dialogs += 1
        self.counter = None
        self.logged_in = False
        if self.keyword_version >= 2000:
            return (0x50, 0x8F)
        return (0x01, 0x8A)

    def begin_session(self) -> None:
        self.listening = True
        self._wakeup.set()

    # -- Dialog ------------------------------------------------------------- #
    def _run(self) -> None:
        while self._running:
            if not self._wakeup.wait(0.2):
                continue
            self._wakeup.clear()
            self.flush()
            try:
                self._session()
            except TimeoutError:
                self.log("KWP1281: Dialog ohne Fortsetzung beendet")
            except Exception as exc:  # pragma: no cover - Simulation
                self.log(f"KWP1281: Fehler im Dialog: {exc}")
            finally:
                self.counter = None
                # Ohne adressierenden Tester hoert das Steuergeraet wieder auf
                # mitzuhoeren - es sei denn, ein naechster Tester wartet schon
                # (dann hat ``begin_session`` das Ereignis bereits gesetzt und
                # das Steuergeraet muss weiter zuhoeren).
                if not self._wakeup.is_set():
                    self.listening = False

    def _session(self) -> None:
        # Steuergeraet meldet sich nach dem Keyword-Austausch selbst mit Identbloecken
        items: list[tuple[int, bytes]] = [
            (BlockTitle.ASCII_DATA, body) for body in self.module.ident_blocks()
        ]
        coding, wsc = self.module.coding()
        doubled = coding * 2 + (1 if wsc > 0xFFFF else 0)
        items.append(
            (
                BlockTitle.ASCII_DATA,
                bytes(
                    [
                        0x00,
                        (doubled >> 8) & 0xFF,
                        doubled & 0xFF,
                        (wsc & 0xFFFF) >> 8,
                        wsc & 0xFF,
                    ]
                ),
            )
        )
        if not self.write_response(items):
            return

        while self._running:
            try:
                # VCDS haelt den Dialog mit regelmaessigem Verkehr offen; hier
                # bleibt er 30 s bestehen (danach hoert das SG wieder auf zu lauschen).
                request = self.read_block(timeout=30.0)
            except TimeoutError:
                return
            if request.title == BlockTitle.END:
                self.log("KWP1281: Dialog beendet (0x06)")
                return
            if request.is_ack_nak:
                continue
            self.handle(request)

    def write_response(self, items: list[tuple[int, bytes]]) -> bool:
        """Sendet Antwortbloecke; zwischen den Bloecken wird die Testerquittung gelesen.

        Der KWP1281-Blockzaehler laeuft ueber *beide* Richtungen. Deshalb muss
        das Steuergeraet die Quittung des Testers einlesen, bevor es den
        naechsten Block sendet (siehe ``docs/PROTOKOLLE.md``).
        """
        for title, body in items:
            self.write_block(title, body)
            if title in (BlockTitle.ACK, BlockTitle.NAK):
                return True
            try:
                ack = self.read_block(timeout=1.5)
            except TimeoutError:
                self.log("KWP1281: Quittung des Testers fehlt - Dialog abgebrochen")
                return False
            if ack.is_nak:
                self.log("KWP1281: Tester meldet NAK")
                return True
        self.write_block(BlockTitle.ACK)
        return True

    # -- Kommandobehandlung -------------------------------------------------- #
    def handle(self, request: Block) -> None:
        title = request.title
        body = request.body
        if title == BlockTitle.READ_IDENT:
            items = [(BlockTitle.ASCII_DATA, b) for b in self.module.ident_blocks()]
            self.write_response(items)
        elif title == BlockTitle.FAULT_CODES_READ:
            self.write_fault_codes(self.module.fault_codes())
        elif title == BlockTitle.FAULT_CODES_DELETE:
            self.write_fault_codes(self.module.clear_fault_codes())
        elif title in (BlockTitle.GROUP_READ, BlockTitle.BASIC_SETTING_READ):
            if not body:
                self.write_block(BlockTitle.NAK)
                return
            reading = self.module.group_read(
                body[0], basic_setting=title == BlockTitle.BASIC_SETTING_READ
            )
            if reading is None:
                self.write_block(BlockTitle.NAK)
                return
            payload = bytearray()
            for sensor_id, a, b in reading:
                payload += bytes([sensor_id, a, b])
            self.write_response([(BlockTitle.GROUP_READ_RESPONSE, bytes(payload))])
        elif title == BlockTitle.ADAPTATION_READ:
            if not body:
                self.write_block(BlockTitle.NAK)
                return
            value = self.module.adaptation_read(body[0])
            if value is None:
                self.write_block(BlockTitle.NAK)
                return
            self.write_response(
                [
                    (
                        BlockTitle.ADAPTATION_RESPONSE,
                        bytes([body[0], (value >> 8) & 0xFF, value & 0xFF]),
                    )
                ]
            )
        elif title == BlockTitle.ADAPTATION_TEST:
            if len(body) < 3:
                self.write_block(BlockTitle.NAK)
                return
            value = (body[1] << 8) | body[2]
            self.write_response(
                [
                    (
                        BlockTitle.ADAPTATION_RESPONSE,
                        bytes([body[0], (value >> 8) & 0xFF, value & 0xFF]),
                    )
                ]
            )
        elif title == BlockTitle.ADAPTATION_SAVE:
            if len(body) < 5:
                self.write_block(BlockTitle.NAK)
                return
            value = (body[1] << 8) | body[2]
            wsc = (body[3] << 8) | body[4]
            new_value = self.module.adaptation_save(body[0], value, wsc)
            if new_value is None:
                self.write_block(BlockTitle.NAK)
                return
            self.write_response(
                [
                    (
                        BlockTitle.ADAPTATION_RESPONSE,
                        bytes([body[0], (new_value >> 8) & 0xFF, new_value & 0xFF]),
                    )
                ]
            )
        elif title == BlockTitle.LOGIN:
            if len(body) < 2:
                self.write_block(BlockTitle.NAK)
                return
            code = (body[0] << 8) | body[1]
            self.logged_in = bool(self.module.login(code))
            self.write_block(BlockTitle.ACK if self.logged_in else BlockTitle.NAK)
        elif title == BlockTitle.SOFTWARE_CODING:
            if len(body) < 4:
                self.write_block(BlockTitle.NAK)
                return
            doubled = (body[0] << 8) | body[1]
            coding = doubled // 2
            wsc = (body[2] << 8) | body[3]
            if body[1] & 1:
                wsc += 65536
            ok = self.module.set_coding(coding, wsc)
            if not ok:
                self.write_block(BlockTitle.NAK)
                return
            items = [(BlockTitle.ASCII_DATA, b) for b in self.module.ident_blocks()]
            items.append(
                (
                    BlockTitle.ASCII_DATA,
                    bytes(
                        [
                            0x00,
                            (doubled >> 8) & 0xFF,
                            doubled & 0xFF,
                            (wsc & 0xFFFF) >> 8,
                            wsc & 0xFF,
                        ]
                    ),
                )
            )
            self.write_response(items)
        elif title == BlockTitle.ACTUATOR_TEST:
            if not body:
                self.write_block(BlockTitle.NAK)
                return
            response = self.module.actuator_test(body[0])
            if response is None:
                self.write_block(BlockTitle.NAK)
                return
            self.write_response([(BlockTitle.ACTUATOR_TEST_RESPONSE, response)])
        else:
            self.log(f"KWP1281: nicht unterstuetzter Blocktitel 0x{title:02X}")
            self.write_block(BlockTitle.NAK)

    def write_fault_codes(self, entries: list[FaultEntry]) -> None:
        payload = bytearray()
        for entry in entries:
            payload += bytes(
                [(entry.code >> 8) & 0xFF, entry.code & 0xFF, entry.status & 0xFF]
            )
        # Abschlusseintrag "keine weiteren Fehler" (0xFFFF 88)
        payload += bytes([0xFF, 0xFF, 0x88])
        self.write_response([(BlockTitle.FAULT_CODES_RESPONSE, bytes(payload))])

    # -- Blockebene ---------------------------------------------------------- #
    def write_block(self, title: int, body: Sequence[int] = ()) -> Block:
        if self.counter is None:
            self.counter = 0x01
        block = Block(counter=self.counter & 0xFF, title=int(title), body=bytes(body))
        self.counter = (self.counter + 1) & 0xFF
        raw = block.to_bytes()
        self.transcript.append(f"E {raw.hex(' ')}")
        for byte in raw:
            self.send_bytes(bytes([byte]))
            self._expect_tester_ack(byte)
            self.bus.clock.r6()
        return block

    def _expect_tester_ack(self, byte: int) -> None:
        try:
            self.expect_echo(byte, timeout=0.5)
        except (TimeoutError, IOError) as exc:  # pragma: no cover - Simulation
            self.log(f"KWP1281: Bytequittung fehlt: {exc}")

    def read_block(self, timeout: float = 1.0) -> Block:
        raw = bytearray()
        length = self._read_byte_acked(timeout)
        raw.append(length)
        if length < 3:
            raise Kwp1281Error(f"Ungueltige Blocklaenge {length}")
        for _ in range(length):
            raw.append(self._read_byte_acked(timeout))
        block = Block.from_bytes(bytes(raw))
        if self.counter is not None and block.counter != self.counter:
            self.log(f"KWP1281: Blockzaehler {block.counter:02X} statt {self.counter:02X}")
        self.counter = (block.counter + 1) & 0xFF
        self.transcript.append(f"T {raw.hex(' ')}")
        return block

    def _read_byte_acked(self, timeout: float) -> int:
        """Liest ein Byte des Testers; die Quittung erzeugt der Bus automatisch."""
        byte = self.read_byte(timeout=timeout)
        self.bus.clock.r6()
        return byte
