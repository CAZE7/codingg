"""UDS / ISO 14229 (Teilmenge).

Steuergeraete ab etwa 2005 (Golf V, Passat B6, Audi B8) werden mit UDS
diagnostiziert. VCDS nutzt dabei dieselben Grundfunktionen wie beim KWP2000,
allerdings mit UDS-Diensten und DTC-Statussatz:

===============  =========================================================
Dienst           Verwendung
===============  =========================================================
``0x10``         Diagnosesitzung (``0x01`` Standard, ``0x03`` erweitert)
``0x11``         Steuergerät zuruecksetzen
``0x14``         Fehlerspeicher loeschen (DTC-Gruppe)
``0x19``         Fehlerspeicher lesen (Subfunktion ``0x02``/``0x0A``)
``0x22``         Datenwert lesen (z. B. ``0xF190`` = Fahrgestellnummer)
``0x23``         Speicher lesen (Adresse/Laenge, herstellerabhaengig)
``0x27``         Sicherheitszugriff
``0x28``         Kommunikation steuern
``0x2E``         Datenwert schreiben
``0x2F``         Ein-/Ausgang steuern (IOControl)
``0x31``         Routine starten/stoppen/ergebnis
``0x3E``         TesterPresent
===============  =========================================================

DTC-Statussatz: 1 Byte ``DTCStatusByte`` (testFailed, confirmedDTC,
testNotCompletedSinceLastClear, warningIndicatorRequested ...) sowie
Serverspezifische Verfuegbarkeitsanzeigen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from ..obd.dtc import decode_uds_dtc
from .kwp2000 import Kwp2000Error, Kwp2000NegativeResponse, response_code_text

__all__ = [
    "UdsClient",
    "UdsSession",
    "UdsError",
    "UdsNegativeResponse",
    "DTC_STATUS_BITS",
    "DTC_STATUS_BITS_DE",
    "decode_status_byte",
    "decode_status_byte_de",
    "status_summary_de",
]

UdsError = Kwp2000Error
UdsNegativeResponse = Kwp2000NegativeResponse

#: Bedeutung der Bits im UDS-DTC-Statussatz (ISO 14229-1)
DTC_STATUS_BITS = {
    0: "testFailed",
    1: "testFailedThisOperationCycle",
    2: "pendingDTC",
    3: "confirmedDTC",
    4: "testNotCompletedSinceLastClear",
    5: "testFailedSinceLastClear",
    6: "testNotCompletedThisOperationCycle",
    7: "warningIndicatorRequested",
}


#: Deutsche Bezeichnungen der Statusbits (Anzeige in der Oberflaeche)
DTC_STATUS_BITS_DE = {
    0: "Fehler aktiv",
    1: "Fehler in diesem Fahrzyklus",
    2: "ausstehend",
    3: "bestätigt",
    4: "Test seit Löschen nicht gelaufen",
    5: "Fehler seit Löschen aufgetreten",
    6: "Test in diesem Zyklus nicht gelaufen",
    7: "Warnleuchte angefordert",
}


def decode_status_byte(status: int) -> list[str]:
    """Liefert die gesetzten Statusbits als Textliste."""
    return [name for bit, name in DTC_STATUS_BITS.items() if status & (1 << bit)]


def decode_status_byte_de(status: int) -> list[str]:
    """Statusbits auf Deutsch (Reihenfolge: Bit 0 bis Bit 7)."""
    return [name for bit, name in DTC_STATUS_BITS_DE.items() if status & (1 << bit)]


def status_summary_de(status: int) -> str:
    """Kurze Einordnung eines UDS-Fehlerstatus wie in VCDS (statisch/sporadisch)."""
    if status & (1 << 0) and status & (1 << 3):
        return "statisch"
    if status & (1 << 0):
        return "sporadisch"
    if status & (1 << 2):
        return "ausstehend"
    return "gespeichert"


class Transport(Protocol):
    def request(self, payload: bytes, timeout: float | None = None) -> bytes:  # pragma: no cover
        ...


@dataclass
class UdsSession:
    """UDS-Sitzung inklusive Sitzungs- und Sicherheitszustand."""

    transport: Transport
    address: int
    timeout: float = 1.0
    session_type: int = 0x01
    security_level: int | None = None
    stats: dict = field(default_factory=lambda: {"requests": 0, "errors": 0})
    last_activity: float = field(default_factory=time.time)

    def raw(self, payload: bytes, timeout: float | None = None) -> bytes:
        timeout = self.timeout if timeout is None else timeout
        self.stats["requests"] += 1
        response = self.transport.request(bytes(payload), timeout=timeout)
        self.last_activity = time.time()
        if not response:
            raise UdsError("Leere Antwort vom Steuergeraet")
        if response[0] == 0x7F:
            self.stats["errors"] += 1
            raise UdsNegativeResponse(payload[0], response[2] if len(response) > 2 else 0x10)
        if response[0] != (payload[0] + 0x40):
            self.stats["errors"] += 1
            raise UdsError(
                f"Unerwarteter Antwortdienst 0x{response[0]:02X} auf 0x{payload[0]:02X}"
            )
        return response[1:]

    # -- Sitzung ------------------------------------------------------------- #
    def start_session(self, session_type: int = 0x01) -> bytes:
        self.session_type = session_type
        return self.raw(bytes([0x10, session_type]))

    def tester_present(self) -> bytes:
        return self.raw(bytes([0x3E, 0x00]))

    def ecu_reset(self, kind: int = 0x01) -> bytes:
        return self.raw(bytes([0x11, kind]))

    # -- Fehlerspeicher ------------------------------------------------------ #
    def read_dtcs(self, *, snapshot: bool = False) -> list[dict]:
        """Fehlerspeicher lesen (``19 02`` = DTCs nach Status, ``19 0A`` = alle)."""
        subfunction = 0x0A if snapshot else 0x02
        data = self.raw(bytes([0x19, subfunction, 0xFF]))
        # Antwort: <Subfunktion> <Verfuegbarkeitsmaske> <DTC(3B) Status(1B)>...
        status_availability = data[1] if len(data) > 1 else 0xFF
        payload = data[2:]
        dtcs: list[dict] = []
        offset = 0
        while offset + 3 <= len(payload):
            dtc_bytes = payload[offset : offset + 3]
            status = payload[offset + 3] if offset + 3 < len(payload) else 0
            decoded = decode_uds_dtc(bytes(dtc_bytes))
            decoded["status"] = status
            decoded["status_text"] = f"0x{status:02X}"
            decoded["status_bits"] = decode_status_byte(status & status_availability)
            dtcs.append(decoded)
            offset += 4
        return dtcs

    def clear_dtcs(self, group: int = 0xFFFFFF) -> bytes:
        return self.raw(
            bytes([0x14, (group >> 16) & 0xFF, (group >> 8) & 0xFF, group & 0xFF])
        )

    # -- Daten --------------------------------------------------------------- #
    def read_data(self, identifier: int) -> bytes:
        data = self.raw(bytes([0x22, (identifier >> 8) & 0xFF, identifier & 0xFF]))
        return data[2:]

    def write_data(self, identifier: int, value: bytes) -> bytes:
        payload = bytes([0x2E, (identifier >> 8) & 0xFF, identifier & 0xFF]) + bytes(value)
        return self.raw(payload)

    def vin(self) -> str:
        try:
            return self.read_data(0xF190).decode("ascii", errors="replace")
        except UdsError:
            return ""

    # -- Messwerte / Stellglieder ------------------------------------------- #
    def measuring_block(self, group: int) -> bytes:
        """Messwerteblock (VAG-Konvention ueber ``22 F4 xx``)."""
        return self.read_data(0xF400 | (group & 0xFF))

    def io_control(self, identifier: int, control: int = 0x03, value: bytes = b"") -> bytes:
        payload = bytes(
            [0x2F, (identifier >> 8) & 0xFF, identifier & 0xFF, control]
        ) + bytes(value)
        return self.raw(payload)

    def routine_control(self, routine: int, kind: int = 0x01, data: bytes = b"") -> bytes:
        payload = (
            bytes([0x31, kind, (routine >> 16) & 0xFF, (routine >> 8) & 0xFF, routine & 0xFF])
            + bytes(data)
        )
        return self.raw(payload)

    def security_access(self, level: int, key: bytes) -> bytes:
        self.raw(bytes([0x27, level & 0xFF]))
        response = self.raw(bytes([0x27, (level + 1) & 0xFF]) + bytes(key))
        self.security_level = level
        return response


class UdsClient:
    """Bequemer Zugriff auf ein UDS-Steuergeraet (VAG-Variante)."""

    def __init__(self, transport: Transport, address: int, *, timeout: float = 1.0) -> None:
        self.session = UdsSession(transport=transport, address=address, timeout=timeout)
        self.address = address
        self.log: list[str] = []

    def start(self) -> None:
        try:
            self.session.start_session(0x03)
        except UdsError as exc:
            self.log.append(f"Erweiterte Sitzung nicht moeglich: {exc}")
            try:
                self.session.start_session(0x01)
            except UdsError as exc2:
                self.log.append(f"Sitzungsstart fehlgeschlagen: {exc2}")

    def stop(self) -> None:
        """Verabschiedet sich (TesterPresent). Das darf nie stoeren."""
        try:
            self.session.tester_present()
        except (Exception,):  # noqa: BLE001 - Abschied ist optional
            pass

    def ident_text(self) -> str:
        fields = {
            0xF190: "Fahrgestellnummer",
            0xF187: "Teilenummer",
            0xF188: "Softwareversion",
            0xF189: "Softwareteilenummer",
            0xF18A: "Herstellerkennung",
            0xF191: "Hardwareteilenummer",
            0xF19E: "Seriennummer",
        }
        lines: list[str] = []
        for identifier, name in fields.items():
            try:
                value = self.session.read_data(identifier)
            except UdsError:
                continue
            text = value.decode("latin-1", errors="replace").strip("\x00 \r\n")
            if text:
                lines.append(f"{name}: {text}")
        return "\n".join(lines)

    def read_faults(self) -> list[dict]:
        return self.session.read_dtcs()

    def clear_faults(self) -> None:
        self.session.clear_dtcs()

    def measuring_block(self, group: int) -> bytes:
        return self.session.measuring_block(group)

    def coding(self) -> bytes:
        return self.session.read_data(0xF1A0)

    def set_coding(self, value: bytes) -> bytes:
        return self.session.write_data(0xF1A0, value)

    def adaptation(self, channel: int) -> bytes:
        return self.session.read_data(0xF500 | (channel & 0xFF))

    def set_adaptation(self, channel: int, value: int) -> bytes:
        return self.session.write_data(
            0xF500 | (channel & 0xFF), value.to_bytes(2, "big")
        )

    def actuator(self, identifier: int, value: int) -> bytes:
        return self.session.io_control(
            identifier, 0x03, bytes([value & 0xFF])
        )

    def login(self, level: int, key: bytes) -> bytes:
        return self.session.security_access(level, key)
