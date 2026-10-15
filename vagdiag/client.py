"""Hochsprachen-API des VCDS-Nachbaus.

Diese Ebene verbindet Datenbank, Protokolle und (simulierte) Fahrzeuge zu der
Schnittstelle, die Kommandozeile und Weboberflaeche nutzen::

    session = VehicleSession()                 # simuliertes Fahrzeug aus der DB
    session.start()
    scan = session.autoscan()
    with session.open_module(0x01) as module:
        print(module.ident()["text"])
        for fault in module.read_faults():
            print(fault.display_code, fault.text)
        print(module.read_block(1)["fields"])

Je nach hinterlegtem Protokoll wird automatisch der passende Transport
gewaehlt:

======================  =====================================================
Protokoll               Transport / Dialog
======================  =====================================================
KWP1281 (K-Line)        KWP1281-Blockdialog mit 5-Baud-Initialisierung
KWP2000 (K-Line)        derselbe Blockdialog (Keywords melden Version 2000)
KWP2000-on-CAN          VW TP 2.0 mit KWP2000-Diensten
UDS                     VW TP 2.0 mit UDS-Diensten
OBD/KWP2000 (ISO-TP)    ISO-TP auf 0x7E0/0x7E8
======================  =====================================================

Faellt der primaere Transport aus (Steuergeraet antwortet nicht), probiert die
Sitzung die naechste Variante - genau wie ein Diagnoseinterface, das sich auf
das Steuergeraet einsynchronisiert.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import address_text, address_wire
from .db import Database, ModuleInfo
from .obd import scan as scan_mod
from .obd.blocks import format_measurement, sensor_description
from .obd.dtc import DtcInfo
from .proto.bus import TraceRecorder
from .proto.isotp import IsoTpEndpoint, IsoTpError
from .proto.kw1281 import IdentResult, Kwp1281Error, Kwp1281Tester
from .proto.kwp2000 import Kwp2000Client, Kwp2000Error
from .proto.uds import (
    UdsClient,
    UdsError,
    decode_status_byte_de as uds_status_bits_de,
    status_summary_de as uds_status_summary,
)
from .proto.vwtp import VwtpChannel, VwtpError
from .sim.vehicles import SimulatedVehicle, load_vehicle

__all__ = ["ModuleSession", "VehicleSession", "DiagnosticError", "Progress"]


class DiagnosticError(Exception):
    """Fehler im Diagnosedialog (kein Transport, Adresse nicht verbaut, ...)."""


@dataclass
class Progress:
    """Fortschrittsmeldung fuer den Autoscan."""

    address: int
    stage: str
    message: str = ""
    status: str = "ok"

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "address_text": address_text(self.address),
            "stage": self.stage,
            "message": self.message,
            "status": self.status,
        }


class ModuleSession:
    """Eine offene Verbindung zu einem Steuergeraet."""

    def __init__(
        self,
        session: "VehicleSession",
        module: ModuleInfo,
        *,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.session = session
        self.module = module
        self.db: Database = session.db
        self.on_event = on_event or (lambda text: None)
        self.transport_name = module.protocol or "unbekannt"
        self.kline: Kwp1281Tester | None = None
        self.vwtp: VwtpChannel | None = None
        self.isotp: IsoTpEndpoint | None = None
        self.kwp2000: Kwp2000Client | None = None
        self.uds: UdsClient | None = None
        self.ident_result: IdentResult | None = None
        self.ident_lines: list[str] = []
        self._label: dict | None = None
        self._open = False
        self._actuator_active: int = 0
        self._basic_group: int = 0

    # ------------------------------------------------------------------ #
    # Verbindungsaufbau
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "ModuleSession":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def open(self) -> "ModuleSession":
        if self._open:
            return self
        bus = (self.module.bus or "").strip()
        protocol = (self.module.protocol or "").upper()
        is_kline = bus.lower().startswith("k")
        errors: list[str] = []
        if is_kline:
            self._open_kline()
        else:
            # CAN-Steuergeraete: VW TP 2.0 zuerst, ISO-TP als Rueckfallebene
            wants_uds = "UDS" in protocol
            try:
                self._open_vwtp(uds=wants_uds)
            except (VwtpError, Kwp2000Error, UdsError, TimeoutError) as exc:
                errors.append(f"VW TP 2.0: {exc}")
                self._close_vwtp()
                try:
                    self._open_isotp(uds=wants_uds)
                except (IsoTpError, Kwp2000Error, UdsError, TimeoutError) as exc2:
                    errors.append(f"ISO-TP: {exc2}")
                    self._close_isotp()
        if not self._open:
            raise DiagnosticError(
                f"Adresse {self.module.address_text}: keine Verbindung "
                f"({self.module.protocol}; {'; '.join(errors) or 'keine Antwort'})"
            )
        return self

    def _open_kline(self) -> None:
        bus = self.session.vehicle.kline_bus
        if bus is None:
            raise DiagnosticError("Dieses Fahrzeug hat keinen K-Line-Bus")
        tester = Kwp1281Tester(
            f"VAGDiag-{self.module.address_text}", bus, log=self.on_event
        )
        tester.connect(address_wire(self.module.address), timeout=self.session.timeout)
        self.kline = tester
        self.ident_result = tester.ident_result
        version = tester.protocol_version or 1281
        self.transport_name = (
            "KWP1281-Blockdialog (K-Line, Keywords KW1281)"
            if version == 1281
            else f"KWP1281-Blockdialog (K-Line, Keywords KW{version})"
        )
        self._open = True

    def _open_vwtp(self, *, uds: bool) -> None:
        bus = self.session.vehicle.can_bus
        if bus is None:
            raise DiagnosticError("Dieses Fahrzeug hat keinen CAN-Bus")
        channel = VwtpChannel(
            f"VAGDiag-{self.module.address:02X}",
            bus,
            address_wire(self.module.address),
            proto=0 if uds else 1,
            # Etwas grosszuegiger als VCDS: die Simulation plant Threads ein,
            # ein zu knapper Wert erzeugt sporadische Zeitfehler.
            timeout=0.6,
            on_event=self.on_event,
        )
        channel.start()
        channel.open()
        self.vwtp = channel
        if uds:
            self.uds = UdsClient(channel, self.module.address, timeout=self.session.timeout)
            self.uds.start()
            self.transport_name = "VW TP 2.0 (UDS)"
        else:
            self.kwp2000 = Kwp2000Client(
                channel, self.module.address, timeout=self.session.timeout
            )
            self.kwp2000.start()
            self.transport_name = "VW TP 2.0 (KWP2000)"
        self._open = True

    def _open_isotp(self, *, uds: bool) -> None:
        bus = self.session.vehicle.can_bus
        if bus is None:
            raise DiagnosticError("Dieses Fahrzeug hat keinen CAN-Bus")
        endpoint = IsoTpEndpoint(
            f"VAGDiag-{self.module.address_text}",
            bus,
            tx_id=0x7E0,
            rx_id=0x7E8,
            timeout=self.session.timeout,
        )
        endpoint.start()
        self.isotp = endpoint
        if uds:
            self.uds = UdsClient(endpoint, self.module.address, timeout=self.session.timeout)
            self.uds.start()
            self.transport_name = "ISO-TP (UDS)"
        else:
            self.kwp2000 = Kwp2000Client(
                endpoint, self.module.address, timeout=self.session.timeout
            )
            self.kwp2000.start()
            self.transport_name = "ISO-TP (KWP2000)"
        self._open = True

    def close(self) -> None:
        if self.kline is not None:
            try:
                self.kline.end()
            except Exception:  # noqa: BLE001 - Abschied darf nie stoeren
                pass
            self._close_vwtp()
            self._close_isotp()
            try:
                self.session.vehicle.kline_bus.detach(self.kline)
            except Exception:  # noqa: BLE001
                pass
            self.kline = None
        if self.kwp2000 is not None:
            try:
                self.kwp2000.stop()
            except Exception:  # noqa: BLE001
                pass
            self.kwp2000 = None
        if self.uds is not None:
            try:
                self.uds.stop()
            except Exception:  # noqa: BLE001
                pass
            self.uds = None
        self._close_vwtp()
        self._close_isotp()
        self._open = False

    def _close_vwtp(self) -> None:
        if self.vwtp is not None:
            try:
                self.vwtp.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.vwtp.stop(0.2)
            except Exception:  # noqa: BLE001
                pass
            self.vwtp = None

    def _close_isotp(self) -> None:
        if self.isotp is not None:
            try:
                self.isotp.stop(0.2)
            except Exception:  # noqa: BLE001
                pass
            self.isotp = None

    # ------------------------------------------------------------------ #
    # Identifikation
    # ------------------------------------------------------------------ #
    def ident(self) -> dict:
        lines: list[str] = []
        if self.ident_result is not None:
            lines = [line for line in self.ident_result.lines if line.strip()]
        elif self.kwp2000 is not None:
            lines = [line for line in (self.kwp2000.ident_text() or "").splitlines() if line]
        elif self.uds is not None:
            lines = [line for line in (self.uds.ident_text() or "").splitlines() if line]
        if not lines:
            lines = [f"{self.module.part_number}  {self.module.component}  {self.module.software}"]
        coding = self.ident_result.coding if self.ident_result is not None else None
        wsc = self.ident_result.workshop_code if self.ident_result is not None else None
        coding_text = (
            self._padded_coding(str(coding)) if coding is not None else (self.module.coding or "")
        )
        label = self.label()
        return {
            "address": self.module.address,
            "address_text": self.module.address_text,
            "name": self.module.name,
            "protocol": self.transport_name,
            "part_number": self.module.part_number,
            "component": self.module.component,
            "software": self.module.software,
            "lines": lines,
            "text": "\n".join(lines),
            "coding": coding,
            "coding_text": coding_text,
            "workshop_code": wsc,
            "workshop_code_text": f"{wsc:05d}" if wsc is not None else (self.module.wsc or ""),
            "label_file": (label or {}).get("name") or self.module.label_file,
        }

    def label(self) -> dict | None:
        """Labeldatei des Steuergeraets (aus der Datenbank, mit Redir-Aufloesung)."""
        if self._label is None:
            self._label = self.db.label_for_module(self.module)
        return self._label

    def block_numbers(self) -> list[int]:
        """Verfuegbare Messwertebloecke (aus dem Steuergeraet, sonst aus dem Label)."""
        if self.kline is not None:
            # Grundeinstellungs- und Messwertbloecke sind geraeteabhhaengig; wir
            # fragen die Bloecke des Labels ab und pruefen sie am Geraet.
            label = self.label() or {}
            candidates = sorted(
                {entry["block"] for entry in label.get("measuring", []) if entry["block"]}
            )
            if candidates:
                return candidates
        return self.db.block_numbers(self.module)

    # ------------------------------------------------------------------ #
    # Fehlerspeicher
    # ------------------------------------------------------------------ #
    def read_faults(self) -> list[DtcInfo]:
        if self.kline is not None:
            entries = self.kline.read_fault_codes(timeout=self.session.timeout)
            return [self._vag_dtc(entry.code, entry.status) for entry in entries]
        if self.kwp2000 is not None:
            return [self._lookup_fault(fault) for fault in self.kwp2000.read_faults()]
        if self.uds is not None:
            return [self._uds_fault(fault) for fault in self.uds.read_faults()]
        return []

    def clear_faults(self) -> None:
        if self.kline is not None:
            self.kline.clear_fault_codes(timeout=self.session.timeout)
        elif self.kwp2000 is not None:
            self.kwp2000.clear_faults()
        elif self.uds is not None:
            self.uds.clear_faults()

    def _vag_dtc(self, code: int, status: int) -> DtcInfo:
        lookup = self.db.dtc_lookup(code) or {}
        text = lookup.get("text") or ""
        if lookup.get("sae_code"):
            text = f"{text}  (SAE {lookup['sae_code']})".strip()
        return DtcInfo(
            vag_code=code,
            sae_code=lookup.get("sae_code"),
            status=status,
            text=text,
            text_short=lookup.get("text_short") or "",
            source=lookup.get("source") or "unbekannt",
        )

    def _lookup_fault(self, fault: DtcInfo) -> DtcInfo:
        lookup = self.db.dtc_lookup(fault.display_code) or {}
        fault.text = lookup.get("text") or fault.text or ""
        fault.text_short = lookup.get("text_short") or fault.text_short or ""
        fault.source = lookup.get("source") or fault.source or "unbekannt"
        return fault

    #: Statusbits, die in der Anzeige genannt werden (Reihenfolge = Prioritaet)
    _UDS_KEY_BITS = ("Fehler aktiv", "bestätigt", "ausstehend", "Warnleuchte angefordert")

    def _uds_fault(self, fault: dict) -> DtcInfo:
        code = fault.get("sae") or ""
        status = int(fault.get("status") or 0)
        lookup = self.db.dtc_lookup(code) if code else None
        bits = uds_status_bits_de(status)
        summary = uds_status_summary(status)
        # Nur die aussagekraeftigen Bits nennen ("statisch (Fehler aktiv, bestätigt)").
        key = [bit for bit in self._UDS_KEY_BITS if bit in bits][:2]
        text_short = f"{summary} ({', '.join(key)})" if key else summary
        text = (lookup or {}).get("text") or ""
        return DtcInfo(
            vag_code=(lookup or {}).get("vag_code"),
            sae_code=code,
            status=status,
            text=text,
            text_short=text_short,
            source=f"UDS (FTB {fault.get('ftb_hex', '0x00')})",
            status_style="uds",
        )

    # ------------------------------------------------------------------ #
    # Messwertebloecke
    # ------------------------------------------------------------------ #
    def read_block(self, group: int, *, basic_setting: bool = False) -> dict:
        triples: list[tuple[int, int, int]] = []
        if self.kline is not None:
            reading = self.kline.group_read(
                group, basic_setting=basic_setting, timeout=self.session.timeout
            )
            triples = list(reading.values)
        elif self.kwp2000 is not None:
            triples = [
                (measurement.sensor_id, measurement.a, measurement.b)
                for measurement in self.kwp2000.measuring_block(group)
            ]
        elif self.uds is not None:
            payload = self.uds.measuring_block(group)
            triples = _parse_triples(payload[1:]) if len(payload) > 1 else []
        label = self.label() or {}
        if not triples:
            raise DiagnosticError(f"Messwerteblock {group:03d} liefert keine Werte")
        fields: list[dict] = []
        for index, (sensor_id, a, b) in enumerate(triples, start=1):
            measurement = format_measurement(sensor_id, a, b)
            entry = self._label_entry(label, group, index, basic=basic_setting)
            # Die Zahl stammt aus der Sensorformel, ihre Einheit gilt daher fuer
            # die Anzeige. Weicht die Labeldatei ab (z. B. "mbar" gegen "bar"),
            # wird die abweichende Angabe nur als Hinweis mitgegeben.
            label_unit = ((entry or {}).get("unit") or "").strip()
            sensor_unit = measurement.unit or ""
            if label_unit in ("", "?") or _units_compatible(label_unit, sensor_unit):
                unit = label_unit if label_unit not in ("", "?") else sensor_unit
                unit_hint = ""
            elif sensor_unit:
                # Die Zahl stammt aus der Sensorformel; ihre Einheit gilt fuer die
                # Anzeige, die abweichende Labelangabe wird als Hinweis gezeigt.
                unit = sensor_unit
                unit_hint = label_unit
            else:
                unit = label_unit
                unit_hint = ""
            fields.append(
                {
                    "field": index,
                    "text": (entry or {}).get("text") or sensor_description(sensor_id),
                    "unit": unit,
                    "label_unit": label_unit,
                    "unit_hint": unit_hint,
                    "value": measurement.value,
                    # Anzeige ohne Einheit; die Oberflaeche ergaenzt "unit"
                    "display": _without_unit(measurement.text, unit),
                    "display_full": measurement.text.rstrip(),
                    "sensor_id": sensor_id,
                    "a": a,
                    "b": b,
                    "min_value": (entry or {}).get("min_value"),
                    "max_value": (entry or {}).get("max_value"),
                }
            )
        return {
            "group": group,
            "title": self.db.block_text(label, group, 0, basic=basic_setting)
            or f"Messwerteblock {group:03d}",
            "basic": basic_setting,
            "label": label.get("name") or "",
            "fields": fields,
        }

    @staticmethod
    def _label_entry(label: dict, group: int, field: int, *, basic: bool) -> dict | None:
        entries = [*label.get("measuring", []), *label.get("measure_extra", [])]
        for entry in entries:
            if (
                entry["block"] == group
                and entry["field"] == field
                and bool(entry.get("basic")) == bool(basic)
            ):
                return entry
        return None

    # ------------------------------------------------------------------ #
    # Codierung / Anpassung / Stellglieder
    # ------------------------------------------------------------------ #
    def coding(self) -> dict:
        value: int | str | None = None
        if self.kline is not None:
            value = self.ident_result.coding if self.ident_result else None
        elif self.kwp2000 is not None:
            try:
                value = self._padded_coding(self.kwp2000.coding().strip("\x00 "))
            except Kwp2000Error as exc:
                value = f"nicht lesbar ({exc})"
        elif self.uds is not None:
            try:
                raw = self.uds.coding()
                value = raw.decode("latin-1", errors="replace").strip("\x00 ")
                value = self._padded_coding(value)
            except UdsError as exc:
                value = f"nicht lesbar ({exc})"
        if isinstance(value, int):
            value = str(value)
        if value:
            # Steuergeraete melden die Codierung als Zahl; VCDS zeigt die
            # Stellenzahl des Bestands (00002, 0021505, 0000071).
            value = self._padded_coding(value)
        else:
            value = self.module.coding
        label = self.label() or {}
        return {
            "address": self.module.address,
            "coding": value,
            "protocol": self.transport_name,
            "options": [
                {
                    "kind": entry.get("kind"),
                    "block": entry.get("block"),
                    "bit": entry.get("bit"),
                    "value": entry.get("value"),
                    "text": entry.get("text"),
                }
                for entry in label.get("coding", [])
            ],
        }

    def write_coding(self, value: int | str) -> dict:
        text = str(value).strip()
        if self.kline is not None:
            coding = self.kline.write_coding(
                int(text), self.session.workshop_code, timeout=self.session.timeout
            )
            self.ident_result = coding
            return {"ok": True, "coding": coding.coding, "workshop_code": coding.workshop_code}
        if self.kwp2000 is not None:
            self.kwp2000.set_coding(text)
            return {"ok": True, "coding": text}
        if self.uds is not None:
            self.uds.set_coding(text.encode("ascii", errors="replace"))
            return {"ok": True, "coding": text}
        raise DiagnosticError("Dieser Transport kann keine Codierung schreiben")

    def adaptations(self) -> list[dict]:
        """Anpassungskanaele des Steuergeraets (aus der Datenbank)."""
        return self.db.adaptation_channels_for_module(self.module)

    def adaptation_read(self, channel: int) -> dict:
        value: int | None = None
        if self.kline is not None:
            value = self.kline.adaptation_read(channel, timeout=self.session.timeout).value
        elif self.kwp2000 is not None:
            value = self.kwp2000.adaptation_value(channel)
        elif self.uds is not None:
            data = self.uds.adaptation(channel)
            value = int.from_bytes(data[:2], "big") if len(data) >= 2 else None
        return {
            "channel": channel,
            "value": value,
            "description": self._channel_text(channel),
        }

    def adaptation_save(self, channel: int, value: int) -> dict:
        if self.kline is not None:
            response = self.kline.adaptation_save(
                channel, int(value), self.session.workshop_code, timeout=self.session.timeout
            )
            return {"ok": True, "channel": channel, "value": response.value}
        if self.kwp2000 is not None:
            self.kwp2000.set_adaptation(channel, int(value))
        elif self.uds is not None:
            self.uds.set_adaptation(channel, int(value))
        else:
            raise DiagnosticError("Dieser Transport kann keine Anpassung speichern")
        return {"ok": True, "channel": channel, "value": int(value)}

    def _padded_coding(self, value: str) -> str:
        """Codierung mit der Stellenzahl des Bestands anzeigen.

        Steuergeraete liefern die Codierung als Zahl; VCDS zeigt sie mit
        fuehrenden Nullen (``0021505``). Hat die hinterlegte Codierung mehr
        Stellen, wird entsprechend aufgefuellt - auch nach einer Aenderung
        (``00002`` -> ``00003``).
        """
        stored = (self.module.coding or "").strip()
        text = (value or "").strip()
        if text.isdigit() and stored.isdigit() and len(stored) > len(text):
            return text.rjust(len(stored), "0")
        return value

    def _channel_text(self, channel: int) -> str:
        for entry in self.adaptations():
            if int(entry["channel"]) == int(channel):
                return entry.get("text") or ""
        return ""

    # ------------------------------------------------------------------ #
    # Stellglieddiagnose (Output Test) und Grundeinstellung
    # ------------------------------------------------------------------ #
    def actuators(self) -> list[dict]:
        """Stellgliedliste des Steuergeraets in der Reihenfolge der Labeldatei."""
        entries = self.db.actuator_list(self.module)
        return [
            {
                "id": int(entry["actuator_id"]),
                "step": int(entry["step"]),
                "text": entry.get("text") or entry.get("short_text") or "",
                "active": int(entry["actuator_id"]) == self._actuator_active,
            }
            for entry in entries
        ]

    @property
    def actuator_active(self) -> int:
        """Zuletzt angesteuertes Stellglied (0 = alle aus)."""
        return self._actuator_active

    def output_test(self, actuator_id: int) -> dict:
        """Stellglied ansteuern (``0`` schaltet alle wieder ab).

        VCDS nennt das "Stellglieddiagnose": die Liste wird der Reihe nach
        durchgeschaltet, jedes Stellglied laesst sich einzeln testen.
        """
        actuator_id = int(actuator_id)
        text = ""
        for entry in self.actuators():
            if entry["id"] == actuator_id:
                text = entry["text"]
                break
        if self.kline is not None:
            data = self.kline.actuator_test(actuator_id, timeout=self.session.timeout)
            response = data.hex(" ").upper()
        elif self.kwp2000 is not None:
            self.kwp2000.actuator_switch(actuator_id, actuator_id != 0)
            response = f"Stellglied {actuator_id:02d} geschaltet"
        elif self.uds is not None:
            self.uds.actuator_switch(actuator_id, actuator_id != 0)
            response = f"Stellglied {actuator_id:02d} geschaltet"
        else:
            raise DiagnosticError("Dieser Transport kann kein Stellglied ansteuern")
        self._actuator_active = actuator_id
        return {
            "ok": True,
            "actuator": actuator_id,
            "text": text,
            "response": response,
        }

    def actuator_test(self, value: int) -> dict:
        """Kurzform von :meth:`output_test` (Kompatibilitaet)."""
        return self.output_test(int(value))

    def basic_settings(self) -> list[dict]:
        """Grundeinstellungsbloecke (B-Eintraege) der Labeldatei."""
        label = self.label() or {}
        entries = [
            entry
            for entry in label.get("measuring", [])
            if entry.get("basic") and int(entry["field"]) == 0
        ]
        return [
            {"group": int(entry["block"]), "text": entry.get("text") or ""}
            for entry in sorted(entries, key=lambda item: int(item["block"]))
        ]

    @property
    def basic_setting_group(self) -> int:
        """Aktive Grundeinstellung (0 = keine)."""
        return self._basic_group

    def basic_setting(self, group: int, on: bool = True) -> dict:
        """Grundeinstellung starten oder beenden.

        VCDS liest waehrend der Grundeinstellung denselben Block wie sonst,
        das Steuergeraet arbeitet aber im Grundstellungsmodus (z. B. fuer
        Foerderbeginn oder Partikelfilter-Regeneration).
        """
        group = int(group)
        block: dict | None = None
        if self.kline is not None:
            # KWP1281: der Grundstellungsblock selbst schaltet den Modus um -
            # Blocktitel 0x28 startet, 0x29 beendet die Grundeinstellung.
            block = self.read_block(group, basic_setting=on)
            mode = on
        elif self.kwp2000 is not None:
            self.kwp2000.basic_setting_switch(group, on)
            mode = on
        elif self.uds is not None:
            self.uds.basic_setting_switch(group, on)
            mode = on
        else:
            raise DiagnosticError("Dieser Transport kann keine Grundeinstellung")
        if self.kline is None and mode:
            # Auch auf CAN liefert VCDS die Werte im Grundstellungsmodus
            try:
                block = self.read_block(group, basic_setting=True)
            except DiagnosticError:
                block = None
        self._basic_group = group if mode else 0
        text = ""
        for entry in self.basic_settings():
            if entry["group"] == group:
                text = entry["text"]
                break
        return {
            "ok": True,
            "group": group,
            "active": bool(mode),
            "text": text,
            "block": block,
        }

    def login(self, code: int) -> bool:
        if self.kline is not None:
            return self.kline.login(int(code), timeout=self.session.timeout)
        if self.kwp2000 is not None:
            self.kwp2000.login(int(code))
            return True
        if self.uds is not None:
            self.uds.login(0x01, int(code).to_bytes(2, "big"))
            return True
        return False

    def as_dict(self) -> dict:
        ident = self.ident()
        return {
            "module": self.module.as_dict(),
            "transport": self.transport_name,
            "ident": ident,
        }


def _units_compatible(label_unit: str, sensor_unit: str) -> bool:
    """Passen zwei Einheiten zusammen (gleiche Einheit, gleiches Symbol)?

    ``°vor OT`` und ``°nach OT`` sind dieselbe Winkeleinheit mit
    unterschiedlicher Richtungsangabe, ``mbar`` und ``bar`` dagegen nicht.
    """
    if not label_unit or not sensor_unit or label_unit == "?":
        # "?" heisst in Labeldateien: Anzeige uebernimmt die Einheit der Formel
        return True
    left = label_unit.lower().replace(" ", "")
    right = sensor_unit.lower().replace(" ", "")
    if left == right:
        return True
    # Gemeinsames fuehrendes Symbol, danach unterschiedliche Zusatzangabe
    symbol = ""
    for char in left:
        if char.isalpha():
            break
        symbol += char
    return bool(symbol) and right.startswith(symbol)


def _without_unit(text: str, unit: str) -> str:
    """Entfernt die Einheit vom Ende einer Messwertanzeige."""
    text = text.rstrip()
    if unit and text.endswith(unit):
        text = text[: -len(unit)].rstrip()
    return text


def _parse_triples(payload: bytes) -> list[tuple[int, int, int]]:
    if len(payload) % 3:
        payload = payload[: len(payload) - (len(payload) % 3)]
    return [(payload[i], payload[i + 1], payload[i + 2]) for i in range(0, len(payload), 3)]


class VehicleSession:
    """Diagnosesitzung fuer ein Fahrzeug (Simulator oder echte Hardware)."""

    def __init__(
        self,
        db: Database | None = None,
        vehicle: SimulatedVehicle | None = None,
        *,
        vehicle_name: str | int | None = None,
        trace: TraceRecorder | None = None,
        clock_speed: float = 0.0,
        speed: float = 6.0,
        drive: bool = True,
        timeout: float = 1.0,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.db = db or Database()
        self.timeout = timeout
        self.log = log or (lambda text: None)
        self.trace = trace or TraceRecorder()
        self.vehicle = vehicle or load_vehicle(
            vehicle_name,
            db=self.db,
            trace=self.trace,
            clock_speed=clock_speed,
            speed=speed,
            drive=drive,
            log=self.log,
        )
        self.workshop_code = 12345
        self._sessions: dict[int, ModuleSession] = {}
        self._lock = threading.RLock()
        self._started = False

    # ------------------------------------------------------------------ #
    # Fahrzeug
    # ------------------------------------------------------------------ #
    @property
    def name(self) -> str:
        return self.vehicle.name

    def start(self) -> None:
        if not self._started:
            self.vehicle.start()
            self._started = True

    def stop(self) -> None:
        for address in list(self._sessions):
            self.close_module(address)
        if self._started:
            self.vehicle.stop()
            self._started = False

    def module_list(self) -> list[ModuleInfo]:
        return list(self.vehicle.modules)

    def module(self, address: int) -> ModuleInfo | None:
        return self.vehicle.module(address)

    def status(self) -> dict:
        data = self.vehicle.status()
        data["database"] = self.db.meta()
        data["stats"] = self.db.stats()
        return data

    def trace_tail(self, count: int = 200) -> list[dict]:
        return [entry.as_dict() for entry in self.trace.tail(count)]

    # ------------------------------------------------------------------ #
    # Steuergeraetedialog
    # ------------------------------------------------------------------ #
    def open_module(self, address: int) -> ModuleSession:
        with self._lock:
            existing = self._sessions.get(address)
            if existing is not None and existing._open:  # noqa: SLF001
                return existing
            self.start()
            module = self.module(address)
            if module is None:
                raise DiagnosticError(f"Adresse {address_text(address)} ist nicht verbaut")
            session = ModuleSession(self, module, on_event=self._trace_event)
            session.open()
            self._sessions[address] = session
            return session

    def close_module(self, address: int) -> None:
        with self._lock:
            session = self._sessions.pop(address, None)
        if session is not None:
            session.close()

    def _trace_event(self, text: str) -> None:
        self.log(text)
        self.trace.event("VAGDiag", text)

    # ------------------------------------------------------------------ #
    # Autoscan
    # ------------------------------------------------------------------ #
    def autoscan(
        self,
        addresses: list[int] | None = None,
        *,
        progress: Callable[[Progress], None] | None = None,
        read_faults: bool = True,
    ) -> scan_mod.AutoscanResult:
        """Alle Steuergeraete ansprechen, Identifikation und Fehlerspeicher lesen."""
        self.start()
        plan = scan_mod.build_scan_plan(self.module_list())
        result = scan_mod.AutoscanResult(vehicle=self.name)
        result.vin = getattr(self.vehicle, "vin", "")
        for target in plan:
            if addresses is not None and target.address not in addresses:
                continue
            module_result = scan_mod.ModuleResult(
                address=target.address,
                name=target.name,
                protocol=target.protocol,
                bus=target.bus,
                part_number=target.part_number,
            )
            started = time.time()
            if progress:
                progress(Progress(target.address, "verbinden", target.name))
            try:
                with self.open_module(target.address) as session:
                    ident = session.ident()
                    module_result.ident_lines = list(ident.get("lines") or [])
                    module_result.ident_text = ident.get("text") or ""
                    module_result.label_file = ident.get("label_file") or ""
                    module_result.coding = str(ident.get("coding_text") or "")
                    module_result.workshop_code = str(ident.get("workshop_code_text") or "")
                    module_result.component = ident.get("component") or ""
                    module_result.status = "ok"
                    if read_faults and progress:
                        progress(Progress(target.address, "fehlerspeicher", "Fehlerspeicher lesen"))
                    if read_faults:
                        module_result.faults = session.read_faults()
            except (DiagnosticError, Kwp1281Error, Kwp2000Error, UdsError, IsoTpError) as exc:
                module_result.status = "keine_antwort"
                module_result.message = str(exc)
            except TimeoutError as exc:
                module_result.status = "keine_antwort"
                module_result.message = f"Zeitueberschreitung: {exc}"
            finally:
                self.close_module(target.address)
            module_result.duration = time.time() - started
            result.modules.append(module_result)
            if progress:
                progress(
                    Progress(
                        target.address,
                        "fertig",
                        (
                            f"{module_result.fault_count} Fehlercode(s)"
                            if module_result.status == "ok"
                            else module_result.message
                        ),
                        status=module_result.status,
                    )
                )
        result.finished = time.time()
        return result
