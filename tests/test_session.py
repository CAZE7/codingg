"""Integrationstests gegen den Fahrzeugsimulator.

Die Tests fahren dieselben Protokolldialoge wie die echten Diagnosesitzungen
(KWP1281 auf der K-Line, KWP2000 und UDS ueber VW TP 2.0) - nur gegen den
virtuellen Bus und mit Zeitraffer, damit sie in Sekunden laufen.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vagdiag.client import VehicleSession
from vagdiag.db import Database
from vagdiag.obd.scan import format_autoscan

GOLF4 = "VW Golf IV 1.9 TDI (2001)"
PASSAT = "VW Passat B5.5 1.9 TDI (2003)"
A4 = "Audi A4 B6 1.8T (2003)"
GOLF5 = "VW Golf V 2.0 TDI (2005)"


class SimulatorTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "sitzung.sqlite3")
        cls.db.build(force=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        cls._tmp.cleanup()

    def session_for(self, vehicle: str) -> VehicleSession:
        session = VehicleSession(vehicle_name=vehicle, db=self.db, clock_speed=0.0, drive=False)
        self.addCleanup(session.stop)
        session.start()
        return session


class KLineSessionTest(SimulatorTestBase):
    """Golf IV: KWP1281-Blockdialog auf der K-Line."""

    def test_identifikation_und_bloecke(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            ident = module.ident()
            self.assertEqual(ident["part_number"], "038906018G")
            self.assertIn("1.9l R4 EDC", ident["component"])
            self.assertEqual(ident["coding_text"], "00002")
            block = module.read_block(1)
            self.assertEqual(block["group"], 1)
            self.assertEqual(len(block["fields"]), 4)
            texts = [field["text"] for field in block["fields"]]
            self.assertIn("Motordrehzahl", texts)

    def test_adressplan_und_fehlerspeicher(self) -> None:
        session = self.session_for(GOLF4)
        addresses = [module.address for module in session.module_list()]
        self.assertEqual(addresses, sorted(addresses))
        self.assertIn(1, addresses)
        with session.open_module(1) as module:
            faults = module.read_faults()
            codes = {fault.display_code for fault in faults}
            self.assertEqual(codes, {"00575", "00522"})
            self.assertIn("17-10", faults[0].status_str)

    def test_fehlerspeicher_loeschen(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(15) as module:
            self.assertTrue(module.read_faults())
            module.clear_faults()
            self.assertEqual(module.read_faults(), [])

    def test_anpassung_lesen_und_schreiben(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            channels = [channel["channel"] for channel in module.adaptations()]
            self.assertEqual(channels, [3, 4, 5])
            before = module.adaptation_read(4)
            self.assertEqual(before["value"], 32150)
            module.adaptation_save(4, 32900)
        with session.open_module(1) as module:
            self.assertEqual(module.adaptation_read(4)["value"], 32900)

    def test_codierung_schreiben(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            module.login(12233)
            self.assertTrue(module.write_coding("00003"))
        with session.open_module(1) as module:
            self.assertEqual(module.coding()["coding"], "00003")


class VwtpSessionTest(SimulatorTestBase):
    """Golf V: UDS ueber VW TP 2.0 auf dem CAN."""

    def test_identifikation_und_codierung(self) -> None:
        session = self.session_for(GOLF5)
        with session.open_module(1) as module:
            ident = module.ident()
            self.assertEqual(ident["part_number"], "03G906016K")
            self.assertEqual(ident["coding_text"], "0000071")
        with session.open_module(3) as module:
            self.assertEqual(module.coding()["coding"], "0021505")

    def test_uds_fehlercodes_mit_statusbits(self) -> None:
        session = self.session_for(GOLF5)
        with session.open_module(1) as module:
            faults = module.read_faults()
            codes = [fault.display_code for fault in faults]
            self.assertIn("P2463", codes)
            p2463 = next(fault for fault in faults if fault.display_code == "P2463")
            self.assertEqual(p2463.status_style, "uds")
            self.assertIn("statisch", p2463.status_str)
            self.assertIn("Fehler aktiv", p2463.status_str)
            self.assertIn("Partikelfilter", p2463.text)

    def test_messwerteblock_und_anpassung(self) -> None:
        session = self.session_for(GOLF5)
        with session.open_module(1) as module:
            block = module.read_block(2)
            units = {field["text"]: field["unit"] for field in block["fields"]}
            self.assertEqual(units["Ladedruck (Ist)"], "mbar")
            self.assertFalse(module.read_block(5)["fields"][0].get("unit_hint"))
            channel = module.adaptation_read(1)
            self.assertEqual(channel["value"], 815)
            self.assertEqual(channel["description"], "Leerlaufdrehzahl")

    def test_viele_modulwechsel_hintereinander(self) -> None:
        """Die Weboberflaeche wechselt staendig das Steuergeraet."""
        session = self.session_for(GOLF5)
        for _ in range(3):
            for address in (1, 3, 9, 17, 19, 44):
                with session.open_module(address) as module:
                    self.assertTrue(module.ident()["part_number"])


class Kwp2000SessionTest(SimulatorTestBase):
    """A4 B6 (CAN) und Passat B5.5 (K-Line): KWP2000."""

    def test_a4_anpassung_und_bloecke(self) -> None:
        session = self.session_for(A4)
        with session.open_module(1) as module:
            channels = {channel["channel"]: channel["text"] for channel in module.adaptations()}
            self.assertEqual(channels, {1: "Leerlaufdrehzahl Sollwert", 3: "Gemischabgleich Leerlauf (Rohwert)"})
            self.assertEqual(module.adaptation_read(1)["value"], 792)
            block = module.read_block(1)
            self.assertEqual([field["text"] for field in block["fields"]][3], "Lambdaspannung Bank 1")

    def test_passat_bloecke_ueber_redir_label(self) -> None:
        session = self.session_for(PASSAT)
        with session.open_module(1) as module:
            blocks = module.block_numbers()
            self.assertGreaterEqual(len(blocks), 3)
            self.assertEqual(module.read_block(blocks[0])["group"], blocks[0])
            self.assertEqual(module.ident()["coding_text"], "00001")

    def test_autoscan_alle_fahrzeuge(self) -> None:
        for vehicle in (GOLF4, PASSAT, A4, GOLF5):
            with self.subTest(vehicle=vehicle):
                session = self.session_for(vehicle)
                result = session.autoscan()
                self.assertEqual(result.found, len(result.modules))
                self.assertGreaterEqual(result.fault_count, 1)
                report = format_autoscan(result)
                self.assertIn("Autoscan abgeschlossen", report)
                self.assertIn("Adresse 01:", report)
                self.assertEqual(len(report.splitlines()[0].split(",")), 5)


if __name__ == "__main__":
    unittest.main()
