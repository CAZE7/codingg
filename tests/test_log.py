"""Tests fuer die Messwertaufzeichnung (VCDS: Log / CSV-Datei).

Geprueft werden Spaltenaufbau, Zusammenfassen mehrerer Bloecke in eine Zeile,
Zeitstempel, Dateischreiben im VCDS-Format und die Aufzeichnung im
Hintergrund - einmal mit einem festen Musterdatensatz und einmal gegen den
Fahrzeugsimulator.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import contextlib
import io

from vagdiag.cli import main as cli_main
from vagdiag.client import VehicleSession
from vagdiag.db import Database
from vagdiag.log import (
    CSV_DELIMITER,
    Column,
    LogRecorder,
    block_columns,
    column_key,
    format_value,
    row_from_block,
    write_csv,
)

GOLF5 = "VW Golf V 2.0 TDI (2005)"
GOLF4 = "VW Golf IV 1.9 TDI (2001)"


class SpaltenTest(unittest.TestCase):
    def test_kopfzeile_mit_einheit(self) -> None:
        column = Column(group=1, field=2, text="Motordrehzahl", unit="1/min")
        self.assertEqual(column.header, "Block 001  Motordrehzahl (1/min)")
        self.assertEqual(column_key(column), "1-2")

        basic = Column(group=4, field=1, text="Zahlenwert", basic=True)
        self.assertEqual(basic.header, "Grundeinstellung 004  Zahlenwert")
        self.assertEqual(column_key(basic), "4-1b")

    def test_werte_mit_deutschem_dezimalkomma(self) -> None:
        self.assertEqual(format_value(16.6), "16,6")
        self.assertEqual(format_value(800), "800")
        self.assertEqual(format_value(0.5), "0,5")
        self.assertEqual(format_value(None), "")
        self.assertEqual(format_value(True), "1")

    def test_zeile_aus_block(self) -> None:
        block = {
            "group": 1,
            "basic": False,
            "fields": [
                {
                    "field": 1,
                    "text": "Motordrehzahl",
                    "unit": "1/min",
                    "value": 800,
                    "display": "800",
                    "display_full": "800 1/min",
                }
            ],
        }
        row = row_from_block(block)
        self.assertEqual(row["1-1"], 800)
        self.assertEqual(row["Block 001  Motordrehzahl (1/min)"], "800 1/min")


class DateiTest(unittest.TestCase):
    def test_schreibt_vcds_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "log.csv"
            columns = [
                Column(group=1, field=1, text="Motordrehzahl", unit="1/min"),
                Column(group=1, field=2, text="Einspritzmenge", unit="mg/Hub"),
            ]
            rows = [
                {"Zeit": "10:00:00", "Sekunden": "0.00", "1-1": 800, "1-2": 16.6},
                {"Zeit": "10:00:01", "Sekunden": "1.00", "1-1": 850, "1-2": 17.2},
            ]
            path = write_csv(target, columns, rows, vehicle=GOLF5, module="01 Motorsteuergerät")
            data = path.read_bytes().decode("latin-1")
            self.assertEqual(path, target)
            lines = data.splitlines()
            self.assertEqual(lines[0], f"Fahrzeug: {GOLF5}")
            self.assertEqual(lines[1], "Steuergeraet: 01 Motorsteuergerät")
            header = lines[2]
            self.assertEqual(header.count(CSV_DELIMITER), 3)
            self.assertTrue(header.startswith("Zeit;Sekunden;Block 001  Motordrehzahl (1/min)"))
            self.assertEqual(lines[3], "10:00:00;0.00;800;16,6")
            self.assertEqual(len(lines), 5)


class RecorderTest(unittest.TestCase):
    """Der Recorder arbeitet mit einem festen Musterdatensatz."""

    def _sampler(self, values: list[int]):
        calls = {"index": 0}

        def sampler():
            index = min(calls["index"], len(values) - 1)
            calls["index"] += 1
            value = values[index]
            columns = [Column(group=1, field=1, text="Motordrehzahl", unit="1/min")]
            rows = [{"1-1": value, "Block 001  Motordrehzahl (1/min)": f"{value} 1/min"}]
            return columns, rows

        return sampler

    def test_einzelne_abtastung(self) -> None:
        recorder = LogRecorder(self._sampler([800, 900]), interval=0.05)
        row = recorder.sample()
        self.assertEqual(row["1-1"], 800)
        self.assertIn("Zeit", row)
        self.assertEqual(len(recorder.columns), 1)
        self.assertEqual(recorder.columns[0].header, "Block 001  Motordrehzahl (1/min)")
        recorder.sample()
        self.assertEqual([r["1-1"] for r in recorder.rows], [800, 900])

    def test_hintergrundaufzeichnung_und_datei(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lauf.csv"
            recorder = LogRecorder(
                self._sampler([800, 810, 820, 830]),
                interval=0.02,
                path=path,
                duration=0.3,
                vehicle=GOLF5,
                module="01",
            )
            recorder.start()
            self.assertTrue(recorder.running)
            time.sleep(0.25)
            recorder.stop()
            self.assertFalse(recorder.running)
            self.assertGreaterEqual(len(recorder.rows), 2)
            self.assertFalse(recorder.errors)
            text = path.read_text(encoding="latin-1")
            self.assertIn("Motordrehzahl (1/min)", text)
            self.assertTrue(recorder.csv_text().startswith("Zeit;Sekunden;Block 001"))
            payload = recorder.as_dict()
            self.assertEqual(payload["gesamt"], len(recorder.rows))
            self.assertFalse(payload["laufend"])

    def test_fehlender_sampler_wird_gemeldet(self) -> None:
        def kaputt():
            raise RuntimeError("Bus weg")

        recorder = LogRecorder(kaputt, interval=0.05)
        self.assertIsNone(recorder.sample())
        self.assertEqual(len(recorder.errors), 1)
        self.assertIn("Bus weg", recorder.errors[0])

    def test_mehrere_bloecke_in_einer_zeile(self) -> None:
        def sampler():
            columns = [
                Column(group=1, field=1, text="Drehzahl", unit="1/min"),
                Column(group=4, field=1, text="Russmasse", unit="g"),
            ]
            return columns, [{"1-1": 800, "4-1": 12}, {"1-1": 800}]

        recorder = LogRecorder(sampler, interval=0.05)
        recorder.sample()
        self.assertEqual(len(recorder.rows), 1)
        self.assertEqual(recorder.rows[0]["4-1"], 12)


class SimulatorLogTest(unittest.TestCase):
    """Aufzeichnung gegen echte Messwertebloecke des Simulators."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "log.sqlite3")
        cls.db.build(force=True)

    def session_for(self, vehicle: str) -> VehicleSession:
        session = VehicleSession(
            db=self.db, vehicle_name=vehicle, drive=False, speed=1.0, clock_speed=0.0
        )
        session.start()
        self.addCleanup(session.stop)
        return session

    def test_spalten_aus_dem_steuergeraet(self) -> None:
        session = self.session_for(GOLF5)
        with session.open_module(1) as module:
            columns = block_columns(module, 1)
            self.assertEqual(len(columns), 4)
            self.assertEqual(columns[0].text, "Motordrehzahl")
            self.assertEqual(columns[0].unit, "1/min")
            self.assertEqual(columns[0].group, 1)

    def test_aufzeichnung_zwei_bloecke(self) -> None:
        session = self.session_for(GOLF5)
        with tempfile.TemporaryDirectory() as tmp, session.open_module(1) as module:
            path = Path(tmp) / "golf5.csv"

            def sampler():
                columns, rows = [], []
                for group in (1, 4):
                    block = module.read_block(group)
                    columns.extend(block_columns_of(block))
                    rows.append(row_from_block(block))
                return columns, rows

            recorder = LogRecorder(sampler, interval=0.05, path=path, vehicle=GOLF5, module="01")
            recorder.sample()
            recorder.sample()
            self.assertEqual(len(recorder.columns), 8)
            self.assertEqual(len(recorder.rows), 2)
            for row in recorder.rows:
                self.assertIn("1-1", row)   # Drehzahl
                self.assertIn("4-2", row)   # Russmasse berechnet
            saved = recorder.save()
            text = saved.read_text(encoding="latin-1")
            self.assertEqual(text.count("Block 001  Motordrehzahl (1/min)"), 1)
            self.assertIn(";", text.splitlines()[2])

    def test_kline_block_ohne_einheit(self) -> None:
        session = self.session_for(GOLF4)
        with session.open_module(1) as module:
            columns = block_columns(module, 1)
            self.assertEqual(len(columns), 4)
            self.assertEqual(columns[1].text, "Einspritzmenge")
            self.assertEqual(columns[1].unit, "mg/Hub")


