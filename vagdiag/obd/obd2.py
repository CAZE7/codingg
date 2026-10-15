"""OBD-II (SAE J1979 / ISO 15031-5) - PIDs, Readiness und Formate.

Der Modus-Begriff folgt der VCDS-Oberflaeche: Modus 01 ist "Live-Daten",
02 "Freeze Frame", 03/07/0A sind Fehlerspeicher (gespeichert, schwebend,
permanent), 06 sind Monitor-Testergebnisse und 09 die Fahrzeuginformation.

Die Tabellen sind die oeffentlich dokumentierten Standard-PIDs; die
Readiness-Bits folgen SAE J1979 (PID 01, Bytes B/C/D).
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "OBD_MODES",
    "PidInfo",
    "PID_TABLE",
    "pid_info",
    "decode_readiness",
    "format_pid_value",
    "format_codes",
    "monitor_names",
]

#: Moduskennungen wie in VCDS benannt
OBD_MODES = {
    0x01: "Live-Daten (aktueller Wert)",
    0x02: "Freeze Frame (Momentaufnahme)",
    0x03: "Fehlercodes (gespeichert)",
    0x04: "Fehlercodes loeschen",
    0x05: "O2-Sensor-Monitortest",
    0x06: "Monitortest-Ergebnisse",
    0x07: "Fehlercodes (schwebend)",
    0x08: "Stellgliedtest (On-Board)",
    0x09: "Fahrzeuginformation",
    0x0A: "Fehlercodes (permanent)",
}


@dataclass(frozen=True)
class PidInfo:
    """Ein unterstuetzter PID samt Einheit und Umrechnung."""

    pid: int
    name: str
    unit: str = ""
    #: (A, B) -> Wert; ``None`` = Rohdaten ohne Formel
    formula: object = None
    group: str = "Motor"

    def text(self, data: bytes) -> str:
        """Wert formatieren.

        Mehrbyte-PIDs liefern zwei Datenbytes (A, B). Einbyte-PIDs wie die
        Kuehlmitteltemperatur beantwortet ein echtes Steuergeraet mit einem
        Byte; die Formeln nutzen dann nur A, der fehlende zweite Wert wird
        deshalb mit 0 ergaenzt.
        """
        if self.formula is None:
            return data.hex(" ").upper()
        if len(data) < 2:
            data = bytes(data) + b"\x00"
        return self.formula(data[0], data[1])  # type: ignore[operator]


def _pct(a: int, b: int) -> str:
    return f"{a * 100 / 255:.1f}".replace(".", ",")


def _temp(a: int, b: int) -> str:
    return f"{a - 40} °C"


def _rpm(a: int, b: int) -> str:
    return f"{(a * 256 + b) / 4:.0f} 1/min"


def _speed(a: int, b: int) -> str:
    return f"{a} km/h"


def _timing(a: int, b: int) -> str:
    return f"{a / 2 - 64:.1f}".replace(".", ",") + " °"


def _maf(a: int, b: int) -> str:
    return f"{(a * 256 + b) / 100:.1f}".replace(".", ",") + " g/s"


def _map_kpa(a: int, b: int) -> str:
    return f"{a} kPa"


def _volt(a: int, b: int) -> str:
    return f"{(a * 256 + b) / 1000:.2f}".replace(".", ",") + " V"


def _fuel(a: int, b: int) -> str:
    return f"{a * 100 / 255:.1f}".replace(".", ",") + " %"


def _runtime(a: int, b: int) -> str:
    return f"{a * 256 + b} s"


def _distance(a: int, b: int) -> str:
    return f"{a * 256 + b} km"


def _trim(a: int, b: int) -> str:
    return f"{a / 1.28 - 100:.1f}".replace(".", ",") + " %"


def _pressure_pa(a: int, b: int) -> str:
    return f"{a * 256 + b} Pa"


PID_TABLE: dict[int, PidInfo] = {
    0x00: PidInfo(0x00, "Unterstuetzte PIDs (01-20)", "", None, "System"),
    0x01: PidInfo(0x01, "Readiness / MIL-Status", "", None, "System"),
    0x02: PidInfo(0x02, "DTC, der den Freeze Frame ausloeste", "", None, "System"),
    0x03: PidInfo(0x03, "Kraftstoffsystem-Status", "", None, "System"),
    0x04: PidInfo(0x04, "Berechnete Motorlast", "%", _pct),
    0x05: PidInfo(0x05, "Kühlmitteltemperatur", "°C", _temp),
    0x06: PidInfo(0x06, "Kurzzeit-Kraftstoffkorrektur Bank 1", "%", _trim),
    0x07: PidInfo(0x07, "Langzeit-Kraftstoffkorrektur Bank 1", "%", _trim),
    0x08: PidInfo(0x08, "Kurzzeit-Kraftstoffkorrektur Bank 2", "%", _trim),
    0x09: PidInfo(0x09, "Langzeit-Kraftstoffkorrektur Bank 2", "%", _trim),
    0x0A: PidInfo(0x0A, "Kraftstoffdruck", "kPa", lambda a, b: f"{a * 3} kPa"),
    0x0B: PidInfo(0x0B, "Saugrohrdruck (absolut)", "kPa", _map_kpa),
    0x0C: PidInfo(0x0C, "Motordrehzahl", "1/min", _rpm),
    0x0D: PidInfo(0x0D, "Fahrzeuggeschwindigkeit", "km/h", _speed),
    0x0E: PidInfo(0x0E, "Zündzeitpunkt-Vorverstellung", "°", _timing),
    0x0F: PidInfo(0x0F, "Ansauglufttemperatur", "°C", _temp),
    0x10: PidInfo(0x10, "Luftmassenmesser", "g/s", _maf),
    0x11: PidInfo(0x11, "Drosselklappenstellung", "%", _pct),
    0x1C: PidInfo(0x1C, "OBD-Standard des Fahrzeugs", "", lambda a, b: f"0x{a:02X}", "System"),
    0x1F: PidInfo(0x1F, "Laufzeit seit Motorstart", "s", _runtime),
    0x21: PidInfo(0x21, "Strecke mit MIL an", "km", _distance),
    0x2F: PidInfo(0x2F, "Kraftstofftankfüllstand", "%", _fuel),
    0x31: PidInfo(0x31, "Strecke seit Fehlerlöschung", "km", _distance),
    0x33: PidInfo(0x33, "Luftdruck (barometrisch)", "kPa", _map_kpa),
    0x3C: PidInfo(0x3C, "Katalysator-Temperatur Bank 1 Sensor 1", "°C", lambda a, b: f"{(a * 256 + b) / 10 - 40:.1f}".replace(".", ",") + " °C"),
    0x42: PidInfo(0x42, "Steuergerätespannung", "V", _volt, "System"),
    0x46: PidInfo(0x46, "Umgebungslufttemperatur", "°C", _temp),
    0x49: PidInfo(0x49, "Gaspedalstellung D", "%", _pct),
    0x4A: PidInfo(0x4A, "Gaspedalstellung E", "%", _pct),
    0x51: PidInfo(0x51, "Kraftstoffart", "", lambda a, b: {"1": "Benzin", "4": "Diesel"}.get(str(a), f"0x{a:02X}"), "System"),
    0x5C: PidInfo(0x5C, "Motoröltemperatur", "°C", _temp),
    0x5E: PidInfo(0x5E, "Kraftstoffverbrauch", "l/h", lambda a, b: f"{(a * 256 + b) / 20:.1f}".replace(".", ",") + " l/h"),
    0xA6: PidInfo(0xA6, "Strecke gesamt", "km", lambda a, b: f"{((a * 256 + b) * 256) * 256 % (2 ** 32)} km", "System"),
}


def pid_info(pid: int) -> PidInfo:
    return PID_TABLE.get(pid, PidInfo(pid, f"PID 0x{pid:02X}"))


# --------------------------------------------------------------------------- #
# Readiness (PID 01)
# --------------------------------------------------------------------------- #
#: Kontinuierliche Monitore (Byte C, Bits 0..2)
CONTINUOUS_MONITORS = ["Fehlzuendung", "Kraftstoffsystem", "umfassende Komponenten"]

#: Nicht kontinuierliche Monitore (Byte D, Bits 0..7)
NON_CONTINUOUS_MONITORS = [
    "Katalysator",
    "beheizter Katalysator",
    "Verdampfungsanlage (EVAP)",
    "Sekundaerluftsystem",
    "Klimaanlagen-Kaeltemittel",
    "Lambdasonde",
    "Lambdasondenheizung",
    "Abgasrueckfuehrung (AGR/VVT)",
]


#: Nicht kontinuierliche Monitore bei Selbstzuendung (Diesel, Byte D)
DIESEL_MONITORS = [
    "NMHC-Katalysator",
    "NOx-/SCR-System",
    "(reserviert)",
    "Ladedruckregelung",
    "(reserviert)",
    "Abgassensor",
    "Partikelfilter",
    "AGR/VVT",
]


def monitor_names(compression: int = 0) -> list[str]:
    """Monitorliste fuer Fremd- (0) oder Selbstzuendung (1)."""
    second = DIESEL_MONITORS if compression else NON_CONTINUOUS_MONITORS
    return [*CONTINUOUS_MONITORS, *second]


@dataclass
class Readiness:
    """Auswertung von Modus 01, PID 01 (vier Nutzbytes)."""

    mil_on: bool = False
    dtc_count: int = 0
    #: Motorbauart: 0 = Fremdzuendung (Benzin), 1 = Selbstzuendung (Diesel)
    compression: int = 0
    raw: bytes = b""
    #: Monitore, deren Test noch laeuft (Bit gesetzt)
    incomplete: list[str] = field(default_factory=list)
    #: Monitore ohne gesetztes Bit - abgeschlossen oder nicht verbaut
    complete: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mil": self.mil_on,
            "dtc_count": self.dtc_count,
            "compression": self.compression,
            "raw": list(self.raw),
            "incomplete": list(self.incomplete),
            "complete": list(self.complete),
        }


def decode_readiness(data: bytes) -> Readiness:
    """Wertet die vier Bytes von PID 01 aus (A, B, C, D).

    Nach SAE J1979 gilt: ein gesetztes Bit bedeutet "Monitor laeuft noch"
    (nicht abgeschlossen). Ob ein Monitor ueberhaupt verbaut ist, laesst sich
    aus den Bytes allein nicht sicher ableiten - die Anzeige nennt deshalb
    beide Listen getrennt.
    """
    if len(data) < 4:
        return Readiness(raw=bytes(data))
    a, b, c, d = data[0], data[1], data[2], data[3]
    result = Readiness(
        mil_on=bool(a & 0x80),
        dtc_count=a & 0x7F,
        compression=(b >> 3) & 0x01,
        raw=bytes(data[:4]),
    )
    for index, name in enumerate(CONTINUOUS_MONITORS):
        (result.incomplete if c & (1 << index) else result.complete).append(name)
    monitors = DIESEL_MONITORS if result.compression else NON_CONTINUOUS_MONITORS
    for index, name in enumerate(monitors):
        (result.incomplete if d & (1 << index) else result.complete).append(name)
    return result


def format_codes(codes: list[str], empty_text: str = "keine") -> str:
    if not codes:
        return empty_text
    return ", ".join(codes)


def format_pid_value(pid: int, data: bytes) -> str:
    info = pid_info(pid)
    return f"{pid:02X} {info.name:<42} {info.text(data)}"
