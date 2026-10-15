"""Tests fuer Anzeige-Hilfen der Client-Ebene."""

from __future__ import annotations

import unittest

from vagdiag import address_text, address_wire
from vagdiag.client import ModuleSession, _units_compatible, _without_unit
from vagdiag.db import ModuleInfo


def make_session(coding: str = "0021505") -> ModuleSession:
    """ModuleSession ohne Dialog aufbauen (nur fuer Anzeige-Hilfen)."""
    session = ModuleSession.__new__(ModuleSession)
    session.module = ModuleInfo(address=3, name="ABS/Bremsenelektronik", coding=coding)
    return session


class CodingDisplayTest(unittest.TestCase):
    def test_fuehrende_nullen_bleiben_erhalten(self) -> None:
        session = make_session("0021505")
        self.assertEqual(session._padded_coding("21505"), "0021505")

    def test_siebenstellige_langcodierung(self) -> None:
        session = make_session("0016751")
        self.assertEqual(session._padded_coding("16751"), "0016751")

    def test_geaenderte_codierung_behaelt_die_stellenzahl(self) -> None:
        session = make_session("00002")
        self.assertEqual(session._padded_coding("3"), "00003")

    def test_andere_codierung_wird_nicht_verbogen(self) -> None:
        session = make_session("00002")
        self.assertEqual(session._padded_coding("12345"), "12345")
        self.assertEqual(session._padded_coding("123456789"), "123456789")

    def test_nicht_numerische_angabe_bleibt(self) -> None:
        session = make_session("00002")
        self.assertEqual(session._padded_coding("nicht lesbar (Timeout)"), "nicht lesbar (Timeout)")


class AddressDisplayTest(unittest.TestCase):
    def test_textform(self) -> None:
        self.assertEqual([address_text(a) for a in (1, 3, 44, 46, 56)], ["01", "03", "44", "46", "56"])

    def test_busform(self) -> None:
        self.assertEqual(address_wire(46), 0x46)
        self.assertEqual(address_wire(1), 0x01)
        self.assertEqual(address_wire(19), 0x19)
        self.assertEqual(address_wire(56), 0x56)

    def test_anzeige_und_bus_gehoeren_zusammen(self) -> None:
        for address in (1, 3, 8, 9, 15, 17, 19, 44, 46, 56):
            with self.subTest(address=address):
                self.assertEqual(int(address_text(address), 16), address_wire(address))


class UnitHelpersTest(unittest.TestCase):
    def test_gleiche_einheit(self) -> None:
        self.assertTrue(_units_compatible("bar", "bar"))
        self.assertTrue(_units_compatible("mbar", "mbar"))

    def test_fragezeichen_und_leer(self) -> None:
        self.assertTrue(_units_compatible("", "bar"))
        self.assertTrue(_units_compatible("?", "bar"))
        self.assertTrue(_units_compatible("bar", ""))

    def test_winkel_mit_richtungsangabe(self) -> None:
        self.assertTrue(_units_compatible("°vor OT", "°nach OT"))
        self.assertTrue(_units_compatible("°vOT", "°vor OT"))

    def test_unterschiedliche_einheiten(self) -> None:
        self.assertFalse(_units_compatible("mbar", "bar"))
        self.assertFalse(_units_compatible("bar", "mbar"))
        self.assertFalse(_units_compatible("g", "mg/Hub"))
        self.assertFalse(_units_compatible("ms", "s"))

    def test_einheit_entfernen(self) -> None:
        self.assertEqual(_without_unit("1,2 V", "V"), "1,2")
        self.assertEqual(_without_unit("800 1/min", "1/min"), "800")
        self.assertEqual(_without_unit("48,0 g", "g"), "48,0")
        self.assertEqual(_without_unit("15,0 ms", "g"), "15,0 ms")


if __name__ == "__main__":
    unittest.main()
