"""Tests fuer Langcodierung (Bit-Ebene) und Kanalmap.

VCDS kann Codierungen bitweise aendern (Langcodierung) und alle Werte eines
Steuergeraets als Tabelle ausgeben (Kanalmap). Beides laeuft ueber alle
Transportwege und wird hier gegen den Fahrzeugsimulator geprueft.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from vagdiag.channelmap import COLUMNS, channel_map, to_csv, write_csv
from vagdiag.cli import main as cli_main
from vagdiag.client import ModuleSession, VehicleSession
from vagdiag.db import Database

GOLF4 = "VW Golf IV 1.9 TDI (2001)"
GOLF5 = "VW Golf V 2.0 TDI (2005)"
A4 = "Audi A4 B6 1.8T (2003)"


class BasisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "map.sqlite3")
        cls.db.build(force=True)

    def session_for(self, vehicle: str) -> VehicleSession:
        session = VehicleSession(
            db=self.db, vehicle_name=vehicle, drive=False, speed=1.0, clock_speed=0.0
        )
        session.start()
        self.addCleanup(session.stop)
        return session


class CodierBitTest(BasisTest):
    def test_kurzkodierung_lesen_und_setzen(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            self.assertEqual(module.coding()["coding"], "00002")
            bits = module.coding_bits()
            self.assertEqual([entry["bit"] for entry in bits], [1])
            # C01,1,2,Schaltgetriebe - Text der Labeldatei zum Bit
            self.assertEqual(bits[0]["text"], "Schaltgetriebe")

            module.coding_set_bit(0, 0, True)
            self.assertEqual(module.coding()["coding"], "00003")
            self.assertEqual([entry["bit"] for entry in module.coding_bits()], [0, 1])

            module.coding_set_bit(0, 0, False)
            self.assertEqual(module.coding()["coding"], "00002")

    def test_langkodierung_auf_can(self) -> None:
        session = self.session_for(GOLF5)
        with session.open_module(1) as module:
            start = module.coding()["coding"]
            self.assertEqual(start, "0000071")
            module.coding_set_bit(0, 5, True)   # Wert 32
            self.assertEqual(module.coding()["coding"], "0000103")
            module.coding_set_bit(0, 5, False)
            self.assertEqual(module.coding()["coding"], "0000071")

    def test_bit_ausserhalb_wird_abgelehnt(self) -> None:
        session = self.session_for(A4)
        with session.open_module(1) as module:
            with self.assertRaises(Exception):
                module.coding_set_bit(0, 9, True)

    def test_umrechnung_hex_und_dezimal(self) -> None:
        self.assertEqual(ModuleSession._coding_int("00003"), 3)
        self.assertEqual(ModuleSession._coding_int("00 1F"), 0x1F)
        self.assertEqual(ModuleSession._coding_text(3, "00002"), "3")
        self.assertEqual(ModuleSession._coding_text(0x1F, "001F"), "001F")


class KanalmapTest(BasisTest):
    def test_zeilen_aus_bloecken_und_kanaelen(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            rows = channel_map(module, groups=[1, 3])
            messwerte = [row for row in rows if row["art"] == "Messwerteblock"]
            kanaele = [row for row in rows if row["art"] == "Anpassung"]
            self.assertEqual(len(messwerte), 8)
            self.assertEqual(len(kanaele), 3)
            self.assertEqual(messwerte[0]["text"], "Motordrehzahl")
            self.assertEqual(messwerte[0]["einheit"], "1/min")
            self.assertEqual(kanaele[0]["gruppe"], "03")

    def test_nicht_verfuegbarer_block_wird_gemeldet(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            rows = channel_map(module, groups=[99])
            blocks = [row for row in rows if row["art"] == "Messwerteblock"]
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["text"], "nicht verfuegbar")
            self.assertIn("nicht verfuegbar", blocks[0]["bereich"])
            # Die Anpassungskanaele stehen unabhaengig von --groups dabei
            self.assertEqual(len([r for r in rows if r["art"] == "Anpassung"]), 3)

    def test_csv_format(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            rows = channel_map(module, groups=[1])
        text = to_csv(rows, vehicle=GOLF4, module="01 Motorsteuergerät")
        lines = text.splitlines()
        self.assertEqual(lines[0], f"Fahrzeug: {GOLF4}")
        self.assertEqual(lines[1], "Steuergeraet: 01 Motorsteuergerät")
        self.assertEqual(lines[2], ";".join(COLUMNS))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "kanalmap.csv"
            write_csv(target, rows, vehicle=GOLF4)
            data = target.read_text(encoding="latin-1")
            self.assertIn("Messwerteblock;001;1;Motordrehzahl", data)


class KanalmapKommandoTest(BasisTest):
    def test_kommandozeile_mit_datei(self) -> None:
        target = Path(self._tmp.name) / "cli-map.csv"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                [
                    "--db", str(self.db.path),
                    "--vehicle", GOLF4,
                    "--clock-speed", "0",
                    "map",
                    "--address", "01",
                    "--groups", "1",
                    "--out", str(target),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("Kanalmap geschrieben", buffer.getvalue())
        self.assertIn("Motordrehzahl", target.read_text(encoding="latin-1"))

    def test_kommandozeile_als_text(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                [
                    "--db", str(self.db.path),
                    "--vehicle", GOLF4,
                    "--clock-speed", "0",
                    "map", "--address", "01", "--groups", "1",
                ]
            )
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("Kanalmap Adresse 01", text)
        self.assertIn("Motordrehzahl", text)

    def test_codierbit_ueber_die_kommandozeile(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                [
                    "--db", str(self.db.path),
                    "--vehicle", GOLF4,
                    "--clock-speed", "0",
                    "coding", "--address", "01", "--login", "12233",
                    "--set-bit", "0.0", "--show-bits",
                ]
            )
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("Bit 0.0 gesetzt", text)
        self.assertIn("Codierung    : 00003", text)
        self.assertIn("Gesetzte Bits", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
