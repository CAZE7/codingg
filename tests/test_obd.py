"""Tests fuer die OBD-II-Schicht (ISO 15031/SAE J1979).

Geprueft wird die Auswertung der PID-Daten (:mod:`vagdiag.obd.obd2`) und die
Sitzung gegen das Motorsteuergeraet (:mod:`vagdiag.obd.session`) - auf der
K-Line (ISO 14230) und auf CAN (ISO 15765-4).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import contextlib
import io
import json

from vagdiag.cli import main as cli_main
from vagdiag.client import VehicleSession
from vagdiag.db import Database
from vagdiag.obd import obd2
from vagdiag.obd.session import OBD_PID_AUSWAHL, ObdError, ObdSession

GOLF4 = "VW Golf IV 1.9 TDI (2001)"
PASSAT = "VW Passat B5.5 1.9 TDI (2003)"
A4 = "Audi A4 B6 1.8T (2003)"
GOLF5 = "VW Golf V 2.0 TDI (2005)"


class Obd2AuswertungTest(unittest.TestCase):
    """Reine Auswertung der PID-Tabelle - ohne Bus."""

    def test_pid_tabelle_kennt_vcds_auswahl(self) -> None:
        for pid in OBD_PID_AUSWAHL:
            self.assertIsNotNone(obd2.pid_info(pid), f"PID 0x{pid:02X} fehlt")

    def test_drehzahl_und_temperatur(self) -> None:
        self.assertEqual(obd2.pid_info(0x0C).text(b"\x1a\xf8"), "1726 1/min")
        self.assertEqual(obd2.pid_info(0x05).text(b"\x7b"), "83 °C")
        self.assertEqual(obd2.pid_info(0x0D).text(b"\x00"), "0 km/h")

    def test_bereitschaft_benzin_und_diesel(self) -> None:
        benzin = obd2.decode_readiness(b"\x82\x00\x06\xe1")
        self.assertTrue(benzin.mil_on)
        self.assertEqual(benzin.dtc_count, 2)
        self.assertFalse(benzin.compression)
        self.assertIn("Katalysator", benzin.incomplete)
        self.assertIn("Lambdasonde", benzin.incomplete)
        self.assertIn("Kraftstoffsystem", benzin.incomplete)

        # Bit 3 in Byte B kennzeichnet Selbstzuendung (Diesel);
        # gesetzte Bits in Byte D sind noch laufende Monitore.
        diesel = obd2.decode_readiness(b"\x00\x09\x00\xe8")
        self.assertTrue(diesel.compression)
        self.assertEqual(
            diesel.incomplete,
            ["Ladedruckregelung", "Abgassensor", "Partikelfilter", "AGR/VVT"],
        )
        self.assertEqual(diesel.as_dict()["compression"], 1)

    def test_modusnamen_und_monitorliste(self) -> None:
        self.assertEqual(obd2.OBD_MODES[0x03], "Fehlercodes (gespeichert)")
        self.assertEqual(obd2.OBD_MODES[0x06], "Monitortest-Ergebnisse")
        diesel_monitore = obd2.monitor_names(1)
        self.assertIn("Partikelfilter", diesel_monitore)
        self.assertEqual(diesel_monitore[-1], "AGR/VVT")
        self.assertIn("Katalysator", obd2.monitor_names(0))


class ObdSitzungTestBase(unittest.TestCase):
    """Baut eine Fahrzeugsitzung ohne Wartezeit auf dem Bus auf."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "obd.sqlite3")
        cls.db.build(force=True)

    def session_for(self, vehicle: str) -> VehicleSession:
        session = VehicleSession(
            db=self.db, vehicle_name=vehicle, drive=False, speed=1.0, clock_speed=0.0
        )
        session.start()
        self.addCleanup(session.stop)
        return session


class ObdKLineTest(ObdSitzungTestBase):
    """OBD-II ueber ISO 14230 (K-Line) - Golf IV und Passat B5.5."""

    def test_transport_ist_kline(self) -> None:
        session = self.session_for(GOLF4)
        with ObdSession(session) as obd:
            self.assertIn("K-Line", obd.transport_name)

    def test_pids_readiness_und_fahrzeugdaten(self) -> None:
        session = self.session_for(GOLF4)
        with ObdSession(session) as obd:
            pids = obd.supported_pids()
            self.assertGreaterEqual(len(pids), 20)
            self.assertIn(0x0C, pids)

            readiness = obd.readiness()
            self.assertFalse(readiness.mil_on)
            self.assertTrue(readiness.compression)
            self.assertIn("Partikelfilter", readiness.incomplete)

            info = obd.vehicle_info()
            self.assertEqual(len(info["vin"]), 17)
            self.assertEqual(info["vin"], "WVWZZZ1JZ27537500")
            self.assertEqual(info["ecu_name"], "038906018G")
            self.assertEqual(info["cvn"], "00 00 02 1A")
            self.assertFalse(info["calibration_id"].startswith("\x01"))

    def test_live_werte_und_monitore(self) -> None:
        session = self.session_for(GOLF4)
        with ObdSession(session) as obd:
            entries = obd.live([0x0C, 0x05, 0x0D])
            self.assertEqual([entry.pid for entry in entries], [0x0C, 0x05, 0x0D])
            self.assertIn("1/min", entries[0].text)
            self.assertIn("°C", entries[1].text)

            monitors = obd.monitors()
            self.assertTrue(monitors)
            self.assertTrue(all(entry["ok"] for entry in monitors))

    def test_fehlercodes_und_vag_codes(self) -> None:
        session = self.session_for(GOLF4)
        with ObdSession(session) as obd:
            # Der Golf IV fuehrt nur VAG-Codes ohne SAE-Schreibweise
            self.assertEqual(obd.codes(0x03), [])
            vag_only = obd.vag_only_codes()
            self.assertEqual([entry["code"] for entry in vag_only], ["00575", "00522"])
            # Nach dem VAG-Dialog muss die OBD-Sitzung weiterarbeiten
            self.assertTrue(obd.supported_pids())

    def test_passat_meldet_sae_codes(self) -> None:
        session = self.session_for(PASSAT)
        with ObdSession(session) as obd:
            codes = [entry["code"] for entry in obd.codes(0x03)]
            self.assertEqual(codes, ["P0299", "P0411"])
            self.assertTrue(obd.readiness().mil_on)

    def test_freeze_frame(self) -> None:
        session = self.session_for(GOLF4)
        with ObdSession(session) as obd:
            frame = {entry.pid: entry for entry in obd.freeze_frame()}
            self.assertIn(0x0C, frame)
            self.assertIn("1/min", frame[0x0C].text)


