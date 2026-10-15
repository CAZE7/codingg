"""Tests fuer die Kanalhygiene auf den virtuellen Bussen.

Hintergrund: Die Weboberflaeche oeffnet fuer jede Anfrage einen Dialog. Bleibt
ein Kanal nach dem Schliessen am Bus haengen, liest und quittiert er fremde
Dialoge - das Steuergeraet "hoert" dann Quittungen, die nicht zu seiner
Uebertragung gehoeren, und der Dialog laeuft in Zeitfehler. Diese Tests halten
fest, dass beendete Kanaele abgemeldet sind und nur der eigene Dialog
mitgehoert wird.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vagdiag.client import DiagnosticError, VehicleSession
from vagdiag.db import Database
from vagdiag.proto.bus import VirtualCanBus
from vagdiag.proto.vwtp import BROADCAST_ID, VwtpChannel
from vagdiag.web.server import _AppState

GOLF5 = "VW Golf V 2.0 TDI (2005)"


class CanNodeHygieneTest(unittest.TestCase):
    """Ein beendeter CAN-Knoten darf keine Rahmen mehr mitlesen."""

    def test_stop_meldet_den_knoten_ab(self) -> None:
        bus = VirtualCanBus(name="Testbus")
        channel = VwtpChannel("VAGDiag-Test", bus, 0x01)
        self.assertIn(channel, bus.nodes)
        channel.start()
        self.assertIn(channel, bus.nodes)
        channel.stop(0.2)
        self.assertNotIn(channel, bus.nodes)

    def test_start_meldet_den_knoten_wieder_an(self) -> None:
        bus = VirtualCanBus(name="Testbus")
        channel = VwtpChannel("VAGDiag-Test", bus, 0x01)
        channel.start()
        channel.stop(0.2)
        channel.start()
        try:
            self.assertIn(channel, bus.nodes)
        finally:
            channel.stop(0.2)

    def test_testerkanal_hoert_nur_den_eigenen_dialog(self) -> None:
        bus = VirtualCanBus(name="Testbus")
        channel = VwtpChannel("VAGDiag-03", bus, 0x03, tester_addr=0xF1)
        ids = channel.accept_ids()
        self.assertEqual(ids, {BROADCAST_ID + 0xF1, BROADCAST_ID + 0x03})
        self.assertNotIn(BROADCAST_ID + 0x01, ids)
        self.assertNotIn(BROADCAST_ID + 0x44, ids)


class SessionKanalhygieneTest(unittest.TestCase):
    """Nach jedem Dialog bleibt kein Kanal des Testers am Bus zurueck."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "hygiene.sqlite3")
        cls.db.build(force=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        cls._tmp.cleanup()

    def session(self) -> VehicleSession:
        session = VehicleSession(vehicle_name=GOLF5, db=self.db, clock_speed=0.0, drive=False)
        self.addCleanup(session.stop)
        session.start()
        return session

    def test_modulwechsel_hinterlaesst_keine_kanaele(self) -> None:
        session = self.session()
        bus = session.vehicle.can_bus
        baseline = len(bus.nodes)
        for address in (1, 3, 44):
            with session.open_module(address) as module:
                module.ident()
                self.assertGreater(len(bus.nodes), baseline)
            self.assertEqual(len(bus.nodes), baseline)

    def test_weboberflaeche_schliesst_den_dialog(self) -> None:
        state = _AppState(self.db, vehicle=GOLF5, speed=6.0)
        self.addCleanup(state.session.stop)
        bus = state.session.vehicle.can_bus
        baseline = len(bus.nodes)
        for address in (1, 3, 44):
            with state.with_module(address) as module:
                self.assertTrue(module.ident()["part_number"])
            self.assertEqual(len(bus.nodes), baseline)

    def test_wechsel_zwischen_zwei_dialogen_bleibt_stoerungsfrei(self) -> None:
        """Staendiger Modulwechsel (wie in der Oberflaeche) ohne Wiederaufbau.

        Vor der Kanalhygiene quittierten verwaiste Kanaele fremde Dialoge; der
        Wechsel erzeugte dann Zeitfehler, die den Dialog neu aufbauten.
        """
        state = _AppState(self.db, vehicle=GOLF5, speed=6.0)
        self.addCleanup(state.session.stop)
        messages: list[str] = []
        state.log = lambda text: messages.append(text)
        for _ in range(5):
            with state.with_module(1) as module:
                module.ident()
                module.read_block(1)
            with state.with_module(3) as module:
                module.ident()
                module.read_block(1)
        self.assertEqual([text for text in messages if "neu aufbauen" in text], [])

    def test_nicht_verbaute_adresse_wird_nicht_wiederholt(self) -> None:
        state = _AppState(self.db, vehicle=GOLF5, speed=6.0)
        self.addCleanup(state.session.stop)
        attempts: list[str] = []
        state.log = lambda text: attempts.append(text)
        with self.assertRaises(DiagnosticError):
            with state.with_module(0x99):
                pass
        self.assertEqual([text for text in attempts if "neu aufbauen" in text], [])


if __name__ == "__main__":
    unittest.main()