class LogKommandoTest(unittest.TestCase):
    """Das Kommando `vagdiag log` schreibt eine vollstaendige CSV-Datei."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "logcli.sqlite3")
        cls.db.build(force=True)

    def test_aufzeichnung_ueber_die_kommandozeile(self) -> None:
        target = Path(self._tmp.name) / "cli.csv"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                [
                    "--db", str(self.db.path),
                    "--vehicle", GOLF5,
                    "--clock-speed", "0",
                    "log",
                    "--address", "01",
                    "--blocks", "1,4",
                    "--interval", "0.2",
                    "--duration", "1",
                    "--out", str(target),
                ]
            )
        self.assertEqual(code, 0)
        text = buffer.getvalue()
        self.assertIn("Schreibe 8 Messwerte", text)
        self.assertIn("Fertig:", text)
        data = target.read_text(encoding="latin-1")
        lines = data.splitlines()
        self.assertEqual(lines[0], f"Fahrzeug: {GOLF5}")
        self.assertTrue(lines[2].startswith("Zeit;Sekunden;Block 001  Motordrehzahl"))
        self.assertGreaterEqual(len(lines), 5)


def block_columns_of(block: dict) -> list[Column]:
    """Spalten aus einem bereits gelesenen Block (fuer den Test-Sampler)."""
    return [
        Column(
            group=int(block["group"]),
            field=int(entry["field"]),
            text=str(entry.get("text") or ""),
            unit=str(entry.get("unit") or ""),
            basic=bool(block.get("basic")),
        )
        for entry in block["fields"]
    ]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
