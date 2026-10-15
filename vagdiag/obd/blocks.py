"""Messwertbloecke: Datentypen und Formatierung.

Ein Messwerteblock (VCDS: "Messwerteblock 001") besteht aus bis zu vier
Messwerten. Jeder Messwert wird im KWP1281-Block als Tripel
``(Sensortyp, A, B)`` uebertragen. Der Sensortyp bestimmt die Formel:

    Wert = f(A, B)

Die Tabelle stammt aus der oeffentlich dokumentierten KWP1281-Datenliste
(blafusel.de, Abschnitt 7) und deckt sich mit der Referenzimplementierung
``kw1281test`` (``Blocks/SensorValue.cs``).

Fuer KWP2000/UDS-Steuergeraete liefert der Messwerteblock-Rahmen dieselben
Tripel; die Zuordnung zu Text und Einheit erfolgt in beiden Faellen ueber
Label-Dateien bzw. Tabellen in der Datenbank (siehe ``docs/DATENBANK.md``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "Measurement",
    "SENSOR_TYPES",
    "format_measurement",
    "measurement_value",
    "sensor_description",
    "parse_measurement_triples",
]


@dataclass
class Measurement:
    """Ein einzelner Messwert aus einem Messwerteblock."""

    sensor_id: int
    a: int
    b: int
    value: float | str | None = None
    unit: str = ""
    text: str = ""

    def as_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "a": self.a,
            "b": self.b,
            "value": self.value,
            "unit": self.unit,
            "text": self.text,
        }


def _r(value: float, digits: int = 1) -> float:
    return round(value, digits)


# --- Tabelle der Sensortypen ----------------------------------------------- #
# Jeder Eintrag: (Formel -> (Wert, Einheit, Nachkommastellen), Beschreibung)
STATIC_FORMULAS: dict[int, tuple[Callable[[int, int], tuple], str]] = {
    1: (lambda a, b: (0.2 * a * b, "1/min", 0), "Drehzahl"),
    2: (lambda a, b: (_r(a * 0.002 * b), "%", 1), "Last / Fuellung"),
    3: (lambda a, b: (_r(0.002 * a * b), "°KW", 1), "Zuendwinkel"),
    4: (
        lambda a, b: (
            _r(abs(b - 127) * 0.01 * a),
            "°" + ("nach OT" if b > 127 else "vor OT"),
            1,
        ),
        "Einspritzbeginn",
    ),
    5: (lambda a, b: (_r(a * (b - 100) * 0.1), "°C", 1), "Temperatur"),
    6: (lambda a, b: (_r(0.001 * a * b), "V", 1), "Spannung"),
    7: (lambda a, b: (0.01 * a * b, "km/h", 2), "Geschwindigkeit"),
    8: (lambda a, b: (_r(0.1 * a * b), "", 1), "Zahlenwert"),
    9: (lambda a, b: (_r((b - 127) * 0.02 * a), "°", 1), "Winkel"),
    10: (lambda a, b: ("kalt" if b == 0 else "warm", "", 0), "Zustand"),
    11: (lambda a, b: (_r(0.0001 * a * (b - 128) + 1), "", 1), "Faktor"),
    12: (lambda a, b: (_r(0.001 * a * b), "Ω", 1), "Widerstand"),
    13: (lambda a, b: (_r((b - 127) * 0.001 * a), "mm", 1), "Hub"),
    14: (lambda a, b: (_r(0.005 * a * b), "bar", 1), "Druck"),
    15: (lambda a, b: (_r(0.01 * a * b), "ms", 1), "Einspritzzeit"),
    16: (
        lambda a, b: (f"{a:08b} {b:08b}", "", 0),
        "Binaerstatus",
    ),
    17: (lambda a, b: (f"{chr(a)}{chr(b)}", "", 0), "Text"),
    18: (lambda a, b: (_r(0.04 * a * b), "mbar", 1), "Druck (klein)"),
    19: (lambda a, b: (_r(a * b * 0.01), "l", 1), "Volumen"),
    20: (lambda a, b: (_r(a * (b - 128) / 128.0), "%", 1), "Differenzwert"),
    21: (lambda a, b: (_r(0.001 * a * b), "V", 1), "Spannung (klein)"),
    22: (lambda a, b: (_r(0.001 * a * b), "ms", 1), "Zeit (klein)"),
    23: (lambda a, b: (_r(b / 256.0 * a), "%", 1), "Verhaeltnis"),
    24: (lambda a, b: (_r(0.001 * a * b), "A", 1), "Strom"),
    25: (lambda a, b: (_r((b * 1.421) + (a / 182.0)), "g/s", 1), "Luftmasse"),
    26: (lambda a, b: (b - a, "°C", 0), "Temperaturdifferenz"),
    27: (
        lambda a, b: (
            _r(abs(b - 128) * 0.01 * a),
            "°" + ("vor OT" if b < 128 else "nach OT"),
            1,
        ),
        "Einspritzbeginn (Differenz)",
    ),
    28: (lambda a, b: (b - a, "", 0), "Zaehler"),
    29: (lambda a, b: ("Kennfeld 1" if b < a else "Kennfeld 2", "", 0), "Kennfeld"),
    30: (lambda a, b: (_r(b / 12.0 * a), "°C", 1), "Temperatur (Skaliert)"),
    31: (lambda a, b: (_r(b / 2560.0 * a), "", 3), "Lambdaverhaeltnis"),
    32: (lambda a, b: (b - 256 if b > 128 else b, "", 0), "Vorzeichenbehaftet"),
    33: (lambda a, b: (_r(100.0 * b if a == 0 else 100.0 * b / a), "%", 1), "Quotient"),
    34: (lambda a, b: (_r((b - 128) * 0.01 * a), "kW", 1), "Leistung"),
    35: (lambda a, b: (_r(0.01 * a * b), "l/h", 1), "Verbrauch"),
    36: (lambda a, b: (a * 2560 + b * 10, "km", 0), "Laufleistung"),
    38: (lambda a, b: (_r((b - 128) * 0.001 * a), "°", 1), "Winkel (Differenz)"),
    39: (lambda a, b: (_r(b / 256.0 * a), "mg/Hub", 1), "Einspritzmenge"),
    40: (lambda a, b: (_r(b * 0.1 + (25.5 * a) - 400), "A", 1), "Strom (Batterie)"),
    41: (lambda a, b: (b + a * 255, "Ah", 0), "Ladungsmenge"),
    42: (lambda a, b: (_r(b * 0.1 + (25.5 * a) - 400), "kW", 1), "Leistung (Batterie)"),
    43: (lambda a, b: (_r(b * 0.1 + (25.5 * a)), "V", 1), "Batteriespannung"),
    44: (lambda a, b: (f"{a:02d}:{b:02d}", "", 0), "Uhrzeit"),
    45: (lambda a, b: (_r(0.1 * a * b / 100.0), "", 1), "Faktor"),
    46: (lambda a, b: (_r((a * b - 3200) * 0.0027), "°", 1), "Temperatur (Skaliert)"),
    47: (lambda a, b: ((b - 128) * a, "ms", 0), "Zeit (vorzeichenbehaftet)"),
    48: (lambda a, b: (b + a * 255, "", 0), "Zaehler"),
    49: (lambda a, b: (_r(b / 4.0 * a * 0.1), "mg/Hub", 1), "Einspritzmenge (fein)"),
    50: (lambda a, b: (_r((b - 128) / 0.01 if a == 0 else (b - 128) / (0.01 * a)), "mbar", 1), "Druck (fein)"),
    51: (lambda a, b: (_r(((b - 128) / 255.0) * a), "mg/Hub", 1), "Einspritzmenge (fein 2)"),
    52: (lambda a, b: (_r(b * 0.02 * a - a), "Nm", 1), "Drehmoment"),
    53: (lambda a, b: (_r((b - 128) * 1.4222 + 0.006 * a), "g/s", 1), "Luftmasse (fein)"),
    54: (lambda a, b: (a * 256 + b, "", 0), "16-Bit-Zahlenwert"),
    55: (lambda a, b: (_r(a * b / 200.0), "s", 1), "Zeit"),
    56: (lambda a, b: (a * 256 + b, "WSC", 0), "Werkstattcode"),
    57: (lambda a, b: (a * 256 + b + 65536, "WSC", 0), "Werkstattcode (mit Ueberlauf)"),
    58: (lambda a, b: (_r(1.0225 * (256 - b) if b > 128 else 1.0225 * b), "1/s", 1), "Drehzahl (klein)"),
    59: (lambda a, b: (_r((a * 256 + b) / 32768.0), "", 1), "Verhaeltnis (16 Bit)"),
    60: (lambda a, b: (_r((a * 256 + b) * 0.01), "s", 2), "Sekunden"),
    61: (lambda a, b: (_r((b - 128) if a == 0 else (b - 128) / a), "", 1), "Zahlenwert (Verhaeltnis)"),
    62: (lambda a, b: (_r(0.256 * a * b), "S", 1), "Leitwert"),
    63: (lambda a, b: (f"{chr(a)}{chr(b)}", "", 0), "Text (gekuerzt)"),
    64: (lambda a, b: (a + b, "Ω", 0), "Widerstand (Summe)"),
    65: (lambda a, b: (_r(0.01 * a * (b - 127)), "mm", 1), "Hub (fein)"),
    66: (lambda a, b: (_r((a * b) / 511.12), "V", 1), "Spannung (Skaliert)"),
    67: (lambda a, b: (_r((640 * a) + b * 2.5), "°", 1), "Temperatur"),
    68: (lambda a, b: (_r((256 * a + b) / 7.365), "°/s", 1), "Winkelgeschwindigkeit"),
    69: (lambda a, b: (_r((256 * a + b) * 0.3254), "bar", 1), "Druck (16 Bit)"),
    70: (lambda a, b: (_r((256 * a + b) * 0.192), "m/s²", 1), "Beschleunigung"),
}

# Alias fuer die Tabelle: Sensortyp -> (Funktion, Beschreibung)
SENSOR_TYPES = {key: value[0] for key, value in STATIC_FORMULAS.items()}


def sensor_description(sensor_id: int) -> str:
    entry = STATIC_FORMULAS.get(sensor_id)
    return entry[1] if entry else f"unbekannter Sensortyp {sensor_id}"


def measurement_value(sensor_id: int, a: int, b: int) -> tuple:
    """Liefert ``(Wert, Einheit, Nachkommastellen)`` fuer einen Sensortyp."""
    entry = STATIC_FORMULAS.get(sensor_id)
    if entry is None:
        return (None, "", 0)
    value, unit, digits = entry[0](a, b)
    return (value, unit, digits)


def format_measurement(sensor_id: int, a: int, b: int) -> Measurement:
    """Erzeugt einen formatierten Messwert (Wert + Einheit + Beschreibung)."""
    value, unit, digits = measurement_value(sensor_id, a, b)
    if isinstance(value, float):
        text = f"{value:.{digits}f}".replace(".", ",")
    elif value is None:
        text = f"{sensor_id}:{a}:{b}"
    else:
        text = str(value)
    return Measurement(
        sensor_id=sensor_id,
        a=a,
        b=b,
        value=value,
        unit=unit,
        text=f"{text} {unit}".strip(),
    )


def parse_measurement_triples(payload: bytes) -> list[tuple[int, int, int]]:
    """Zerlegt eine Messwerteblock-Antwort in (Sensortyp, A, B)-Tripel."""
    if len(payload) % 3 != 0:
        raise ValueError(f"Messwerteblocklaenge {len(payload)} ist kein Vielfaches von 3")
    return [
        (payload[i], payload[i + 1], payload[i + 2]) for i in range(0, len(payload), 3)
    ]
