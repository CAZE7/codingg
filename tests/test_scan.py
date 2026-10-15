"""Tests fuer den Autoscan-Bericht im VCDS-Textformat."""

from __future__ import annotations

import re
import unittest

from vagdiag.obd.dtc import DtcInfo
from vagdiag.obd.scan import (
    AutoscanResult,
    ModuleResult,
    build_scan_plan,
    format_autoscan,
    format_fault_list,
    vcds_timestamp,
)
from vagdiag.db import ModuleInfo

VAG_FAULT = DtcInfo(
    vag_code=575,
    status=0x91,
    text="Saugrohrdruck - Regelgrenze überschritten",
    text_short="Sporadisch",
    source="VAG",
)

UDS_FAULT = DtcInfo(
    sae_code="P0299",
    status=0x09,
    text="Ladedruckregelung - Regelgrenze unterschritten",
    status_style="uds",
)


class TimestampTest(unittest.TestCase):
    def test_format(self) -> None:
        # "Donnerstag,15,Oktober,2026,08:41:34:00000"
        stamp = vcds_timestamp()
        self.assertRegex(
            stamp,
            r"^(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag),"
            r"\d{1,2},(Januar|Februar|März|April|Mai|Juni|Juli|August|September|"
            r"Oktober|November|Dezember),\d{4},\d{2}:\d{2}:\d{2}:00000$",
        )


class ScanPlanTest(unittest.TestCase):
    def test_sortierung_und_uebernahme(self) -> None:
        modules = [
            ModuleInfo(address=46, name="Komfortsystem", protocol="KWP1281", bus="K-Line"),
            ModuleInfo(address=1, name="Motorsteuergerät", protocol="KWP1281", bus="K-Line"),
            ModuleInfo(address=17, name="Schalttafeleinsatz", protocol="KWP1281"),
        ]
        plan = build_scan_plan(modules)
        self.assertEqual([target.address for target in plan], [1, 17, 46])
        engine = plan[0]
        self.assertEqual(engine.name, "Motorsteuergerät")
        self.assertEqual(engine.bus, "K-Line")


class FaultListTest(unittest.TestCase):
    def test_kein_fehlercode(self) -> None:
        self.assertEqual(format_fault_list([]), "   Kein Fehlercode gefunden.")

    def test_fehlercode_mit_status(self) -> None:
        text = format_fault_list([VAG_FAULT])
        self.assertIn("1 Fehlercode(s) gefunden:", text)
        self.assertIn("00575 - 17-10", text)
        self.assertIn("Saugrohrdruck", text)

    def test_uds_fehlercode_ohne_kurztext(self) -> None:
        text = format_fault_list([UDS_FAULT])
        self.assertIn("P0299", text)
        self.assertIn("Ladedruckregelung", text)
        self.assertNotIn("Sporadisch", text)


class AutoscanReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.result = AutoscanResult(
            vehicle="VW Golf IV 1.9 TDI (2001)",
            vin="WVWZZZ1JZ27537500",
            started=1_700_000_000.0,
            finished=1_700_000_006.3,
            modules=[
                ModuleResult(
                    address=1,
                    name="Motorsteuergerät",
                    protocol="KWP1281",
                    bus="K-Line",
                    part_number="038906018G",
                    ident_lines=["038906018G  1.9l R4 EDC  G00SG  1087"],
                    label_file="038906018G.lbl",
                    coding="00002",
                    workshop_code="00000",
                    faults=[VAG_FAULT],
                    duration=1.2,
                ),
                ModuleResult(
                    address=3,
                    name="ABS/Bremsenelektronik",
                    protocol="KWP1281",
                    part_number="1J0907551AC",
                    ident_text="1J0907551AC  ABS/ESP  MK60  V1.1",
                    label_file="1J0907551AC.lbl",
                    coding="0021505",
                    duration=1.1,
                ),
                ModuleResult(
                    address=17,
                    name="Schalttafeleinsatz",
                    status="timeout",
                    message="Keine Antwort (Timeout).",
                    duration=2.0,
                ),
            ],
        )

    def test_kopfzeilen(self) -> None:
        report = format_autoscan(self.result)
        lines = report.splitlines()
        self.assertRegex(lines[0], r"^(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag),")
        self.assertIn("VAGDiag", report)
        self.assertIn("Fahrzeug: VW Golf IV 1.9 TDI (2001)", lines)
        self.assertIn("Fahrgestellnummer: WVWZZZ1JZ27537500", lines)
        self.assertIn("Adressplan:", lines)

    def test_modulbloecke(self) -> None:
        report = format_autoscan(self.result)
        self.assertIn("Adresse 01: Motorsteuergerät (038906018G)", report)
        self.assertIn("   Labeldatei: 038906018G.lbl", report)
        self.assertIn("   Codierung: 00002   Werkstattcode: 00000", report)
        self.assertIn("   1 Fehlercode(s) gefunden:", report)
        # Modul ohne Fehler
        self.assertIn("Adresse 03: ABS/Bremsenelektronik (1J0907551AC)", report)
        self.assertIn("   Kein Fehlercode gefunden.", report)
        # Nicht antwortendes Modul
        self.assertIn("Adresse 17: Schalttafeleinsatz", report)
        self.assertIn("   Keine Antwort (Timeout).", report)
        self.assertIn("   Wartezeit: 2.0 s", report)

    def test_fusszeile(self) -> None:
        report = format_autoscan(self.result)
        self.assertIn("Autoscan abgeschlossen in 6.3 s", report)
        self.assertIn("2/3 Steuergeraete antworten", report)
        self.assertIn("1 Fehlercode(s).", report)

    def test_protokoll_optional(self) -> None:
        without = format_autoscan(self.result)
        self.assertNotIn("[KWP1281", without)
        with_protocol = format_autoscan(self.result, show_protocol=True)
        self.assertIn("[KWP1281 / K-Line]", with_protocol)


if __name__ == "__main__":
    unittest.main()


class EinheitenTest(unittest.TestCase):
    """Einheiten aus Labeldatei und Sensorformel zusammenfuehren."""

    def test_kompatible_einheiten(self) -> None:
        from vagdiag.client import _units_compatible

        self.assertTrue(_units_compatible("°vor OT", "°nach OT"))
        self.assertTrue(_units_compatible("bar", "bar"))
        self.assertTrue(_units_compatible("", "bar"))
        self.assertTrue(_units_compatible("?", "bar"))
        self.assertFalse(_units_compatible("mbar", "bar"))
        self.assertFalse(_units_compatible("g", "mg/Hub"))
