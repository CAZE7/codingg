"""Simuliertes Steuergeraet.

Ein :class:`SimulatedEcu` verbindet die fachliche Ebene
(:class:`SimulatedModule` - Messwerte, Fehlerspeicher, Codierung, Anpassung) mit
der jeweiligen Protokoll-/Busebene:

* **K-Line / KWP1281** - :class:`~vagdiag.proto.kw1281.Kwp1281EcuServer`
* **CAN / VW TP 2.0** - :class:`~vagdiag.proto.vwtp.VwtpEcuServer` inkl. CAN-Knoten
* **CAN / ISO-TP** - :class:`~vagdiag.proto.isotp.IsoTpEndpoint`

Die Nutzdatenschnittstelle ist in allen Faellen identisch, sodass dieselbe
Fachlogik KWP1281-, KWP2000- und UDS-Auftraege beantwortet.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .. import address_wire
from ..db import Database, ModuleInfo
from ..obd.dtc import decode_sae16, encode_sae16
from ..proto.bus import CanFrame, CanNode, KLineBus, SimClock, VirtualCanBus
from ..proto.isotp import IsoTpEndpoint
from ..proto.kw1281 import FaultEntry, Kwp1281EcuServer
from ..proto.vwtp import VwtpEcuServer
from .profile import VehicleState
from .roles import Group, ROLES, fault_set, role_for

__all__ = ["SimulatedModule", "SimulatedEcu", "CanEcuNode"]

#: Zuordnung der VAG-Kennfelder fuer die KWP2000-Identifikation (Simulationskonvention)
IDENT_LIDS = {
    0x80: "part_number",
    0x81: "component",
    0x82: "software",
    0x83: "hardware",
    0x84: "serial",
    0x85: "manufacturer",
    0x86: "coding",
    0x87: "workshop_code",
    0x88: "production_date",
}


class SimulatedModule:
    """Fachliche Ebene eines simulierten Steuergeraets."""

    def __init__(
        self,
        module: ModuleInfo,
        state: VehicleState,
        *,
        db: Database | None = None,
        log: Callable[[str], None] | None = None,
        vin: str | None = None,
    ) -> None:
        self.info = module
        self.state = state
        self.db = db
        self.log = log or (lambda text: "")
        self.role = role_for(module.address, module.dtc_set)
        self.groups: dict[int, Group] = {group.group: group for group in ROLES[self.role]}
        self.coding_value = int(module.coding or 0) if str(module.coding or "0").isdigit() else 0
        self.wsc = int(module.wsc or 0) if str(module.wsc or "0").isdigit() else 0
        self.faults: list[tuple[int | str, int]] = fault_set(module.dtc_set)
        self.adaptations: dict[int, int] = {}
        self.logged_in = False
        self.actuator_value: int | None = None
        self.routines: dict[int, str] = {}
        # Fahrgestellnummer: vom Fahrzeug uebernommen, sonst aus dem Modell abgeleitet
        from .vehicles import build_vin  # lokaler Import vermeidet Zyklen

        self.vin = vin or build_vin(getattr(module, "vehicle", "") or "")
        # Startwerte: der hinterlegte Bereich gibt den plausiblen Wert vor
        # (Rohwerte wie 32768 fuer den Foerderbeginn, aber z. B. 800 1/min
        # fuer die Leerlaufdrehzahl des Golf V).
        for entry in (db.adaptation_channels(module) if db is not None else []):
            channel = int(entry["channel"])
            low = entry.get("min_value")
            high = entry.get("max_value")
            if low is None:
                self.adaptations[channel] = 32768
            elif high is None or high <= low:
                self.adaptations[channel] = int(low)
            else:
                self.adaptations[channel] = int(low + (high - low) * 0.1)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Stammdaten
    # ------------------------------------------------------------------ #
    def ident_blocks(self) -> list[bytes]:
        """Identifikationsbloecke im VCDS-Format (Teilenummer/Bauteil)."""
        part = self.info.part_number or "000000000"
        component = self.info.component or "Steuergerät"
        software = self.info.software or "-"
        lines = [f"{part:<12}{component}  {software}"]
        return [line.encode("latin-1", errors="replace")[:90] for line in lines]

    def ident_values(self) -> dict[str, str]:
        return {
            "part_number": self.info.part_number,
            "component": self.info.component,
            "software": self.info.software,
            "hardware": self.info.part_number[:2] + self.info.part_number[-2:] + "H",
            "serial": f"{abs(hash(self.info.part_number)) % 10**8:08d}",
            "manufacturer": "VAGDiag Simulation",
            "coding": f"{self.coding_value:05d}",
            "workshop_code": f"{self.wsc:05d}",
            "production_date": time.strftime("%d.%m.%Y"),
            "vin": self.vin,
        }

    def coding(self) -> tuple[int, int]:
        return (self.coding_value, self.wsc)

    def set_coding(self, coding: int, workshop_code: int) -> bool:
        with self._lock:
            self.log(f"{self.info.name}: Codierung {coding:05d} (vorher {self.coding_value:05d})")
            self.coding_value = int(coding)
            self.wsc = int(workshop_code)
        return True

    # ------------------------------------------------------------------ #
    # Fehlerspeicher
    # ------------------------------------------------------------------ #
    def fault_codes(self) -> list[FaultEntry]:
        entries: list[FaultEntry] = []
        for code, status in self.faults:
            if isinstance(code, int):
                entries.append(FaultEntry(code, status))
            else:
                entries.append(FaultEntry(encode_sae16(str(code)), status))
        return entries

    def clear_fault_codes(self) -> list[FaultEntry]:
        with self._lock:
            self.log(f"{self.info.name}: Fehlerspeicher geloescht ({len(self.faults)} Einträge)")
            self.faults = []
        return []

    # ------------------------------------------------------------------ #
    # Messwerte
    # ------------------------------------------------------------------ #
    def group_read(self, group: int, basic_setting: bool = False):
        definition = self.groups.get(group)
        if definition is None:
            return None
        return [field.provider(self.state) for field in definition.fields]

    def group_titles(self) -> dict[int, str]:
        return {group: definition.title for group, definition in self.groups.items()}

    # ------------------------------------------------------------------ #
    # Anpassung / Stellglieder / Login
    # ------------------------------------------------------------------ #
    def adaptation_read(self, channel: int) -> int | None:
        return self.adaptations.get(channel)

    def adaptation_save(self, channel: int, value: int, workshop_code: int) -> int | None:
        with self._lock:
            if channel not in self.adaptations:
                self.adaptations[channel] = value
            else:
                self.adaptations[channel] = value
            self.log(f"{self.info.name}: Kanal {channel:03d} auf {value} gesetzt")
            return self.adaptations[channel]

    def actuator_test(self, value: int) -> bytes | None:
        with self._lock:
            self.actuator_value = value
        return bytes([value, 0x00])

    def login(self, code: int) -> bool:
        with self._lock:
            self.logged_in = code == 12233 or code == self.coding_value
        return self.logged_in

    # ------------------------------------------------------------------ #
    # KWP2000 (ISO 14230)
    # ------------------------------------------------------------------ #
    def handle_kwp2000(self, payload: bytes) -> bytes | None:
        if not payload:
            return None
        service = payload[0]
        body = payload[1:]
        if service == 0x10:  # StartDiagnosticSession
            return bytes([0x50, body[0] if body else 0x89])
        if service == 0x11:  # ECUReset
            return bytes([0x51, body[0] if body else 0x01])
        if service == 0x1A:  # ReadEcuIdentification
            key = IDENT_LIDS.get(body[0] if body else 0x80)
            if key is None:
                return bytes([0x7F, service, 0x31])
            return bytes([0x5A, 0x01]) + self.ident_values()[key].encode("latin-1") + b"\x00"
        if service == 0x18:  # ReadDTCByStatus
            return self._kwp2000_dtcs()
        if service == 0x14:  # ClearDiagnosticInformation
            self.clear_fault_codes()
            return bytes([0x54])
        if service == 0x21:  # ReadDataByLocalIdentifier (Messwerteblock)
            group = body[0] if body else 0
            values = self.group_read(group)
            if values is None:
                return bytes([0x7F, service, 0x31])
            data = bytearray([0x61, group])
            for sensor_id, a, b in values:
                data += bytes([sensor_id, a, b])
            return bytes(data)
        if service == 0x22:  # ReadDataByIdentifier
            identifier = (body[0] << 8) | body[1] if len(body) >= 2 else 0
            value = self._read_identifier(identifier)
            if value is None:
                return bytes([0x7F, service, 0x31])
            return bytes([0x62, body[0], body[1]]) + value
        if service == 0x2E:  # WriteDataByIdentifier
            return self._write_identifier(body, service, kwp=True)
        if service == 0x3B:  # WriteDataByLocalIdentifier (Codierung)
            coding_text = body[1:].decode("latin-1", errors="replace").strip("\x00 ")
            digits = "".join(ch for ch in coding_text[:5] if ch.isdigit()) or "0"
            self.set_coding(int(digits), self.wsc)
            return bytes([0x7B, body[0] if body else 0x00]) + bytes([0x01])
        if service == 0x30:  # InputOutputControlByLocalIdentifier
            if len(body) < 3:
                return bytes([0x7F, service, 0x13])
            self.actuator_test(body[2])
            return bytes([0x70, body[0], body[1], body[2]])
        if service == 0x31:  # Anpassung lesen/speichern (VAG auf StartRoutine)
            # Leitungsformat (siehe docs/PROTOKOLLE.md):
            #   31 00 <Kanal>               Anpassung lesen
            #   31 02 <Kanal>               Anpassung pruefen (nur lesen)
            #   31 01 <Kanal> <Wert hi/lo>  Anpassung speichern
            if not body:
                return bytes([0x7F, service, 0x31])
            kind = body[0]
            if kind in (0x00, 0x02):
                if len(body) < 2:
                    return bytes([0x7F, service, 0x13])
                channel = body[1]
                value = self.adaptation_read(channel)
                if value is None:
                    return bytes([0x7F, service, 0x31])
                return bytes([0x71, channel, (value >> 8) & 0xFF, value & 0xFF])
            if kind == 0x01:
                if len(body) == 3:  # Routine starten/abfragen (hier nur vermerkt)
                    routine = (body[1] << 8) | body[2]
                    self.routines[routine] = "gestartet"
                    return bytes([0x71, body[1], body[2]])
                if len(body) >= 4:
                    channel = body[1]
                    value = (body[2] << 8) | body[3]
                    new_value = self.adaptation_save(channel, value, self.wsc)
                    if new_value is None:
                        return bytes([0x7F, service, 0x31])
                    return bytes([0x71, channel, (new_value >> 8) & 0xFF, new_value & 0xFF])
            return bytes([0x7F, service, 0x31])
        if service == 0x27:  # SecurityAccess
            if len(body) >= 3:
                key = (body[1] << 8) | body[2]
                if self.login(key):
                    return bytes([0x67, body[0]])
                return bytes([0x7F, service, 0x35])
            return bytes([0x67, body[0] if body else 0x01, 0x12, 0x34])
        if service == 0x3E:  # TesterPresent
            return bytes([0x7E, body[0] if body else 0x00])
        return bytes([0x7F, service, 0x11])

    def _kwp2000_dtcs(self) -> bytes:
        entries = self.fault_codes()
        data = bytearray([0x58, 0x01, len(entries) & 0xFF])
        for entry in entries:
            data += bytes([(entry.code >> 8) & 0xFF, entry.code & 0xFF, entry.status & 0xFF])
        return bytes(data)

    # ------------------------------------------------------------------ #
    # UDS (ISO 14229)
    # ------------------------------------------------------------------ #
    def handle_uds(self, payload: bytes) -> bytes | None:
        if not payload:
            return None
        service = payload[0]
        body = payload[1:]
        if service == 0x10:
            return bytes([0x50, body[0] if body else 0x01, 0x00, 0x32, 0x01, 0xF4])
        if service == 0x11:
            return bytes([0x51, body[0] if body else 0x01])
        if service == 0x19:
            subfunction = body[0] if body else 0x02
            if subfunction not in (0x02, 0x0A):
                return bytes([0x7F, service, 0x12])
            return self._uds_dtcs()
        if service == 0x14:
            self.clear_fault_codes()
            return bytes([0x54])
        if service == 0x22:
            if len(body) < 2:
                return bytes([0x7F, service, 0x13])
            identifier = (body[0] << 8) | body[1]
            value = self._read_identifier(identifier)
            if value is None:
                return bytes([0x7F, service, 0x31])
            return bytes([0x62, body[0], body[1]]) + value
        if service == 0x2E:
            return self._write_identifier(body, service, kwp=False)
        if service == 0x2F:
            if len(body) < 4:
                return bytes([0x7F, service, 0x13])
            identifier = (body[0] << 8) | body[1]
            self.actuator_test(body[3])
            return bytes([0x6F, body[0], body[1], body[2], body[3]])
        if service == 0x31:
            if len(body) < 4:
                return bytes([0x7F, service, 0x13])
            routine = (body[1] << 8) | body[2]
            self.routines[routine] = "gestartet" if body[0] == 0x01 else "gestoppt"
            return bytes([0x71, body[0], body[1], body[2], 0x00])
        if service == 0x27:
            level = body[0] if body else 0x01
            if len(body) > 1:
                return bytes([0x67, level])
            return bytes([0x67, level, 0x11, 0x22, 0x33, 0x44])
        if service == 0x3E:
            return bytes([0x7E, body[0] if body else 0x00])
        return bytes([0x7F, service, 0x11])

    def _uds_dtcs(self) -> bytes:
        entries = self.fault_codes()
        data = bytearray([0x59, 0x02, 0xFF])  # Verfuegbarkeitsmaske: alle Statusbits
        for entry in entries:
            data += bytes([(entry.code >> 8) & 0xFF, entry.code & 0xFF, 0x00, entry.status & 0xFF])
        return bytes(data)

    # ------------------------------------------------------------------ #
    # Datenkennungen
    # ------------------------------------------------------------------ #
    def _read_identifier(self, identifier: int) -> bytes | None:
        values = self.ident_values()
        if identifier == 0xF190:
            return self.vin.encode("latin-1")
        if identifier == 0xF187:
            return values["part_number"].encode("latin-1")
        if identifier == 0xF188:
            return values["software"].encode("latin-1")
        if identifier == 0xF189:
            return values["software"].encode("latin-1") + b"\x00"
        if identifier == 0xF18A:
            return values["manufacturer"].encode("latin-1")
        if identifier == 0xF191:
            return values["hardware"].encode("latin-1")
        if identifier == 0xF19E:
            return values["serial"].encode("latin-1")
        if identifier in (0xF1A0, 0xF1):
            return f"{self.coding_value}".encode("latin-1")
        if identifier == 0xF15A:
            return self.info.part_number.encode("latin-1")
        if 0xF400 <= identifier <= 0xF4FF:  # Messwerteblock (VAG-Konvention)
            group = identifier & 0xFF
            values = self.group_read(group)
            if values is None:
                return None
            data = bytearray([group])
            for sensor_id, a, b in values:
                data += bytes([sensor_id, a, b])
            return bytes(data)
        if 0xF500 <= identifier <= 0xF5FF:  # Anpassungskanal
            channel = identifier & 0xFF
            value = self.adaptation_read(channel)
            if value is None:
                return None
            return bytes([(value >> 8) & 0xFF, value & 0xFF])
        return None

    def _write_identifier(self, body: bytes, service: int, *, kwp: bool) -> bytes:
        if len(body) < 3:
            return bytes([0x7F, service, 0x13])
        identifier = (body[0] << 8) | body[1]
        value = body[2:]
        response_service = 0x7B if kwp else 0x6E
        if identifier in (0xF1A0, 0xF1):
            text = value.decode("latin-1", errors="replace").strip("\x00 ")
            digits = "".join(ch for ch in text[:5] if ch.isdigit()) or "0"
            self.set_coding(int(digits), self.wsc)
            return bytes([response_service, body[0], body[1]])
        if 0xF500 <= identifier <= 0xF5FF:
            channel = identifier & 0xFF
            if len(value) >= 2:
                new_value = self.adaptation_save(channel, (value[0] << 8) | value[1], self.wsc)
                if new_value is None:
                    return bytes([0x7F, service, 0x31])
            return bytes([response_service, body[0], body[1]])
        return bytes([0x7F, service, 0x31])


class CanEcuNode(CanNode):
    """CAN-Knoten eines Steuergeraets (VW TP 2.0)."""

    def __init__(
        self,
        name: str,
        bus: VirtualCanBus,
        *,
        ecu_addr: int,
        tester_addr: int = 0xF1,
        handler: Callable[[bytes], bytes | None],
        proto: int = 1,
        clock: SimClock | None = None,
    ) -> None:
        super().__init__(name, bus)
        self.ecu_addr = ecu_addr
        self.tester_addr = tester_addr
        self.server = VwtpEcuServer(
            ecu_addr=ecu_addr,
            handler=handler,
            tester_addr=tester_addr,
            proto=proto,
            send_fn=self.send_can,
            on_event=lambda text: bus.trace_event(name, text),
        )

    def accept_ids(self):
        # Broadcast (Aufbau) und eigene ID (Daten vom Tester); auf der
        # Tester-ID sendet dieses Steuergeraet - sie wird nicht mitgehoert.
        return {0x200, 0x200 + self.ecu_addr}

    def on_start(self) -> None:
        self.server.start()

    def on_stop(self) -> None:
        self.server.stop()

    def on_frame(self, frame: CanFrame) -> None:
        self.server.on_frame(frame.arbitration_id, frame.data)


class SimulatedEcu:
    """Ein simuliertes Steuergeraet mit Busanbindung."""

    def __init__(
        self,
        info: ModuleInfo,
        state: VehicleState,
        *,
        kline_bus: KLineBus | None = None,
        can_bus: VirtualCanBus | None = None,
        db: Database | None = None,
        log: Callable[[str], None] | None = None,
        tester_addr: int = 0xF1,
        vin: str | None = None,
    ) -> None:
        self.info = info
        self.module = SimulatedModule(info, state, db=db, log=log, vin=vin)
        self.nodes: list = []
        # VCDS-Adressen sind hexadezimal notiert: Modul "46" spricht auf dem Bus
        # unter dem Byte 0x46. Die Anzeige bleibt "46" (address_text).
        wire = address_wire(info.address)
        self.address_wire = wire
        self.bus_kind = "kline" if (info.bus or "").lower().startswith("k") else "can"
        protocol = (info.protocol or "").upper()
        self.protocol = protocol

        if self.bus_kind == "kline" and kline_bus is not None:
            # Auf der K-Line laeuft der KWP1281-Blockdialog; das Keyword meldet
            # lediglich die Protokollversion (1281 bzw. 2000).
            server = Kwp1281EcuServer(
                f"ECU-{info.address_text}",
                kline_bus,
                wire,
                self.module,
                log=log,
                keyword_version=2000 if "KWP2000" in protocol else 1281,
            )
            self.nodes.append(server)
            self.server = server
        elif can_bus is not None:
            if "UDS" in protocol:
                self.use_vwtp = True
                self.proto = 0  # UDS ohne Laengenpraefix
                handler = self.module.handle_uds
            elif "KWP2000" in protocol:
                if "CAN" in protocol or "TP" in protocol:
                    self.use_vwtp = True
                    self.proto = 1
                else:
                    self.use_vwtp = False
                    self.proto = 1
                handler = self.module.handle_kwp2000
            else:
                self.use_vwtp = True
                self.proto = 1
                handler = self.module.handle_kwp2000

            if self.use_vwtp:
                node = CanEcuNode(
                    f"ECU-{info.address_text}",
                    can_bus,
                    ecu_addr=wire,
                    tester_addr=tester_addr,
                    handler=handler,
                    proto=self.proto,
                )
            else:
                node = IsoTpEndpoint(
                    f"ECU-{info.address_text}",
                    can_bus,
                    tx_id=0x7E8,
                    rx_id=0x7E0,
                    handler=handler,
                )
            self.nodes.append(node)
            self.server = node
        else:
            self.use_vwtp = False
            self.proto = 1
            self.server = None

    # -- Lebenszyklus -------------------------------------------------------- #
    def start(self) -> None:
        for node in self.nodes:
            node.start()

    def stop(self) -> None:
        for node in self.nodes:
            try:
                node.stop()
            except Exception:  # pragma: no cover - Simulation
                pass

    @property
    def address(self) -> int:
        return self.info.address

    @property
    def address_text(self) -> str:
        """Adresse in VCDS-Schreibweise (z. B. "46")."""
        return self.info.address_text

    @property
    def name(self) -> str:
        return self.info.name or f"Steuergerät {self.info.address_text}"
