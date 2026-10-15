"""KWP2000 / ISO 14230 inklusive der VAG-Dienstvariante.

Ueber der Transportschicht (ISO-TP oder VW TP 2.0) werden die Diagnosedienste
abgebildet, die VCDS bei Steuergeraeten ab etwa Modelljahr 2000 verwendet:

===============  =========================================================
Dienst           Verwendung in VCDS
===============  =========================================================
``0x10``         Diagnosesitzung starten
``0x11``         Steuergerät zuruecksetzen
``0x14``         Fehlerspeicher loeschen
``0x18``         Fehlerspeicher lesen (byStatus)
``0x1A``         Steuergeraete-Identifikation (Kennfelder 0x80 .. 0x9F)
``0x21``         Messwerteblock lesen (readDataByLocalIdentifier)
``0x22``         Datenwert lesen (readDataByIdentifier)
``0x27``         Sicherheitszugriff (Login)
``0x2E``         Datenwert schreiben
``0x30``         Stellglied ansteuern (inputOutputControlByLocalIdentifier)
``0x31``         Routine starten/stoppen
``0x3B``         Codierung schreiben (writeDataByLocalIdentifier)
``0x3E``         TesterPresent
===============  =========================================================

Die Nutzlast des Messwerteblocks entspricht der KWP1281-Kodierung
(3 Bytes je Messwert: ``Sensortyp, A, B``); das ist die in der Community
verbreitete Darstellung, die auch VCDS-kompatible Label-Dateien voraussetzen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from ..obd.blocks import format_measurement, parse_measurement_triples
from ..obd.dtc import DtcInfo, decode_sae16, status_text
from .kw1281 import Block, FaultEntry

__all__ = [
    "Kwp2000Error",
    "Kwp2000NegativeResponse",
    "Transport",
    "Kwp2000Client",
    "DiagnosticSession",
    "NRC_TEXT",
    "response_code_text",
]

NRC_TEXT = {
    0x10: "Allgemeine Ablehnung",
    0x11: "Dienst nicht unterstuetzt",
    0x12: "Subfunktion nicht unterstuetzt",
    0x13: "Falsche Laenge oder Format",
    0x21: "Steuergeraet beschaeftigt",
    0x22: "Bedingungen nicht erfuellt",
    0x23: "Anfrage ausserhalb des Bereichs",
    0x24: "Anfragefolge fehlerhaft",
    0x31: "Anfrage ausserhalb des Bereichs",
    0x33: "Sicherheitszugriff verweigert",
    0x35: "Ungueltiger Schluessel",
    0x36: "Anzahl Versuche ueberschritten",
    0x37: "Zeitverzoegerung aktiv",
    0x78: "Antwort ausstehend",
    0x7E: "Subfunktion in aktueller Sitzung nicht unterstuetzt",
    0x7F: "Dienst in aktueller Sitzung nicht unterstuetzt",
}


def response_code_text(code: int) -> str:
    return NRC_TEXT.get(code, f"Unbekannter Antwortcode 0x{code:02X}")


class Kwp2000Error(Exception):
    """Allgemeiner KWP2000-Fehler."""


class Kwp2000NegativeResponse(Kwp2000Error):
    """Steuergeraet hat mit 0x7F (negativer Antwort) geantwortet."""

    def __init__(self, service: int, code: int) -> None:
        super().__init__(
            f"Dienst 0x{service:02X} abgelehnt: 0x{code:02X} ({response_code_text(code)})"
        )
        self.service = service
        self.code = code


class Transport(Protocol):
    """Schnittstelle der Transportschicht (ISO-TP oder VW TP 2.0)."""

    def request(self, payload: bytes, timeout: float | None = None) -> bytes:  # pragma: no cover
        ...


@dataclass
class DiagnosticSession:
    """KWP2000-Sitzung: haelt Dienstzustand und Sitzungsverwaltung."""

    transport: Transport
    address: int
    protocol: str = "KWP2000"
    timeout: float = 1.0
    session_type: int = 0x89  # VAG-Diagnosesitzung
    last_activity: float = field(default_factory=time.time)
    stats: dict = field(default_factory=lambda: {"requests": 0, "errors": 0})

    # ------------------------------------------------------------------ #
    # Basis
    # ------------------------------------------------------------------ #
    def raw(self, payload: bytes, timeout: float | None = None) -> bytes:
        """Schickt einen Rohauftrag und prueft auf negative Antworten."""
        timeout = self.timeout if timeout is None else timeout
        self.stats["requests"] += 1
        response = self.transport.request(bytes(payload), timeout=timeout)
        self.last_activity = time.time()
        if not response:
            raise Kwp2000Error("Leere Antwort vom Steuergeraet")
        if response[0] == 0x7F:
            self.stats["errors"] += 1
            raise Kwp2000NegativeResponse(payload[0], response[2] if len(response) > 2 else 0x10)
        if response[0] != (payload[0] + 0x40):
            self.stats["errors"] += 1
            raise Kwp2000Error(
                f"Unerwarteter Antwortdienst 0x{response[0]:02X} auf 0x{payload[0]:02X}"
            )
        return response[1:]

    # ------------------------------------------------------------------ #
    # Dienste
    # ------------------------------------------------------------------ #
    def start_session(self, session_type: int | None = None) -> bytes:
        session = self.session_type if session_type is None else session_type
        return self.raw(bytes([0x10, session]))

    def tester_present(self) -> bytes:
        return self.raw(bytes([0x3E, 0x01]))

    def read_ident(self) -> dict:
        """Liest die VAG-Identifikationskennfelder (0x1A 0x80 .. 0x9F)."""
        ident: dict[str, str] = {}
        for index in range(0x80, 0x97):
            try:
                data = self.raw(bytes([0x1A, index]))
            except Kwp2000Error:
                continue
            text = data[1:].decode("latin-1", errors="replace").strip("\x00 \r\n")
            if text:
                ident[f"0x{index:02X}"] = text
        return ident

    def read_dtcs(self) -> list[DtcInfo]:
        """Fehlerspeicher lesen: ``18 02 FF 00``; Antwort: DTC (2 B) + Status (1 B)."""
        data = self.raw(bytes([0x18, 0x02, 0xFF, 0x00]))
        # Erste zwei Bytes: Anzahl der Fehlersaetze (Herstellerabhaengig)
        payload = data[2:] if len(data) >= 2 and data[0] == 0x01 else data
        entries: list[DtcInfo] = []
        for index in range(0, len(payload) - 2, 3):
            code = (payload[index] << 8) | payload[index + 1]
            status = payload[index + 2]
            if code == 0:
                continue
            entries.append(
                DtcInfo(
                    vag_code=None,
                    sae_code=decode_sae16(code),
                    status=status,
                    source="KWP2000",
                )
            )
        return entries

    def clear_dtcs(self) -> None:
        self.raw(bytes([0x14, 0xFF, 0x00]))

    def read_measuring_block(self, group: int) -> list:
        """Messwerteblock lesen (Dienst ``0x21``, Blocknummer als lokale Kennung)."""
        data = self.raw(bytes([0x21, group & 0xFF]))
        return [format_measurement(*triple) for triple in parse_measurement_triples(data[1:])]

    def read_data_by_identifier(self, identifier: int) -> bytes:
        data = self.raw(bytes([0x22, (identifier >> 8) & 0xFF, identifier & 0xFF]))
        return data[2:]

    def read_coding(self) -> bytes:
        """Codierung lesen (Kennung ``0xF1`` / VAG-Konvention)."""
        return self.read_data_by_identifier(0xF1)

    def write_coding(self, coding: str | bytes) -> bytes:
        """Codierung schreiben: ``3B 00`` + Codingstring + Werkstattcode."""
        payload = coding.encode("ascii") if isinstance(coding, str) else bytes(coding)
        return self.raw(bytes([0x3B, 0x00]) + payload)

    def adaptation_read(self, channel: int) -> bytes:
        """Anpassungskanal lesen: ``31 00 <Kanal>`` (VAG-Erweiterung von 0x31)."""
        return self.raw(bytes([0x31, 0x00, channel & 0xFF]))

    def adaptation_write(self, channel: int, value: int) -> bytes:
        """Anpassungskanal speichern: ``31 01 <Kanal> <Wert hi> <Wert lo>``."""
        return self.raw(
            bytes([0x31, 0x01, channel & 0xFF, (value >> 8) & 0xFF, value & 0xFF])
        )

    def output_control(self, local_id: int, value: int) -> bytes:
        return self.raw(bytes([0x30, local_id & 0xFF, 0x03, value & 0xFF]))

    def actuator_switch(self, actuator_id: int, on: bool = True) -> bytes:
        """Stellglied ansteuern/abschalten (InputOutputControlByLocalIdentifier).

        Steuerparameter ``0x01`` = ansteuern, ``0x00`` = abschalten; die
        Stellglied-ID wird als LocalIdentifier und als Wert gefuehrt.
        """
        actuator_id &= 0xFF
        if on:
            return self.raw(bytes([0x30, actuator_id, 0x01, actuator_id]))
        return self.raw(bytes([0x30, actuator_id, 0x00, 0x00]))

    def basic_setting_switch(self, group: int, on: bool = True) -> bytes:
        """Grundeinstellung ein-/ausschalten (Routine ``FC<Gruppe>``)."""
        return self.raw(bytes([0x31, 0x01 if on else 0x02, 0xFC, group & 0xFF]))

    def login(self, key: int) -> bytes:
        return self.raw(bytes([0x27, 0x01, (key >> 8) & 0xFF, key & 0xFF]))

    def adaptation_value(self, channel: int) -> int:
        """Anpassungskanal lesen und den Wert aus der Antwort holen.

        Antwort: ``71 <Kanal> <Wert hi> <Wert lo>`` - das Kanalbyte wird
        mitgesendet und hier verworfen.
        """
        data = self.adaptation_read(channel)
        if len(data) >= 3:
            return int.from_bytes(data[1:3], "big")
        return int.from_bytes(data[:2], "big") if len(data) >= 2 else 0

    def routine_control(self, routine: int, start: bool = True) -> bytes:
        """Routine ueber ``31 01 <Routine hi> <Routine lo>`` starten/stoppen.

        Der kurze Aufruf ist vom Speichern eines Anpassungskanals
        (``31 01 <Kanal> <Wert hi> <Wert lo>``) durch die Laenge unterschieden.
        """
        return self.raw(bytes([0x31, 0x01 if start else 0x02, (routine >> 8) & 0xFF, routine & 0xFF]))


#: Deutsche Namen der VAG-Identifikationskennfelder (Dienste 1A 80..88)
IDENT_NAMES = {
    0x80: "Teilenummer",
    0x81: "Bauteil",
    0x82: "Softwareversion",
    0x83: "Hardwareversion",
    0x84: "Seriennummer",
    0x85: "Hersteller",
    0x86: "Codierung",
    0x87: "Werkstattcode",
    0x88: "Herstelldatum",
    0x89: "Fahrgestellnummer",
    0x90: "Kennfeld 0x90",
    0x91: "Kennfeld 0x91",
    0x92: "Kennfeld 0x92",
}


class Kwp2000Client:
    """Bequemer Zugriff auf ein KWP2000-Steuergeraet (VAG-Variante)."""

    def __init__(self, transport: Transport, address: int, *, timeout: float = 1.0) -> None:
        self.session = DiagnosticSession(transport=transport, address=address, timeout=timeout)
        self.address = address
        self.log: list[str] = []

    # -- Kennung ------------------------------------------------------------- #
    def start(self) -> None:
        try:
            self.session.start_session()
        except Kwp2000Error as exc:
            self.log.append(f"Sitzungsstart fehlgeschlagen: {exc}")

    def stop(self) -> None:
        """Verabschiedet sich (TesterPresent). Das darf nie stoeren."""
        try:
            self.session.tester_present()
        except (Exception,):  # noqa: BLE001 - Abschied ist optional
            pass

    def ident_text(self) -> str:
        """Identifikation als VCDS-aehnlicher Text (Kennfelder auf Deutsch)."""
        ident = self.session.read_ident()
        lines = []
        for key, value in ident.items():
            try:
                index = int(key, 16)
            except ValueError:  # pragma: no cover - defensiv
                index = -1
            lines.append(f"{IDENT_NAMES.get(index, key)}: {value}")
        return "\n".join(lines)

    def read_faults(self) -> list[DtcInfo]:
        return self.session.read_dtcs()

    def clear_faults(self) -> None:
        self.session.clear_dtcs()

    def measuring_block(self, group: int):
        return self.session.read_measuring_block(group)

    def actuator_switch(self, actuator_id: int, on: bool = True) -> bytes:
        """Stellglied ansteuern/abschalten (Stellglieddiagnose)."""
        return self.session.actuator_switch(actuator_id, on)

    def basic_setting_switch(self, group: int, on: bool = True) -> bytes:
        """Grundeinstellung starten/beenden."""
        return self.session.basic_setting_switch(group, on)

    def coding(self) -> str:
        return self.session.read_coding().decode("latin-1", errors="replace")

    def set_coding(self, coding: str) -> bytes:
        return self.session.write_coding(coding)

    def adaptation(self, channel: int) -> bytes:
        return self.session.adaptation_read(channel)

    def adaptation_value(self, channel: int) -> int:
        """Anpassungskanal lesen; liefert direkt den 16-Bit-Wert."""
        return self.session.adaptation_value(channel)

    def set_adaptation(self, channel: int, value: int) -> bytes:
        return self.session.adaptation_write(channel, value)

    def actuator(self, local_id: int, value: int) -> bytes:
        return self.session.output_control(local_id, value)

    def login(self, key: int) -> bytes:
        return self.session.login(key)
