"""Tests fuer Stellglieddiagnose und Grundeinstellung.

VCDS kann Stellglieder einzeln ansteuern ("Stellglieddiagnose") und
Messwertebloecke in der Grundeinstellung lesen. Beides laeuft ueber alle
drei Transportwege (KWP1281 auf der K-Line, KWP2000 und UDS ueber VW TP 2.0)
und wird hier gegen den Fahrzeugsimulator geprueft.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import contextlib
import io

from vagdiag.cli import main as cli_main
from vagdiag.client import VehicleSession
from vagdiag.db import Database

GOLF4 = "VW Golf IV 1.9 TDI (2001)"
PASSAT = "VW Passat B5.5 1.9 TDI (2003)"
A4 = "Audi A4 B6 1.8T (2003)"
GOLF5 = "VW Golf V 2.0 TDI (2005)"


class StellgliedTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "stellglied.sqlite3")
        cls.db.build(force=True)

    def session_for(self, vehicle: str) -> VehicleSession:
        session = VehicleSession(
            db=self.db, vehicle_name=vehicle, drive=False, speed=1.0, clock_speed=0.0
        )
        session.start()
        self.addCleanup(session.stop)
        return session

    def ecu_module(self, session: VehicleSession, address: int = 1):
        """Simuliertes Steuergeraet - zeigt den Zustand hinter dem Dialog."""
        return session.vehicle.ecu(address).module


class StellgliedlisteTest(StellgliedTestBase):
    def test_liste_kommt_aus_der_labeldatei(self) -> None:
        for vehicle in (GOLF4, GOLF5, PASSAT, A4):
            with self.subTest(vehicle=vehicle):
                session = self.session_for(vehicle)
                with session.open_module(1) as module:
                    entries = module.actuators()
                    self.assertGreaterEqual(len(entries), 6)
                    self.assertEqual([entry["step"] for entry in entries],
                                     list(range(1, len(entries) + 1)))
                    self.assertTrue(all(entry["text"] for entry in entries))
                    self.assertFalse(any(entry["active"] for entry in entries))

    def test_abs_hat_eigene_stellglieder(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(3) as module:  # ABS/ESP
            entries = module.actuators()
            self.assertEqual(len(entries), 4)
            self.assertEqual(entries[0]["text"], "ABS-Warnleuchte")


class StellgliedAnsteuerungTest(StellgliedTestBase):
    def test_kwp1281_kline(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            result = module.output_test(2)
            self.assertEqual(result["actuator"], 2)
            self.assertIn("Kühlerlüfter", result["text"])
            self.assertEqual(result["response"], "02 00")
            self.assertEqual(module.actuator_active, 2)

            # Ausgangszustand im Steuergeraet und in der Anzeige
            self.assertEqual(self.ecu_module(session).actuator_active, 2)
            self.assertTrue([e for e in module.actuators() if e["active"]][0]["id"] == 2)

            result = module.output_test(0)
            self.assertEqual(result["response"], "00 00")
            self.assertEqual(self.ecu_module(session).actuator_active, 0)
            self.assertEqual(module.actuator_active, 0)

    def test_kwp2000_ueber_vw_tp(self) -> None:
        session = self.session_for(A4)
        with session.open_module(1) as module:
            self.assertEqual(module.output_test(4)["response"], "Stellglied 04 geschaltet")
            self.assertEqual(self.ecu_module(session).actuator_active, 4)
            module.output_test(0)
            self.assertEqual(self.ecu_module(session).actuator_active, 0)

    def test_uds_ueber_vw_tp(self) -> None:
        session = self.session_for(GOLF5)
        with session.open_module(1) as module:
            module.output_test(3)
            self.assertEqual(self.ecu_module(session).actuator_active, 3)
            self.assertEqual(module.actuator_active, 3)
            module.output_test(0)
            self.assertEqual(self.ecu_module(session).actuator_active, 0)

    def test_kurzform_bleibt_erhalten(self) -> None:
        session = self.session_for(PASSAT)
        with session.open_module(1) as module:
            self.assertEqual(module.actuator_test(1)["actuator"], 1)


class GrundeinstellungTest(StellgliedTestBase):
    def test_bloecke_aus_der_labeldatei(self) -> None:
        session = self.session_for(GOLF5)
        with session.open_module(1) as module:
            groups = [entry["group"] for entry in module.basic_settings()]
            self.assertIn(4, groups)
            text = [entry["text"] for entry in module.basic_settings() if entry["group"] == 4][0]
            self.assertIn("Partikelfilter", text)

    def test_grundeinstellung_starten_und_beenden(self) -> None:
        session = self.session_for(GOLF5)
        with session.open_module(1) as module:
            info = module.basic_setting(4)
            self.assertTrue(info["active"])
            self.assertEqual(module.basic_setting_group, 4)
            self.assertEqual(self.ecu_module(session).basic_setting_group, 4)
            self.assertIn("Partikelfilter", info["text"])
            self.assertEqual(info["block"]["basic"], True)
            self.assertEqual(len(info["block"]["fields"]), 4)

            info = module.basic_setting(4, on=False)
            self.assertFalse(info["active"])
            self.assertEqual(module.basic_setting_group, 0)
            self.assertEqual(self.ecu_module(session).basic_setting_group, 0)

    def test_kline_grundeinstellung(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            info = module.basic_setting(1)
            self.assertTrue(info["active"])
            self.assertIn("Grundeinstellung", info["block"]["title"])
            self.assertEqual(module.basic_setting_group, 1)

            info = module.basic_setting(1, on=False)
            self.assertFalse(info["active"])
            # Im Normalbetrieb liest derselbe Block die Messwerte
            self.assertEqual(info["block"]["basic"], False)
            self.assertNotIn("Grundeinstellung", info["block"]["title"])

    def test_unbekannter_block(self) -> None:
        session = self.session_for(GOLF5)
        with session.open_module(1) as module:
            with self.assertRaises(Exception):
                module.basic_setting(99)


class StellgliedKommandoTest(StellgliedTestBase):
    """Das Kommando `vagdiag actuator` - Liste, Ansteuerung, Grundeinstellung."""

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                [
                    "--db", str(self.db.path),
                    "--vehicle", GOLF4,
                    "--clock-speed", "0",
                    "actuator",
                    *arguments,
                ]
            )
        return code, buffer.getvalue()

    def test_liste_und_ansteuerung(self) -> None:
        code, text = self.run_cli("--address", "01", "--test", "2")
        self.assertEqual(code, 0)
        self.assertIn("Stellglieddiagnose Adresse 01", text)
        self.assertIn("02  Kühlerlüfter", text)
        self.assertIn("02 00", text)

    def test_abschalten_und_reihenfolge(self) -> None:
        code, text = self.run_cli("--address", "01", "--release")
        self.assertEqual(code, 0)
        self.assertIn("00 00", text)

    def test_grundeinstellung_ueber_die_kommandozeile(self) -> None:
        code, text = self.run_cli("--address", "01", "--basic", "1")
        self.assertEqual(code, 0)
        self.assertIn("Grundeinstellung Block 001", text)
        self.assertIn("Messwerteblock 001", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