class ObdCanTest(ObdSitzungTestBase):
    """OBD-II ueber ISO 15765-4 (CAN 0x7E0/0x7E8) - A4 und Golf V."""

    def test_transport_ist_isotp(self) -> None:
        for vehicle in (A4, GOLF5):
            with self.subTest(vehicle=vehicle):
                session = self.session_for(vehicle)
                with ObdSession(session) as obd:
                    self.assertIn("ISO-TP", obd.transport_name)

    def test_golf5_meldet_drei_fehler(self) -> None:
        session = self.session_for(GOLF5)
        with ObdSession(session) as obd:
            codes = [entry["code"] for entry in obd.codes(0x03)]
            self.assertEqual(codes, ["P2463", "P2004", "P0299"])
            readiness = obd.readiness()
            self.assertTrue(readiness.mil_on)
            self.assertEqual(readiness.dtc_count, 3)
            self.assertEqual(obd.codes(0x07), obd.codes(0x07))

    def test_a4_pending_codes_und_monitore(self) -> None:
        session = self.session_for(A4)
        with ObdSession(session) as obd:
            self.assertEqual(
                [entry["code"] for entry in obd.codes(0x03)], ["P0420", "P1127"]
            )
            self.assertFalse(obd.readiness().compression)
            monitors = obd.monitors()
            self.assertTrue(monitors)
            labels = [str(entry["name"]) for entry in monitors]
            self.assertIn("Katalysator", labels)
            self.assertTrue(all(entry["ok"] for entry in monitors))

    def test_loeschen(self) -> None:
        session = self.session_for(GOLF5)
        with ObdSession(session) as obd:
            self.assertTrue(obd.clear_codes())
            self.assertEqual(obd.codes(0x03), [])

    def test_kurzfassung_als_json(self) -> None:
        session = self.session_for(A4)
        with ObdSession(session) as obd:
            payload = obd.as_dict()
            for key in ("transport", "supported_pids", "readiness", "codes", "vehicle_info"):
                self.assertIn(key, payload)
            self.assertEqual(payload["readiness"]["dtc_count"], 2)


class ObdFehlerTest(ObdSitzungTestBase):
    """Fehlerfaelle der OBD-Sitzung."""

    def test_abgelehnter_dienst(self) -> None:
        session = self.session_for(GOLF4)
        with ObdSession(session) as obd:
            with self.assertRaises(ObdError):
                obd.request(b"\x99\x01")

    def test_geschlossene_sitzung(self) -> None:
        session = self.session_for(GOLF4)
        obd = ObdSession(session)
        with self.assertRaises(ObdError):
            obd.request(b"\x01\x0c")


class ObdKommandozeileTest(unittest.TestCase):
    """Der OBD-Befehl der Kommandozeile (Ausgabe und Rueckgabewert)."""

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                ["--vehicle", GOLF4, "--clock-speed", "0", *arguments]
            )
        return code, buffer.getvalue()

    def test_bereitschaft_anzeigen(self) -> None:
        code, text = self.run_cli("obd", "--readiness-only")
        self.assertEqual(code, 0)
        self.assertIn("MIL: aus", text)
        self.assertIn("Motorart: Diesel", text)
        self.assertIn("Bereitschaft offen", text)

    def test_fehlercodes_und_infos(self) -> None:
        code, text = self.run_cli("obd", "--codes", "--info")
        self.assertEqual(code, 0)
        self.assertIn("Fahrgestellnummer : WVWZZZ1JZ27537500", text)
        self.assertIn("VAG-Codes ohne OBD-Schreibweise", text)
        self.assertIn("00575", text)

    def test_monitore_und_json(self) -> None:
        code, text = self.run_cli("obd", "--monitors")
        self.assertEqual(code, 0)
        self.assertIn("MID", text)

        code, text = self.run_cli("obd", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(text)
        self.assertEqual(len(payload["vehicle_info"]["vin"]), 17)
        self.assertTrue(payload["readiness"]["incomplete"])

    def test_live_werte(self) -> None:
        code, text = self.run_cli(
            "obd", "--live", "--count", "3", "--interval", "0.1", "--duration", "0.4"
        )
        self.assertEqual(code, 0)
        self.assertIn("Live-Messwerte (Modus 01)", text)
        self.assertIn("Motordrehzahl", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
