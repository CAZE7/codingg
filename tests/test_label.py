"""Tests fuer den Labeldatei-Parser (VCDS-Format)."""

from __future__ import annotations

import unittest
from pathlib import Path

from vagdiag.label.lbl import parse_label_file, parse_label_text

SAMPLE = """; Beispiel-Datei
:001,0
001,0,Leerlauf
001,1,Motordrehzahl,0.2*A*B,1/min,780,950
B001,1,Grundeinstellung Leerlauf
:002,0
002,0,Beschleunigung
002,1,Gaspedalstellung,,%,0,100
C01,1,2,Schaltgetriebe
L03,12233,Login Codierung
Redir: 038906019.lbl
Unbekannte Zeile 123
"""

SEED_LABEL = Path(__file__).resolve().parent.parent / "vagdiag/db/seed/labels/038906018G.lbl"


class ParseLabelTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.label = parse_label_text(SAMPLE, name="beispiel.lbl")

    def test_messwert_eintraege(self) -> None:
        self.assertEqual(len(self.label.measuring), 4)
        drehzahl = self.label.field_text(1, 1)
        self.assertIsNotNone(drehzahl)
        self.assertEqual(drehzahl.text, "Motordrehzahl")
        self.assertEqual(drehzahl.short_text, "0.2*A*B")
        self.assertEqual(drehzahl.unit, "1/min")
        self.assertEqual((drehzahl.min_value, drehzahl.max_value), (780.0, 950.0))

    def test_blocktitel(self) -> None:
        self.assertEqual(self.label.block_text(1), "Leerlauf")
        self.assertEqual(self.label.block_text(2), "Beschleunigung")
        self.assertEqual(self.label.block_text(99), "")

    def test_grundeinstellung(self) -> None:
        self.assertEqual(len(self.label.basic_setting), 1)
        self.assertEqual(self.label.basic_setting[0].block, 1)
        self.assertEqual(self.label.basic_setting[0].text, "Grundeinstellung Leerlauf")

    def test_codier_und_loginhilfe(self) -> None:
        self.assertEqual(len(self.label.coding), 2)
        coding = self.label.coding_for(1)[0]
        self.assertEqual((coding.kind, coding.block, coding.bit, coding.value), ("C", 1, 1, 2))
        self.assertEqual(coding.text, "Schaltgetriebe")
        login = self.label.coding_for(3)[0]
        self.assertEqual((login.kind, login.value), ("L", 12233))

    def test_leere_spalten(self) -> None:
        gaspedal = self.label.field_text(2, 1)
        self.assertEqual(gaspedal.text, "Gaspedalstellung")
        self.assertEqual(gaspedal.short_text, "")
        self.assertEqual(gaspedal.unit, "%")
        self.assertEqual((gaspedal.min_value, gaspedal.max_value), (0.0, 100.0))

    def test_redir_und_kommentare(self) -> None:
        self.assertEqual(self.label.redirects, ["038906019.lbl"])
        self.assertIn("Beispiel-Datei", self.label.comments)
        self.assertIn("?? Unbekannte Zeile 123", self.label.comments)


class SeedLabelFileTest(unittest.TestCase):
    def test_beispiel_labeldatei(self) -> None:
        self.assertTrue(SEED_LABEL.exists(), "Seed-Labeldatei fehlt")
        label = parse_label_file(SEED_LABEL)
        self.assertEqual(label.name, "038906018G.lbl")
        self.assertEqual(label.block_text(1), "Leerlauf (Einspritzmenge)")
        drehzahl = label.field_text(1, 1)
        self.assertEqual(drehzahl.unit, "1/min")
        self.assertEqual((drehzahl.min_value, drehzahl.max_value), (780.0, 950.0))
        kuehlmittel = label.field_text(1, 4)
        self.assertEqual(kuehlmittel.text, "Kühlmitteltemperatur")
        self.assertEqual(kuehlmittel.unit, "°C")
        # Mehrere Bloecke und Codierhilfen vorhanden
        self.assertGreaterEqual(len({entry.block for entry in label.measuring}), 5)
        # Codierhilfe mit Bitangabe ("C01,1,2,Schaltgetriebe")
        schaltgetriebe = [
            entry
            for entry in label.coding_for(1)
            if entry.value == 2 and entry.bit == 1 and entry.text == "Schaltgetriebe"
        ]
        self.assertTrue(schaltgetriebe, "Codierhilfe C01 nicht geparst")


if __name__ == "__main__":
    unittest.main()
