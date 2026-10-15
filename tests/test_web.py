"""Tests der JSON-Schnittstelle der Weboberflaeche.

Der Server laeuft im Test auf einem freien Port im Hintergrund; geprueft
werden die Endpunkte, die die Reiter der Oberflaeche bedienen -
einschliesslich Stellglieddiagnose, OBD-II und Aufzeichnung.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from vagdiag.db import Database
from vagdiag.web.server import VagdiagServer, _AppState

GOLF4 = "VW Golf IV 1.9 TDI (2001)"


class WebApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "web.sqlite3")
        cls.db.build(force=True)
        cls.state = _AppState(cls.db, vehicle=GOLF4, speed=6.0)
        cls.server = VagdiagServer(("127.0.0.1", 0), cls.state)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.state.session.stop()
        cls._tmp.cleanup()

    def call(self, method: str, path: str, payload=None, *, raw: bool = False):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                return response.status, (body if raw else json.loads(body.decode()))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode()
            try:
                return exc.code, json.loads(body or "{}")
            except ValueError:
                return exc.code, {"error": body}

    # -- Grundlagen -------------------------------------------------------- #
    def test_seite_und_zustand(self) -> None:
        status, page = self.call("GET", "/", raw=True)
        self.assertEqual(status, 200)
        text = page.decode("utf-8")
        for tab in ("Stellglieder", "OBD-II", "Log"):
            self.assertIn(tab, text)

        status, state = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["vehicle"], GOLF4)
        self.assertTrue(state["modules"])

    def test_unbekannter_pfad_und_adresse(self) -> None:
        self.assertEqual(self.call("GET", "/api/gibtsnicht")[0], 404)
        status, payload = self.call("GET", "/api/module/99")
        self.assertEqual(status, 400)
        self.assertIn("nicht verbaut", payload["error"])

    # -- Stellglieddiagnose ------------------------------------------------ #
    def test_stellglieder_lesen(self) -> None:
        status, data = self.call("GET", "/api/actuator/01")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["actuators"]), 6)
        self.assertTrue(data["basic"])
        self.assertEqual(data["active"], 0)

    def test_stellglied_ansteuern_und_abschalten(self) -> None:
        status, data = self.call("POST", "/api/actuator/01", {"actuator": 3})
        self.assertEqual(status, 200)
        self.assertEqual(data["active"], 3)
        self.assertTrue(data["actuators"][2]["active"])

        status, data = self.call("POST", "/api/actuator/01", {"actuator": 0})
        self.assertEqual(data["active"], 0)

    def test_grundeinstellung_schalten(self) -> None:
        status, _ = self.call("POST", "/api/actuator/01", {"basic": 1, "on": True})
        self.assertEqual(status, 200)
        _, data = self.call("POST", "/api/actuator/01", {"basic": 1, "on": True})
        self.assertEqual(data["basic_active"], 1)
        self.assertTrue(data["basic_on"])
        _, data = self.call("POST", "/api/actuator/01", {"basic": 1, "on": False})
        self.assertEqual(data["basic_active"], 0)

    # -- OBD-II ------------------------------------------------------------ #
    def test_obd_uebersicht(self) -> None:
        status, data = self.call("POST", "/api/obd", {"dienst": "info"})
        self.assertEqual(status, 200)
        self.assertIn("K-Line", data["transport"])
        self.assertGreaterEqual(len(data["supported_pids"]), 20)
        self.assertEqual(len(data["vehicle_info"]["vin"]), 17)
        self.assertTrue(data["live"])
        self.assertTrue(data["monitors"])
        self.assertIsInstance(data["vag_codes"], list)
        self.assertTrue(data["freeze_frame"])

    def test_obd_loeschen_ist_ein_eigener_dienst(self) -> None:
        status, data = self.call("POST", "/api/obd", {"dienst": "clear"})
        self.assertEqual(status, 200)
        self.assertTrue(data["geloescht"])

    def test_kanalmap_und_langcodierung(self) -> None:
        status, data = self.call("GET", "/api/map/01")
        self.assertEqual(status, 200)
        self.assertTrue(data["rows"])
        self.assertEqual(data["rows"][0]["art"], "Messwerteblock")

        status, csv_bytes = self.call("GET", "/api/map/01?format=csv", raw=True)
        self.assertEqual(status, 200)
        text = csv_bytes.decode("latin-1")
        self.assertIn("art;gruppe;feld;text;wert;einheit;bereich", text)

        # Bit setzen und wieder loeschen (Langcodierung)
        status, data = self.call("POST", "/api/coding/01", {"byte": 0, "bit": 0, "on": True})
        self.assertEqual(status, 200)
        self.assertEqual(data["coding"], "00003")
        self.assertTrue(any(entry["bit"] == 0 for entry in data["bits"]))
        status, data = self.call("POST", "/api/coding/01", {"byte": 0, "bit": 0, "on": False})
        self.assertEqual(data["coding"], "00002")

    # -- Aufzeichnung ------------------------------------------------------ #
    def test_aufzeichnung_starten_lesen_und_speichern(self) -> None:
        status, data = self.call(
            "POST",
            "/api/log/start",
            {"address": 1, "blocks": "1,4", "interval": 0.2, "duration": 2},
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(data["spalten"]), 8)
        self.assertTrue(data["laufend"])

        time.sleep(0.6)
        _, state = self.call("GET", "/api/log")
        self.assertGreaterEqual(state["gesamt"], 2)
        # Zeitstempel, Spaltenschluessel und Ueberschriften je Messwert
        self.assertEqual(len(state["zeilen"][0]), 2 + 2 * 8)

        status, csv_bytes = self.call("GET", "/api/log.csv", raw=True)
        self.assertEqual(status, 200)
        text = csv_bytes.decode("latin-1")
        self.assertTrue(text.startswith("Zeit;"))
        self.assertIn("Motordrehzahl (1/min)", text)

        _, stopped = self.call("POST", "/api/log/stop", {})
        self.assertFalse(stopped["laufend"])
        self.assertTrue(stopped["datei"].endswith(".csv"))

    def test_aufzeichnung_ohne_start(self) -> None:
        # Vor dem Start und nach dem Stopp ist der Reiter leer, aber nutzbar
        self.call("POST", "/api/log/stop", {})
        status, state = self.call("GET", "/api/log")
        self.assertEqual(status, 200)
        self.assertFalse(state["laufend"])
        self.assertEqual(self.call("GET", "/api/log.csv")[0], 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
