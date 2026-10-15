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
from ..obd.dtc import decode_sae16, encode_sae16, vag_to_sae
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
        # Codierungen zeigt VCDS mit fuehrenden Nullen (5- bzw. 7-stellig); die
        # Textform der Seed-Daten bleibt deshalb erhalten.
        self.coding_text = (str(module.coding or "0").strip() or "0")
        self.coding_value = int(self.coding_text) if self.coding_text.isdigit() else 0
        self.wsc = int(module.wsc or 0) if str(module.wsc or "0").isdigit() else 0
        self.faults: list[tuple[int | str, int]] = fault_set(module.dtc_set)
        self.adaptations: dict[int, int] = {}
        self.logged_in = False
        self.actuator_value: int | None = None
        self.actuator_active: int = 0          # aktuell angesteuertes Stellglied (0 = aus)
        self.basic_setting_group: int = 0      # aktive Grundeinstellung (0 = aus)
        self._obd_started: float | None = None
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

    def set_coding(self, coding: int, workshop_code: int, *, text: str | None = None) -> bool:
        with self._lock:
            self.log(f"{self.info.name}: Codierung {coding:05d} (vorher {self.coding_value:05d})")
            self.coding_value = int(coding)
            # Textform: ausdruecklich uebergebene Angabe gewinnt, sonst die
            # bisherige Stellenzahl behalten (00002 bleibt 00002).
            if text:
                self.coding_text = text
            elif len(self.coding_text) > len(str(int(coding))):
                self.coding_text = str(int(coding)).rjust(len(self.coding_text), "0")
            else:
                self.coding_text = str(int(coding))
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
        """Messwerteblock lesen.

        Der Grundstellungszustand wird nicht hier gesetzt: auf der K-Line
        schaltet der Blocktitel (``0x28``/``0x29``) um, auf CAN die Routine
        ``FC<Gruppe>`` - siehe :meth:`begin_basic_setting`.
        """
        definition = self.groups.get(group)
        if definition is None:
            return None
        return [field.provider(self.state) for field in definition.fields]

    def begin_basic_setting(self, group: int, on: bool = True) -> None:
        """Grundstellungsmodus ein-/ausschalten (fuer den Blockdialog)."""
        with self._lock:
            self.basic_setting_group = int(group) if on else 0

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
        """Stellglied ansteuern (``value = 0`` schaltet alle wieder ab)."""
        with self._lock:
            self.actuator_value = value
            self.actuator_active = 0 if value == 0 else value
            text = self.actuator_text(self.actuator_active) if self.actuator_active else "keine"
            self.log(f"{self.info.name}: Stellglied {value:02d} ({text})")
        return bytes([value, 0x00])

    def actuator_list(self) -> list[dict]:
        """Stellgliedliste aus der Label-Datei (A-Eintraege)."""
        if self.db is None:
            return []
        return self.db.actuator_list(self.info)

    def actuator_text(self, actuator_id: int) -> str:
        for entry in self.actuator_list():
            if int(entry["actuator_id"]) == int(actuator_id):
                return str(entry.get("text") or entry.get("short_text") or "")
        return ""

    def basic_setting(self, group: int, on: bool) -> bool:
        """Grundeinstellung ein-/ausschalten (Routine 0xFC<Gruppe>)."""
        with self._lock:
            if on:
                self.basic_setting_group = int(group)
                self.log(f"{self.info.name}: Grundeinstellung Block {group:03d} gestartet")
            elif self.basic_setting_group == int(group) or group == 0:
                self.basic_setting_group = 0
                self.log(f"{self.info.name}: Grundeinstellung beendet")
        return True

    def login(self, code: int) -> bool:
        with self._lock:
            self.logged_in = code == 12233 or code == self.coding_value
        return self.logged_in

    # ------------------------------------------------------------------ #
    # OBD-II (SAE J1979) - generische Abgasdiagnose
    # ------------------------------------------------------------------ #
    #: PIDs, die dieses Steuergeraet auf Modus 01 liefert
    OBD_PIDS = (
        0x04, 0x05, 0x06, 0x07, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10,
        0x11, 0x1F, 0x21, 0x2F, 0x31, 0x33, 0x42, 0x46, 0x49, 0x4A,
        0x51, 0x5C, 0x5E,
    )

    @property
    def is_diesel(self) -> bool:
        text = f"{self.info.component or ''} {self.info.name or ''}".upper()
        return any(tag in text for tag in ("EDC", "TDI", "SDI", "PD", "CR "))

    def _sae_text(self, code: int | str) -> str | None:
        """SAE-Schreibweise eines Fehlercodes - nur mit belegter Zuordnung."""
        if isinstance(code, str):
            return code
        value = int(code)
        if self.db is not None:
            found = self.db.dtc_lookup(value)
            if found and found.get("sae_code"):
                return str(found["sae_code"])
        return vag_to_sae(value)

    def _obd_codes(self, mode: int) -> list[str]:
        """Fehlercodes in SAE-Schreibweise fuer die OBD-Modi 03/07/0A."""
        codes: list[str] = []
        for code, status in self.faults:
            if mode == 0x07 and not (status & 0x20):
                continue
            if mode == 0x0A and not (status & 0x08):
                continue
            text = self._sae_text(code)
            # Ohne belegte SAE-Zuordnung wird kein Code erfunden - der Eintrag
            # bleibt der VAG-Diagnose vorbehalten (siehe obd_unmapped()).
            if text:
                codes.append(text)
        return codes

    def obd_unmapped(self) -> list[int | str]:
        """Fehlercodes ohne SAE-Zuordnung (nur ueber die VAG-Diagnose lesbar)."""
        return [code for code, _ in self.faults if self._sae_text(code) is None]

    def _obd_supported(self, base: int) -> bytes:
        """Bitmaske der unterstuetzten PIDs ab ``base`` (Modus 01, PID 00/20/..)."""
        bits = bytearray(4)
        for pid in self.OBD_PIDS:
            offset = pid - base
            if 1 <= offset <= 32:
                bits[(offset - 1) // 8] |= 0x80 >> ((offset - 1) % 8)
        # Die Abfrage der naechsten Gruppe wird mit gemeldet
        nxt = base + 32
        if any(pid > base + 32 for pid in self.OBD_PIDS) or nxt in (0x20, 0x40, 0x60):
            offset = 32
            bits[(offset - 1) // 8] |= 0x80 >> ((offset - 1) % 8)
        return bytes(bits)

    def _obd_pid(self, pid: int) -> bytes | None:
        """Rohdaten eines Modus-01-PID aus dem Fahrzeugzustand."""
        state = self.state
        if pid == 0x00 or pid in (0x20, 0x40, 0x60):
            return self._obd_supported(pid)
        if pid == 0x01:
            return self._obd_status_bytes()
        if pid == 0x02:
            codes = self._obd_codes(0x03)
            return encode_sae16(codes[0]).to_bytes(2, "big") if codes else b"\x00\x00"
        if pid == 0x03:
            return b"\x02\x00"  # geschlossener Regelkreis, Bank 1
        if pid == 0x04:
            load = min(100.0, max(0.0, state.throttle_percent + state.injection_mg * 2))
            return bytes([int(load * 255 / 100), 0])
        if pid == 0x05:
            return bytes([int(state.coolant_temp) + 40, 0])
        if pid == 0x06:
            return bytes([int(max(0.0, min(255.0, (state.lambda_value - 1.0) * 128 + 128))), 0])
        if pid == 0x07:
            return bytes([int(max(0.0, min(255.0, (state.lambda_value - 1.0) * 128 + 128))), 0])
        if pid == 0x0B:
            return bytes([int(state.boost_mbar / 10), 0])
        if pid == 0x0C:
            value = int(state.rpm * 4)
            return bytes([(value >> 8) & 0xFF, value & 0xFF])
        if pid == 0x0D:
            return bytes([int(max(0.0, state.speed_kmh)), 0])
        if pid == 0x0E:
            value = int((state.injection_deg + 64) * 2)
            return bytes([max(0, min(255, value)), 0])
        if pid == 0x0F:
            return bytes([int(state.intake_temp) + 40, 0])
        if pid == 0x10:
            value = int(state.maf_mgh * max(state.rpm, 800) / 60 / 100 * 100)
            return bytes([(value >> 8) & 0xFF, value & 0xFF])
        if pid == 0x11:
            return bytes([int(max(0.0, min(255.0, state.throttle_percent * 255 / 100))), 0])
        if pid == 0x1F:
            runtime = int(self._obd_runtime())
            return bytes([(runtime >> 8) & 0xFF, runtime & 0xFF])
        if pid == 0x21:
            return bytes([0, 0]) if not self.fault_codes() else bytes([1, 20])
        if pid == 0x2F:
            return bytes([int(state.fuel_level_l / 55 * 255), 0])
        if pid == 0x31:
            return bytes([0, 0]) if not self.fault_codes() else bytes([2, 40])
        if pid == 0x33:
            return bytes([100, 0])  # 100 kPa Normalluftdruck
        if pid == 0x42:
            value = int(state.battery_v * 1000)
            return bytes([(value >> 8) & 0xFF, value & 0xFF])
        if pid == 0x46:
            return bytes([int(state.outside_temp) + 40, 0])
        if pid == 0x49:
            return bytes([int(max(0.0, min(255.0, state.throttle_percent * 255 / 100))), 0])
        if pid == 0x4A:
            return bytes([int(max(0.0, min(255.0, state.throttle_percent * 255 / 90))), 0])
        if pid == 0x51:
            return bytes([4 if self.is_diesel else 1, 0])
        if pid == 0x5C:
            return bytes([int(state.oil_temp) + 40, 0])
        if pid == 0x5E:
            value = int(state.injection_mg * max(state.rpm, 800) * 4 / 60 / 100 * 20)
            return bytes([(value >> 8) & 0xFF, value & 0xFF])
        return None

    def _obd_runtime(self) -> float:
        """Laufzeit seit Motorstart (s) - aus der Simulationsuhr."""
        if not self.state.engine_running:
            return 0.0
        if self._obd_started is None:
            self._obd_started = time.monotonic()
        return time.monotonic() - self._obd_started

    def _obd_status_bytes(self) -> bytes:
        """PID 01: MIL, Fehleranzahl und Readiness-Bits."""
        codes = self._obd_codes(0x03)
        mil = 0x80 if codes else 0x00
        first = mil | (len(codes) & 0x7F)
        engine = 0x08 if self.is_diesel else 0x00
        continuous = 0x00  # alle kontinuierlichen Monitore abgeschlossen
        if self.is_diesel:
            # Bit 3 Ladedruck, Bit 5 Abgassensor, Bit 6 Partikelfilter, Bit 7 AGR
            monitors = 0xE8 if self.groups else 0x00
        else:
            # Bit 0 Katalysator, Bit 1 beheizter Katalysator, Bit 5 Lambda, Bit 7 AGR
            monitors = 0x22 if self.groups else 0x00
        return bytes([first, engine, continuous, monitors])

    def _obd_freeze_frame(self, pid: int) -> bytes | None:
        if pid == 0x02:
            codes = self._obd_codes(0x03)
            return encode_sae16(codes[0]).to_bytes(2, "big") if codes else b"\x00\x00"
        return self._obd_pid(pid)

    def _obd_vehicle_info(self, pid: int) -> bytes | None:
        if pid == 0x00:
            return bytes([0x55, 0x40, 0x00, 0x00])  # 02, 04, 06, 0A unterstuetzt
        if pid == 0x02:
            return bytes([0x01]) + self.vin.encode("ascii", errors="replace") + b"\x00"
        if pid == 0x04:
            text = (self.info.software or "0000").encode("ascii", errors="replace")
            return bytes([0x01]) + text[:16].ljust(16, b"\x00")
        if pid == 0x06:
            seed = sum(self.info.part_number.encode("ascii", errors="replace")) & 0xFFFFFFFF
            return bytes([0x01, (seed >> 24) & 0xFF, (seed >> 16) & 0xFF,
                          (seed >> 8) & 0xFF, seed & 0xFF])
        if pid == 0x0A:
            text = (self.info.part_number or "VAGDIAG").encode("ascii", errors="replace")
            return bytes([0x01]) + text[:20]
        return None

    def _obd_monitors(self) -> bytes:
        """Modus 06: reduzierte Monitortabelle (MID, TID, Wert, Grenzen)."""
        state = self.state
        tests: list[tuple[int, int, int, int, int]] = []
        tests.append((0x21, 0x01, int(state.coolant_temp + 40), 0, 255))
        tests.append((0x31, 0x01, int(state.egr_percent * 255 / 100), 0, 255))
        tests.append((0x01, 0x01, int(max(0.0, min(255.0, state.lambda_value * 128))), 0, 255))
        if self.is_diesel:
            tests.append((0x3D, 0x01, int(state.boost_mbar / 10), 80, 260))
        payload = bytearray()
        for mid, tid, value, low, high in tests:
            payload += bytes(
                [
                    mid,
                    tid,
                    0x00,
                    0x00,
                    (value >> 8) & 0xFF,
                    value & 0xFF,
                    (low >> 8) & 0xFF,
                    low & 0xFF,
                    (high >> 8) & 0xFF,
                    high & 0xFF,
                ]
            )
        return bytes(payload)

    def handle_obd(self, payload: bytes) -> bytes | None:
        """Generische OBD-II-Dienste (Modi 01, 02, 03, 04, 06, 07, 09, 0A)."""
        if not payload:
            return None
        mode = payload[0]
        body = payload[1:]
        if mode == 0x01:
            if not body:
                return bytes([0x7F, mode, 0x13])
            pid = body[0]
            data = self._obd_pid(pid)
            if data is None:
                return bytes([0x7F, mode, 0x12])
            return bytes([0x41, pid]) + data
        if mode == 0x02:
            if not body:
                return bytes([0x7F, mode, 0x13])
            pid = body[0]
            data = self._obd_freeze_frame(pid)
            if data is None:
                return bytes([0x7F, mode, 0x12])
            return bytes([0x42, pid]) + data
        if mode in (0x03, 0x07, 0x0A):
            codes = self._obd_codes(mode)
            payload_out = bytearray([0x40 | mode, len(codes) & 0xFF])
            for code in codes:
                payload_out += encode_sae16(code).to_bytes(2, "big")
            return bytes(payload_out)
        if mode == 0x04:
            self.clear_fault_codes()
            return bytes([0x44])
        if mode == 0x06:
            return bytes([0x46]) + self._obd_monitors()
        if mode == 0x09:
            if not body:
                return bytes([0x7F, mode, 0x13])
            pid = body[0]
            data = self._obd_vehicle_info(pid)
            if data is None:
                return bytes([0x7F, mode, 0x12])
            return bytes([0x49, pid]) + data
        return bytes([0x7F, mode, 0x11])

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
            digits = "".join(ch for ch in coding_text[:7] if ch.isdigit()) or "0"
            self.set_coding(int(digits), self.wsc, text=digits)
            return bytes([0x7B, body[0] if body else 0x00]) + bytes([0x01])
        if service == 0x30:  # InputOutputControlByLocalIdentifier
            # Leitungsformat: 30 <Stellglied-ID> <Steuerparameter> <Wert>
            # Steuerparameter 0x01 = ansteuern, 0x00 = abschalten.
            if len(body) < 3:
                return bytes([0x7F, service, 0x13])
            control = body[1]
            self.actuator_test(body[2] if control else 0)
            return bytes([0x70, body[0], body[1], body[2]])
        if service == 0x31:  # StartRoutine: Anpassung, Stellglied, Grundeinstellung
            # Leitungsformat (siehe docs/PROTOKOLLE.md):
            #   31 00 <Kanal>                  Anpassung lesen
            #   31 02 <Kanal>                  Anpassung pruefen (nur lesen)
            #   31 01 <Kanal> <Wert hi/lo>     Anpassung speichern
            #   31 01 F0<Stellglied-ID>        Stellglied ansteuern
            #   31 02 F0<Stellglied-ID>        Stellglied abschalten
            #   31 01 FC<Gruppe>               Grundeinstellung starten
            #   31 02 FC<Gruppe>               Grundeinstellung beenden
            if not body:
                return bytes([0x7F, service, 0x31])
            kind = body[0]
            if kind in (0x01, 0x02) and len(body) == 3 and body[1] >= 0xF0:
                routine = (body[1] << 8) | body[2]
                if routine >= 0xFC00:
                    self.basic_setting(routine & 0xFF, kind == 0x01)
                else:
                    self.actuator_test((routine & 0xFF) if kind == 0x01 else 0)
                return bytes([0x71, kind, body[1], body[2]])
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
        if service in (0x01, 0x02, 0x03, 0x04, 0x06, 0x07, 0x09, 0x0A):
            # Generische OBD-Dienste (SAE J1979) laufen ueber denselben Kanal
            return self.handle_obd(payload)
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
        if service == 0x31:  # RoutineControl
            # 31 <Sub> F0<Stellglied-ID>   Stellglied an/aus (Sub 01 = starten)
            # 31 <Sub> FC<Gruppe>          Grundeinstellung an/aus
            # 31 <Sub> <Kanal> <Wert>      Anpassung (VAG-Konvention, s. Doku)
            if len(body) < 2:
                return bytes([0x7F, service, 0x13])
            sub = body[0]
            if len(body) == 3 and body[1] >= 0xF0:
                routine = (body[1] << 8) | body[2]
                if routine >= 0xFC00:
                    self.basic_setting(routine & 0xFF, sub == 0x01)
                else:
                    self.actuator_test((routine & 0xFF) if sub == 0x01 else 0)
                return bytes([0x71, sub, body[1], body[2]])
            if len(body) < 4:
                return bytes([0x7F, service, 0x13])
            routine = (body[1] << 8) | body[2]
            self.routines[routine] = "gestartet" if sub == 0x01 else "gestoppt"
            return bytes([0x71, sub, body[1], body[2], 0x00])
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
            return self.coding_text.encode("latin-1")
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
            digits = "".join(ch for ch in text[:7] if ch.isdigit()) or "0"
            self.set_coding(int(digits), self.wsc, text=digits)
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
        self.obd_node: IsoTpEndpoint | None = None
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
            if self.use_vwtp and info.address == 1:
                # Auf CAN-Fahrzeugen laeuft die generische Abgasdiagnose
                # (OBD-II) ueber ISO-TP 0x7E0/0x7E8, waehrend die
                # VAG-Diagnose den VW-TP-Kanal benutzt.
                obd_node = IsoTpEndpoint(
                    f"OBD-{info.address_text}",
                    can_bus,
                    tx_id=0x7E8,
                    rx_id=0x7E0,
                    handler=self.module.handle_obd,
                )
                self.nodes.append(obd_node)
                self.obd_node = obd_node
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
