"""Messwertebloecke und Fehlersaetze je Steuergeraeterolle.

Jede Rolle beschreibt, welche Messwertegruppen ein simuliertes Steuergeraet
beantwortet und welche Fehlercodes es im Speicher haelt. Die Messwerte werden
als ``(Sensortyp, A, B)``-Tripel gebildet (siehe :mod:`vagdiag.obd.blocks`);
die Umrechnungsfunktionen invertieren die Formeln so, dass die Anzeige im
VCDS-Nachbau plausible Werte liefert.

Beispiel: Kühlmitteltemperatur mit Sensortyp 5 (``a * (b - 100) * 0.1``) -
mit ``a = 10`` gilt ``Temperatur = b - 100``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .profile import VehicleState

__all__ = ["Field", "Group", "ROLES", "role_for", "FieldProvider"]


FieldProvider = Callable[[VehicleState], tuple[int, int, int]]


def _clamp(value: float, low: float = 0.0, high: float = 255.0) -> int:
    return int(max(low, min(high, round(value))))


# --- Umrechnungshelfer (invertierte Formeln) -------------------------------- #
def p_rpm(state: VehicleState) -> tuple[int, int, int]:
    return (1, _clamp(state.rpm / 50.0), 250)


def p_temp(value: float) -> FieldProvider:
    """Fester Temperaturwert in Grad Celsius."""
    return lambda state: (5, 10, _clamp(value))


def p_temp_coolant(state: VehicleState) -> tuple[int, int, int]:
    return (5, 10, _clamp(state.coolant_temp + 100))


def p_temp_intake(state: VehicleState) -> tuple[int, int, int]:
    return (5, 10, _clamp(state.intake_temp + 100))


def p_temp_oil(state: VehicleState) -> tuple[int, int, int]:
    return (5, 10, _clamp(state.oil_temp + 100))


def p_temp_fuel(state: VehicleState) -> tuple[int, int, int]:
    return (5, 10, _clamp(state.fuel_temp + 100))


def p_temp_ambient(state: VehicleState) -> tuple[int, int, int]:
    return (5, 10, _clamp(state.ambient_temp + 100))


def p_temp_outside(state: VehicleState) -> tuple[int, int, int]:
    return (5, 10, _clamp(state.outside_temp + 100))


def p_temp_interior(state: VehicleState) -> tuple[int, int, int]:
    return (5, 10, _clamp(state.interior_temp + 100))


def p_speed(state: VehicleState) -> tuple[int, int, int]:
    return (7, _clamp(state.speed_kmh), 100)


def p_volt(value: float | None = None) -> FieldProvider:
    if value is None:
        return lambda state: (6, _clamp(state.battery_v * 10), 100)
    return lambda state: (6, _clamp(value * 10), 100)


def p_percent(value: float | None = None) -> FieldProvider:
    if value is None:
        return lambda state: (2, 250, _clamp(state.throttle_percent * 2))
    return lambda state: (2, 250, _clamp(value * 2))


def p_mg(value: float | None = None) -> FieldProvider:
    if value is None:
        return lambda state: (39, 250, _clamp(state.injection_mg * 1.024))
    return lambda state: (39, 250, _clamp(value * 1.024))


def _mbar_triple(mbar: float) -> tuple[int, int, int]:
    """Sensortyp 18 (Druck klein, ``0.04*A*B`` mbar) fuer Druckwerte in mbar."""
    return (18, 250, _clamp(mbar / 10.0))


def p_mbar(value: float | None = None) -> FieldProvider:
    if value is None:
        return lambda state: _mbar_triple(state.boost_mbar)
    return lambda state: _mbar_triple(float(value))


def p_mbar_from_state(state: VehicleState) -> tuple[int, int, int]:
    """Ladedruck-Sollwert aus dem Fahrzustand."""
    return _mbar_triple(state.boost_target_mbar)


def p_boost_bar(value: float | None = None) -> FieldProvider:
    """Ladedruck in bar (Sensortyp 14, ``0.005*A*B``) - Benziner-Labeldateien."""
    def provider(state: VehicleState) -> tuple[int, int, int]:
        mbar = state.boost_mbar if value is None else float(value)
        return (14, 2, _clamp(mbar / 10.0))

    return provider


def p_boost_bar_from_state(state: VehicleState) -> tuple[int, int, int]:
    """Ladedruck-Sollwert in bar (Benziner)."""
    return (14, 2, _clamp(state.boost_target_mbar / 10.0))


def p_g(value: float) -> FieldProvider:
    """Massenwert in Gramm (Sensortyp 8, Einheit kommt aus der Labeldatei)."""
    def provider(state: VehicleState) -> tuple[int, int, int]:
        target = max(0.0, float(value))
        a = max(1, int(round(target / 24.0)))
        b = int(round(target / (0.1 * a)))
        return (8, _clamp(a), _clamp(b))

    return provider


def p_deg(value: float | None = None) -> FieldProvider:
    if value is None:
        return lambda state: (4, 10, _clamp(127 + state.injection_deg * 10))
    return lambda state: (4, 10, _clamp(127 + value * 10))


def p_liter(value: float | None = None) -> FieldProvider:
    if value is None:
        return lambda state: (19, 22, _clamp(state.fuel_level_l / 0.22))
    return lambda state: (19, 22, _clamp(value / 0.22))


def p_km(value: float | None = None) -> FieldProvider:
    if value is None:
        return lambda state: (36, _clamp(state.odo_km / 2560), _clamp((state.odo_km % 2560) / 10))
    value = float(value)
    return lambda state: (36, _clamp(value / 2560), _clamp((value % 2560) / 10))


def p_ratio(value: float | None = None) -> FieldProvider:
    """Verhaeltnis 0..1 (Sensortyp 59: (a*256 + b) / 32768)."""
    def make(target: float) -> FieldProvider:
        raw = int(target * 32768)

        def provider(state: VehicleState) -> tuple[int, int, int]:
            return (59, (raw >> 8) & 0xFF, raw & 0xFF)

        return provider

    if value is None:
        return lambda state: make(state.lambda_value)(state)
    return make(float(value))


def p_bar(value: float) -> FieldProvider:
    """Druckwert in bar (Sensortyp 69, ``(256*A+B)*0.3254`` bar)."""
    return lambda state: _bar16_triple(float(value))


def p_count(value: float) -> FieldProvider:
    value_int = int(value)
    return lambda state: (54, (value_int >> 8) & 0xFF, value_int & 0xFF)


def p_status(value: int) -> FieldProvider:
    """Reiner Statuswert (Sensortyp 8: 0.1 * a * b)."""
    return lambda state: (8, 1, _clamp(value * 10))


def p_bitfield(mask: int) -> FieldProvider:
    return lambda state: (16, (mask >> 8) & 0xFF, mask & 0xFF)


def p_text(text: str) -> FieldProvider:
    a = ord(text[0]) if text else 0x20
    b = ord(text[1]) if len(text) > 1 else 0x20
    return lambda state: (17, a, b)


def p_amp(value: float) -> FieldProvider:
    return lambda state: (24, _clamp(1), _clamp(value * 1000))


def p_ohm(value: float) -> FieldProvider:
    return lambda state: (12, 8, _clamp(value * 125))


def p_nm(value: float) -> FieldProvider:
    return lambda state: (52, _clamp(1), _clamp(value / 0.02))


def p_deg_per_s(value: float) -> FieldProvider:
    value_int = int(value * 7.365)
    return lambda state: (68, (value_int >> 8) & 0xFF, value_int & 0xFF)


def p_rpm_small(value: float) -> FieldProvider:
    return lambda state: (58, 0, _clamp(value / 1.0225))


@dataclass
class Field:
    """Ein Messwert innerhalb einer Gruppe."""

    text: str
    provider: FieldProvider
    unit: str = ""


@dataclass
class Group:
    """Ein Messwerteblock."""

    group: int
    title: str
    fields: list[Field]
    basic_setting: bool = False


def _g(group: int, title: str, *fields: Field, basic: bool = False) -> Group:
    return Group(group=group, title=title, fields=list(fields), basic_setting=basic)


# --- Hilfsprovider fuer den Fahrzustand ------------------------------- #
# Hilfsprovider, die den Fahrzustand auswerten
def _egr_percent(state: VehicleState) -> tuple[int, int, int]:
    return (2, 250, _clamp(state.egr_percent * 2))


def _consumption(state: VehicleState) -> tuple[int, int, int]:
    lph = state.injection_mg * state.rpm * 3.0e-5 * 4
    return (35, 4, _clamp(lph * 25))


def _bar16_triple(bar: float) -> tuple[int, int, int]:
    """Sensortyp 69 (Druck 16 Bit, ``(256*A+B)*0.3254`` bar)."""
    raw = max(0, int(round(max(0.0, bar) / 0.3254)))
    return (69, _clamp(raw >> 8), _clamp(raw & 0xFF))


def _rail_target(state: VehicleState) -> tuple[int, int, int]:
    return _bar16_triple(state.rail_pressure_bar)


def _rail_actual(state: VehicleState) -> tuple[int, int, int]:
    return _bar16_triple(state.rail_pressure_bar + 12)


def _wheel(index: int) -> FieldProvider:
    return lambda state: (7, _clamp(state.wheel_speed.get(index, 0.0)), 100)


def _brake_switch(state: VehicleState) -> tuple[int, int, int]:
    return (8, 1, 10 if state.brake_switch else 0)


def _handbrake(state: VehicleState) -> tuple[int, int, int]:
    return (8, 1, 10 if state.handbrake else 0)


def _brake_pressure(state: VehicleState) -> tuple[int, int, int]:
    return (2, 25, _clamp(state.brake_pressure_bar * 2))


def _steering_angle(state: VehicleState) -> tuple[int, int, int]:
    angle = state.steering_angle
    raw = int(abs(angle) * 10) + 1
    return (9, _clamp(raw / 2), 127 + (10 if angle >= 0 else 0))


def _yaw_rate(state: VehicleState) -> tuple[int, int, int]:
    value = int(abs(state.yaw_rate) * 100)
    return (9, _clamp(value / 2), 127 + (10 if state.yaw_rate >= 0 else 0))


def _lateral_g(state: VehicleState) -> tuple[int, int, int]:
    return (8, 1, _clamp(state.lateral_g * 1000))


def _trip_km(state: VehicleState) -> tuple[int, int, int]:
    return (7, _clamp(state.trip_km), 100)


def _door(index: int) -> FieldProvider:
    return lambda state: (8, 1, 10 if state.doors.get(index) else 0)


def _frequency(state: VehicleState) -> tuple[int, int, int]:
    mhz = 97.3
    return (31, _clamp(mhz * 25.6), 0)



# --- Provider fuer den Benziner (ME7.5) ------------------------------------- #
def p_temp_intake(state: VehicleState) -> tuple[int, int, int]:
    """Ansauglufttemperatur (Sensorart 5: a * (b - 100) * 0.1)."""
    return (5, 10, _clamp(state.intake_temp + 100))


def p_ignition(state: VehicleState) -> tuple[int, int, int]:
    """Zuendwinkel in Grad Kurbelwelle (Sensorart 3: 0.002 * a * b)."""
    angle = 9.0 + state.throttle_percent * 0.22 + max(0.0, (state.rpm - 800.0) / 700.0)
    return (3, _clamp(angle * 2), 250)


def p_injection_time(state: VehicleState) -> tuple[int, int, int]:
    """Einspritzzeit in ms (Sensorart 15: 0.01 * a * b, b = 100)."""
    return (15, _clamp(1.2 + state.injection_mg * 0.85), 100)


def p_air_mass(state: VehicleState) -> tuple[int, int, int]:
    """Luftmasse in g/s (Sensorart 25: b * 1.421 + a / 182)."""
    grams = state.maf_mgh * state.rpm / 30000.0
    b = int(grams / 1.421)
    a = round((grams - b * 1.421) * 182.0)
    return (25, _clamp(a), _clamp(b))


def p_lambda_voltage(state: VehicleState) -> tuple[int, int, int]:
    """Lambdaspannung der Sprungsonde in V (Sensorart 6, b = 100)."""
    volts = 0.45 + (state.lambda_value - 1.0) * 2.2
    return (6, _clamp(volts * 10), 100)


def p_lambda_ratio(state: VehicleState) -> tuple[int, int, int]:
    """Lambdaverhaeltnis (Sensorart 31: b / 2560 * a, b = 128)."""
    return (31, _clamp(state.lambda_value * 20), 128)


# --- Rollen ----------------------------------------------------------------- #
ROLES: dict[str, list[Group]] = {
    # Motorsteuergeraet, 1.9 TDI mit Verteilerpumpe (KWP1281)
    "engine_vp37": [
        _g(1, "Grundeinstellung (Leerlauf)",
           Field("Motordrehzahl", p_rpm),
           Field("Einspritzmenge", p_mg()),
           Field("Spannung Modulationskolbenweg", p_volt(1.2)),
           Field("Kühlmitteltemperatur", p_temp_coolant)),
        _g(2, "Leerlaufdrehzahl",
           Field("Motordrehzahl", p_rpm),
           Field("Gaspedalstellung", p_percent()),
           Field("Betriebszustand", p_status(4)),
           Field("Kühlmitteltemperatur", p_temp_coolant)),
        _g(3, "Abgasrückführung (AGR)",
           Field("Motordrehzahl", p_rpm),
           Field("Luftmasse (Ist)", p_mg(280.0)),
           Field("Luftmasse (Soll)", p_mg(300.0)),
           Field("AGR-Ventil Ansteuerung", _egr_percent)),
        _g(4, "Einspritzbeginn",
           Field("Motordrehzahl", p_rpm),
           Field("Einspritzbeginn (Ist)", p_deg()),
           Field("Einspritzbeginn (Soll)", p_deg(2.0)),
           Field("Förderdauer", p_deg(4.5))),
        _g(10, "Kraftstoffsystem",
           Field("Luftmasse (Ist)", p_mg()),
           Field("Absoluter Saugrohrdruck", p_mbar()),
           Field("Umgebungsdruck", p_mbar(1000.0)),
           Field("Gaspedalstellung", p_percent())),
        _g(11, "Ladedruckregelung",
           Field("Motordrehzahl", p_rpm),
           Field("Ladedruck (Soll)", p_mbar_from_state),
           Field("Ladedruck (Ist)", p_mbar()),
           Field("N75-Ansteuerung", p_percent(45.0))),
        _g(13, "Leerlaufstabilisierung",
           Field("Einspritzmenge (Ist)", p_mg()),
           Field("Einspritzmenge (Soll)", p_mg(5.0)),
           Field("Korrektur Zylinder 1", p_mg(0.2)),
           Field("Korrektur Zylinder 2", p_mg(0.1))),
        _g(15, "Kraftstoffverbrauch",
           Field("Motordrehzahl", p_rpm),
           Field("Einspritzmenge", p_mg()),
           Field("Verbrauch", _consumption),
           Field("Kraftstofftemperatur", p_temp_fuel)),
    ],
    # Motorsteuergeraet Pumpe-Duese (KWP2000)
    "engine_pd": [
        _g(1, "Einspritzmenge",
           Field("Motordrehzahl", p_rpm),
           Field("Einspritzmenge", p_mg()),
           Field("Einspritzdauer", p_injection_time),
           Field("Kühlmitteltemperatur", p_temp_coolant)),
        _g(3, "Abgasrückführung",
           Field("Motordrehzahl", p_rpm),
           Field("Luftmasse (Ist)", p_mg()),
           Field("AGR-Ventil Ansteuerung", _egr_percent),
           Field("AGR-Ventil Stellung", _egr_percent)),
        _g(11, "Ladedruckregelung",
           Field("Motordrehzahl", p_rpm),
           Field("Ladedruck (Soll)", p_mbar_from_state),
           Field("Ladedruck (Ist)", p_mbar()),
           Field("N75-Ansteuerung", p_percent(45.0))),
    ],
    # Motorsteuergeraet 2.0 TDI mit Pumpe-Duese und Partikelfilter (UDS)
    "engine_cr": [
        _g(1, "Einspritzmenge",
           Field("Motordrehzahl", p_rpm),
           Field("Einspritzmenge", p_mg()),
           Field("Einspritzdauer", p_injection_time),
           Field("Kühlmitteltemperatur", p_temp_coolant)),
        _g(2, "Ladedruck",
           Field("Motordrehzahl", p_rpm),
           Field("Ladedruck (Soll)", p_mbar_from_state),
           Field("Ladedruck (Ist)", p_mbar()),
           Field("Ladelufttemperatur", p_temp_intake)),
        _g(4, "Partikelfilter",
           Field("Aschemasse", p_g(48.0)),
           Field("Russmasse berechnet", p_g(12.0)),
           Field("Differenzdruck", p_mbar(45.0)),
           Field("Strecke seit Regeneration", p_km(238.0))),
        _g(5, "Kraftstoffsystem",
           Field("Raildruck (Soll)", _rail_target),
           Field("Raildruck (Ist)", _rail_actual),
           Field("Kraftstofftemperatur", p_temp_fuel),
           Field("Druckregelventil", p_percent(38.0))),
    ],
    "abs": [
        _g(1, "Raddrehzahlen",
           Field("Raddrehzahl vorne links", _wheel(0)),
           Field("Raddrehzahl vorne rechts", _wheel(1)),
           Field("Raddrehzahl hinten links", _wheel(2)),
           Field("Raddrehzahl hinten rechts", _wheel(3))),
        _g(2, "Bremseneingang",
           Field("Bremslichtschalter", _brake_switch),
           Field("Handbremsschalter", _handbrake),
           Field("ABS-Warnleuchte", p_status(0)),
           Field("ESP-Taster", p_status(0))),
        _g(3, "Bremsdruck und Lenkwinkel",
           Field("Bremsdruck", _brake_pressure),
           Field("Lenkwinkel", _steering_angle),
           Field("Gierrate", _yaw_rate),
           Field("Querbeschleunigung", _lateral_g)),
        _g(6, "Sensorversorgung",
           Field("Versorgungsspannung", p_volt()),
           Field("Temperatur Steuergerät", p_temp(45.0)),
           Field("Status ABS", p_status(1)),
           Field("Status ESP", p_status(1))),
    ],
    "airbag": [
        _g(1, "Airbag-System Front",
           Field("Zündkreis Fahrerairbag", p_ohm(2.2)),
           Field("Zündkreis Beifahrerairbag", p_ohm(2.2)),
           Field("Zündkreis Fahrergurtstraffer", p_ohm(2.1)),
           Field("Zündkreis Beifahrergurtstraffer", p_ohm(2.1))),
        _g(2, "Seitenairbags",
           Field("Seitenairbag Fahrer", p_ohm(2.3)),
           Field("Seitenairbag Beifahrer", p_ohm(2.3)),
           Field("Kopfairbag Fahrer", p_ohm(2.4)),
           Field("Kopfairbag Beifahrer", p_ohm(2.4))),
        _g(3, "Systemzustand",
           Field("Bordspannung", p_volt()),
           Field("Airbag-Warnleuchte", p_status(0)),
           Field("Crash-Auslösung", p_status(0)),
           Field("Anzahl Auslösungen", p_count(0.0))),
    ],
    "cluster": [
        _g(1, "Kombiinstrument Basis",
           Field("Motordrehzahl", p_rpm),
           Field("Geschwindigkeit", p_speed),
           Field("Kühlmitteltemperatur", p_temp_coolant),
           Field("Tankfüllstand", p_liter())),
        _g(2, "Bordnetz",
           Field("Bordspannung", p_volt()),
           Field("Außentemperatur", p_temp_outside),
           Field("Öldruck-Warnung", p_status(0)),
           Field("Kraftstoffwarnung", p_status(1))),
        _g(3, "Betriebsstunden und Wegstrecke",
           Field("Kilometerstand", p_km()),
           Field("Tageskilometerzähler", _trip_km),
           Field("Betriebsstunden", p_count(1240.0)),
           Field("Ölwechselintervall", p_km(9500.0))),
        _g(4, "Wegfahrsperre",
           Field("WFS-Status", p_status(1)),
           Field("Schlüsselnummer", p_status(1)),
           Field("Anzahl gespeicherte Schlüssel", p_status(2)),
           Field("Startfreigabe", p_status(1))),
    ],
    "comfort": [
        _g(1, "Zentralverriegelung",
           Field("Fahrertür", _door(0)),
           Field("Beifahrertür", _door(1)),
           Field("Hintertür links", _door(2)),
           Field("Hintertür rechts", _door(3))),
        _g(2, "Funkfernbedienung",
           Field("Taste Öffnen", p_status(0)),
           Field("Taste Schließen", p_status(0)),
           Field("Taste Kofferraum", p_status(0)),
           Field("Schlüsselnummer", p_status(1))),
        _g(3, "Diebstahlwarnanlage",
           Field("DWA aktiv", p_status(0)),
           Field("Innenraumüberwachung", p_status(0)),
           Field("Alarmauslösungen", p_count(0.0)),
           Field("Alarmursache", p_status(0))),
        _g(4, "Fensterheber",
           Field("Fensterheber Fahrer", p_percent(100.0)),
           Field("Fensterheber Beifahrer", p_percent(100.0)),
           Field("Komfortschließen", p_status(1)),
           Field("Blockierschutz", p_status(1))),
    ],
    "hvac": [
        _g(1, "Klimaanlage Grundwerte",
           Field("Innenraumtemperatur", p_temp_interior),
           Field("Außentemperatur", p_temp_outside),
           Field("Verdampfertemperatur", p_temp(4.0)),
           Field("Kühlmitteltemperatur", p_temp_coolant)),
        _g(2, "Kompressor und Lüfter",
           Field("Kompressorstatus", p_status(1)),
           Field("Kältemitteldruck", p_bar(8.0)),
           Field("Lüfterstufe", p_status(3)),
           Field("Gebläsespannung", p_volt(7.5))),
        _g(3, "Stellmotoren",
           Field("Stellmotor Temperaturklappe", p_percent(45.0)),
           Field("Stellmotor Luftverteilung", p_percent(60.0)),
           Field("Stellmotor Umluftklappe", p_percent(0.0)),
           Field("Stellmotor Frischluftklappe", p_percent(100.0))),
    ],
    "gateway": [
        _g(1, "Gateway-Verbauinformation",
           Field("Verbauliste Motor", p_status(1)),
           Field("Verbauliste ABS", p_status(1)),
           Field("Verbauliste Kombi", p_status(1)),
           Field("Verbauliste Airbag", p_status(1))),
        _g(2, "CAN-Bus-Status",
           Field("Antriebs-CAN", p_status(0)),
           Field("Komfort-CAN", p_status(0)),
           Field("Infotainment-CAN", p_status(0)),
           Field("Busruhe", p_status(0))),
        _g(3, "Bordnetz",
           Field("Bordspannung", p_volt()),
           Field("Zündung", p_status(1)),
           Field("Klemme 15", p_status(1)),
           Field("Klemme 30", p_status(1))),
    ],
    "radio": [
        _g(1, "Radio-Status",
           Field("Klemme 30", p_volt()),
           Field("Klemme 15", p_status(1)),
           Field("Diebstahlschutz", p_status(1)),
           Field("Senderspeicher", p_status(12))),
        _g(2, "Tuner",
           Field("Frequenz", _frequency),
           Field("Empfangspegel", p_status(48)),
           Field("Stereo", p_status(1)),
           Field("RDS", p_status(1))),
    ],
    "steering": [
        _g(1, "Lenkhilfe Basiswerte",
           Field("Motordrehzahl", p_rpm),
           Field("Fahrgeschwindigkeit", p_speed),
           Field("Lenkmoment", p_nm(1.5)),
           Field("Lenkwinkel", _steering_angle)),
        _g(2, "Unterstützung",
           Field("Unterstützungsmoment (Ist)", p_nm(8.0)),
           Field("Unterstützungsmoment (Soll)", p_nm(8.5)),
           Field("Motordrehzahl E-Motor", p_rpm_small(1200.0)),
           Field("Motorstrom", p_amp(22.0))),
        _g(3, "Systemzustand",
           Field("Bordspannung", p_volt()),
           Field("Temperatur Steuergerät", p_temp(58.0)),
           Field("Lenkwinkelsensor", p_status(1)),
           Field("Störungsanzeige", p_status(0))),
    ],
    "bcm": [
        _g(1, "Bordnetzsteuergerät",
           Field("Bordspannung", p_volt()),
           Field("Klemme 15", p_status(1)),
           Field("Standlicht", p_status(1)),
           Field("Warnblinker", p_status(0))),
        _g(2, "Beleuchtung",
           Field("Abblendlicht links", p_status(1)),
           Field("Abblendlicht rechts", p_status(1)),
           Field("Innenraumlicht", p_status(0)),
           Field("Tagfahrlicht", p_status(1))),
    ],
    "generic": [
        _g(1, "Messwerteblock 001",
           Field("Messwert 1", p_status(12)),
           Field("Messwert 2", p_status(24)),
           Field("Messwert 3", p_status(48)),
           Field("Messwert 4", p_status(96))),
    ],
}


# Motorsteuergeraet 1.8l 20V Turbo (ME7.5, Audi A4 B6)
ROLES["engine_petrol"] = [
    _g(1, "Motordrehzahl, Last, Kühlmitteltemperatur, Lambdaregelung",
       Field("Motordrehzahl", p_rpm),
       Field("Last / Füllung", p_percent()),
       Field("Kühlmitteltemperatur", p_temp_coolant),
       Field("Lambdaspannung Bank 1", p_lambda_voltage)),
    _g(2, "Ansauglufttemperatur, Zuendwinkel, Einspritzzeit, Luftmasse",
       Field("Ansauglufttemperatur", p_temp_intake),
       Field("Zündwinkel", p_ignition),
       Field("Einspritzzeit", p_injection_time),
       Field("Luftmasse (Ist)", p_air_mass)),
    _g(3, "Ladedruck, Drosselklappenwinkel, Lambda, Bordnetzspannung",
       Field("Ladedruck (Ist)", p_boost_bar()),
       Field("Drosselklappenwinkel", p_percent()),
       Field("Lambdaverhaeltnis", p_lambda_ratio),
       Field("Bordnetzspannung", p_volt())),
    _g(10, "Geschwindigkeit, Drehzahl, Ladedruck (Soll/Ist)",
       Field("Geschwindigkeit", p_speed),
       Field("Motordrehzahl", p_rpm),
       Field("Ladedruck (Soll)", p_boost_bar_from_state),
       Field("Ladedruck (Ist)", p_boost_bar())),
]


ROLE_BY_ADDRESS: dict[int, str] = {
    1: "engine_vp37",
    3: "abs",
    8: "hvac",
    9: "bcm",
    15: "airbag",
    17: "cluster",
    19: "gateway",
    44: "steering",
    46: "comfort",
    56: "radio",
}


def role_for(address: int, dtc_set: str = "") -> str:
    """Ermittelt die Rolle eines Steuergeraets (Adresse bzw. Fehlersatzkennung)."""
    if dtc_set.startswith("b55_tdi"):
        return "engine_pd"
    if dtc_set.startswith(("golf5_tdi",)):
        return "engine_cr"
    if dtc_set.startswith(("a4b6",)):
        # A4 B6 1.8T = Benziner (ME7.5), alle anderen Adressen wie ueblich
        return "engine_petrol" if address == 1 else ROLE_BY_ADDRESS.get(address, "generic")
    return ROLE_BY_ADDRESS.get(address, "generic")


# --- Fehlersaetze ---------------------------------------------------------- #
# Jeder Eintrag: (Kennung, Fehlercode, Statusbyte, Art)
#   Art "vag" = 5-stelliger VAG-Code (KWP1281), "sae" = SAE-Code (KWP2000/UDS)
FAULT_SETS: dict[str, list[tuple[int | str, int]]] = {
    "golf4_tdi": [(575, 0x91), (522, 0x11)],
    "golf4_abs": [(283, 0x91), (1044, 0x11)],
    "golf4_airbag": [(655, 0x11)],
    "golf4_kombi": [(1176, 0x91)],
    "golf4_komfort": [(1314, 0x11)],
    "golf4_klima": [],
    "b55_tdi": [(16683, 0x21), (16795, 0x31)],  # 16683 ~ P0299, 16795 ~ P0411
    "b55_abs": [(287, 0x91)],
    "b55_airbag": [],
    "b55_kombi": [(1317, 0x11)],
    "b55_gateway": [("U0121", 0x08)],
    "b55_radio": [],
    "a4b6_18t": [("P0420", 0x2F), ("P1127", 0x21)],
    "a4b6_abs": [("C0035", 0x28)],
    "a4b6_klima": [],
    "a4b6_airbag": [],
    "a4b6_kombi": [],
    "a4b6_komfort": [],
    "golf5_tdi": [("P2463", 0x2F), ("P2004", 0x21), ("P0299", 0x08)],
    "golf5_abs": [("C0040", 0x28)],
    "golf5_bcm": [],
    "golf5_airbag": [],
    "golf5_kombi": [],
    "golf5_gateway": [("U0100", 0x08)],
    "golf5_lenkhilfe": [],
    "": [],
}


def fault_set(dtc_set: str) -> list[tuple[int | str, int]]:
    return list(FAULT_SETS.get(dtc_set, []))
