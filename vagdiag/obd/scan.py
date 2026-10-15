"""Autoscan: Adressplan, Ergebnisstruktur und VCDS-Bericht.

Der Autoscan-Bericht ist absichtlich am Textformat von VCDS angelehnt::

    Dienstag,14,Oktober,2025,21:34:12:00000
    VCDS -- VAG-Schnittstelle ...
    Fahrzeug: VW Golf IV 1.9 TDI (2001)   Fahrgestellnummer: WVWZZZ1JZXW000000

    Adresse 01: Motorsteuergerät (038 906 018 G)
    ...
    Kein Fehlercode gefunden.
    ...

    Ende -- 6 Steuergeraete, 4 Fehlercodes
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .. import APP_NAME, APP_SUBTITLE, __version__, address_text
from .dtc import DtcInfo

__all__ = [
    "ScanTarget",
    "ModuleResult",
    "AutoscanResult",
    "build_scan_plan",
    "format_fault_list",
    "format_autoscan",
    "vcds_timestamp",
]

WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONTHS = (
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
)


def vcds_timestamp(when: float | None = None) -> str:
    """Zeitstempel im VCDS-Format: ``Dienstag,14,Oktober,2025,21:34:12:00000``."""
    moment = time.localtime(time.time() if when is None else when)
    return (
        f"{WEEKDAYS[moment.tm_wday]},{moment.tm_mday},{MONTHS[moment.tm_mon - 1]},"
        f"{moment.tm_year},{time.strftime('%H:%M:%S', moment)}:00000"
    )


@dataclass
class ScanTarget:
    """Ein Steuergeraet, das beim Autoscan angesprochen wird."""

    address: int
    name: str
    protocol: str = ""
    bus: str = ""
    part_number: str = ""

    @property
    def address_text(self) -> str:
        """Adresse in VCDS-Schreibweise ("01", "15", "46")."""
        return address_text(self.address)


@dataclass
class ModuleResult:
    """Ergebnis fuer ein einzelnes Steuergeraet."""

    address: int
    name: str
    protocol: str = ""
    bus: str = ""
    status: str = "ok"  # ok | keine_antwort | fehler
    part_number: str = ""
    component: str = ""
    ident_lines: list[str] = field(default_factory=list)
    ident_text: str = ""
    label_file: str = ""
    coding: str = ""
    workshop_code: str = ""
    faults: list[DtcInfo] = field(default_factory=list)
    duration: float = 0.0
    message: str = ""

    @property
    def address_text(self) -> str:
        """Adresse in VCDS-Schreibweise ("01", "15", "46")."""
        return address_text(self.address)

    @property
    def fault_count(self) -> int:
        return len(self.faults)

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "address_text": self.address_text,
            "name": self.name,
            "protocol": self.protocol,
            "bus": self.bus,
            "status": self.status,
            "part_number": self.part_number,
            "component": self.component,
            "ident_lines": list(self.ident_lines),
            "ident_text": self.ident_text,
            "label_file": self.label_file,
            "coding": self.coding,
            "workshop_code": self.workshop_code,
            "fault_count": self.fault_count,
            "faults": [fault.as_dict() for fault in self.faults],
            "duration": round(self.duration, 3),
            "message": self.message,
        }


@dataclass
class AutoscanResult:
    """Gesamtergebnis eines Autoscans."""

    vehicle: str = ""
    vin: str = ""
    modules: list[ModuleResult] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    @property
    def duration(self) -> float:
        end = self.finished or time.time()
        return max(0.0, end - self.started)

    @property
    def found(self) -> int:
        return sum(1 for module in self.modules if module.status == "ok")

    @property
    def missing(self) -> list[ModuleResult]:
        return [module for module in self.modules if module.status != "ok"]

    @property
    def fault_count(self) -> int:
        return sum(module.fault_count for module in self.modules)

    def as_dict(self) -> dict:
        return {
            "vehicle": self.vehicle,
            "vin": self.vin,
            "started": self.started,
            "finished": self.finished,
            "duration": round(self.duration, 3),
            "modules_found": self.found,
            "modules_total": len(self.modules),
            "fault_count": self.fault_count,
            "modules": [module.as_dict() for module in self.modules],
        }


def build_scan_plan(modules) -> list[ScanTarget]:
    """Adressplan fuer den Autoscan (nach Adresse sortiert)."""
    targets = []
    for module in modules:
        targets.append(
            ScanTarget(
                address=module.address,
                name=module.name,
                protocol=module.protocol,
                bus=module.bus,
                part_number=module.part_number,
            )
        )
    return sorted(targets, key=lambda target: target.address)


def format_fault_list(faults: list[DtcInfo], indent: str = "   ") -> str:
    """Fehlerliste im VCDS-Stil (``00575 - Saugrohrdruck ...``)."""
    if not faults:
        return f"{indent}Kein Fehlercode gefunden."
    lines = [f"{indent}{len(faults)} Fehlercode(s) gefunden:"]
    for fault in faults:
        code = fault.display_code or "?????"
        text = fault.text or "(kein Eintrag in der Datenbank)"
        suffix = f" - {fault.status_str}" if fault.status_str else ""
        lines.append(f"{indent}{code}{suffix}")
        lines.append(f"{indent}   {text}")
        if fault.text_short and fault.status_style != "uds":
            lines.append(f"{indent}   {fault.text_short}")
    return "\n".join(lines)


def format_autoscan(result: AutoscanResult, *, show_protocol: bool = False) -> str:
    """Erzeugt den Autoscan-Bericht im VCDS-Textformat."""
    lines: list[str] = []
    lines.append(vcds_timestamp(result.started))
    lines.append("")
    lines.append(f"{APP_NAME} -- {APP_SUBTITLE} {__version__}")
    lines.append("")
    lines.append(f"Fahrzeug: {result.vehicle}")
    if result.vin:
        lines.append(f"Fahrgestellnummer: {result.vin}")
    lines.append("")
    lines.append("Adressplan:")
    lines.append("")
    for module in result.modules:
        name = module.name
        part = f" ({module.part_number})" if module.part_number else ""
        if show_protocol and module.protocol:
            name = f"{name} [{module.protocol}{' / ' + module.bus if module.bus else ''}]"
        lines.append(f"Adresse {module.address_text}: {name}{part}")
        if module.status != "ok":
            reason = module.message or "keine Antwort"
            lines.append(f"   {reason}")
            wait = f"   Wartezeit: {module.duration:.1f} s" if module.duration else ""
            if wait:
                lines.append(wait)
            lines.append("")
            continue
        if module.ident_lines:
            for ident_line in module.ident_lines:
                lines.append(f"   {ident_line.strip()}")
        elif module.ident_text:
            for ident_line in module.ident_text.splitlines():
                lines.append(f"   {ident_line.strip()}")
        elif module.part_number or module.component:
            lines.append(f"   {module.part_number}  {module.component}".rstrip())
        if module.label_file:
            lines.append(f"   Labeldatei: {module.label_file}")
        if module.coding:
            lines.append(
                f"   Codierung: {module.coding}   Werkstattcode: {module.workshop_code or '-'}"
            )
        lines.append(format_fault_list(module.faults))
        lines.append("")
    lines.append(
        f"Autoscan abgeschlossen in {result.duration:.1f} s - "
        f"{result.found}/{len(result.modules)} Steuergeraete antworten, "
        f"{result.fault_count} Fehlercode(s)."
    )
    return "\n".join(lines)
