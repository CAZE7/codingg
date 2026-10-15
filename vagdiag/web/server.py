"""Weboberflaeche im VCDS-Look (Python-Standardbibliothek).

Der Server liefert eine einzige HTML-Seite (CSS und JavaScript sind inline,
damit sie in jeder Vorschau-Umgebung ohne externe Ressourcen laeuft) und eine
JSON-Schnittstelle:

===============================  ==========================================================
``GET  /api/state``              Fahrzeug, Live-Werte, Datenbankkennzahlen, Modulliste
``POST /api/vehicle``            Fahrzeug wechseln (``{"name": "..."}``)
``POST /api/scan``               Autoscan ausfuehren (JSON + VCDS-Bericht)
``GET  /api/module/<addr>``      Identifikation, Codierung, Anpassung, Blockliste
``GET  /api/faults/<addr>``      Fehlerspeicher lesen
``POST /api/faults/<addr>``      Fehlerspeicher loeschen (``{"clear": true}``)
``GET  /api/blocks/<addr>``      Messwerteblock (``?group=1&basic=0``)
``POST /api/coding/<addr>``      Codierung schreiben (``{"value": "...", "login": "..."}``)
``POST /api/adapt/<addr>``       Anpassung lesen/schreiben (``{"channel": 4, "value": 33000}``)
``GET  /api/actuator/<addr>``    Stellgliedliste und Grundeinstellungsbloecke
``POST /api/actuator/<addr>``    Stellglied ansteuern / Grundeinstellung schalten
``GET  /api/obd``                OBD-II-Uebersicht (Transport, Bereitschaft, Fehler, Info)
``POST /api/obd``                OBD-II-Dienst ausfuehren (``{"dienst": "loeschen"|"monitore"}``)
``POST /api/log/start``          Messwerte mitschreiben (``{"address": 1, "blocks": "1,4"}``)
``GET  /api/log``                Stand der Aufzeichnung (Zeilen, Spalten, Fehler)
``POST /api/log/stop``           Aufzeichnung beenden
``GET  /api/log.csv``            Aufzeichnung als CSV-Datei herunterladen
``GET  /api/trace``              Busprotokoll der Simulation
===============================  ==========================================================
"""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .. import APP_NAME, APP_SUBTITLE, __version__, address_text
from ..client import DiagnosticError, VehicleSession
from ..db import Database
from ..log import LogRecorder, row_from_block
from ..obd.scan import format_autoscan
from ..obd.session import ObdSession
from ..proto.isotp import IsoTpError
from ..proto.kw1281 import Kwp1281Error
from ..proto.kwp2000 import Kwp2000Error
from ..proto.uds import UdsError
from ..proto.vwtp import VwtpError
from ..sim.vehicles import load_vehicle

__all__ = ["VagdiagServer", "run_server", "STATIC_DIR"]

STATIC_DIR = Path(__file__).resolve().parent / "static"


class _AppState:
    """Gemeinsamer Zustand aller Anfragen: Fahrzeug, Datenbank, Sitzungen."""

    def __init__(
        self,
        db: Database,
        *,
        vehicle: str | None = None,
        speed: float = 6.0,
        log=None,
    ) -> None:
        self.db = db
        self.speed = speed
        self.log = log or (lambda text: None)
        self.lock = threading.RLock()
        self.session = VehicleSession(
            db=db,
            vehicle_name=vehicle,
            speed=speed,
            drive=True,
            log=self.log,
        )
        self.session.start()
        self.last_scan: dict | None = None
        self._last_report: str = ""
        #: Aufzeichnung (VCDS: Log) - laeuft optional im Hintergrund
        self.recorder: LogRecorder | None = None
        self.record_error: str = ""

    # -- Fahrzeug ---------------------------------------------------------- #
    def switch_vehicle(self, name: str) -> dict:
        with self.lock:
            self.stop_log()
            self.session.stop()
            self.session = VehicleSession(
                db=self.db, vehicle_name=name, speed=self.speed, drive=True, log=self.log
            )
            self.session.start()
            self.last_scan = None
            return self.state()

    def state(self) -> dict:
        with self.lock:
            data = self.session.status()
            data["app"] = {"name": APP_NAME, "subtitle": APP_SUBTITLE, "version": __version__}
            data["modules"] = [
                {
                    "address": module.address,
                    "address_text": module.address_text,
                    "name": module.name,
                    "protocol": module.protocol,
                    "bus": module.bus,
                    "part_number": module.part_number,
                    "component": module.component,
                    "label_file": module.label_file,
                }
                for module in self.session.module_list()
            ]
            data["last_scan"] = self.last_scan
            return data

    def vehicles(self) -> list[dict]:
        """Alle Fahrzeuge der Datenbank (Name, Plattform, Modulanzahl)."""
        entries = []
        for row in self.db.vehicles():
            modules = self.db.modules_for_vehicle(row["id"])
            entries.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "platform": row.get("platform"),
                    "year": row.get("model_year"),
                    "engine": row.get("engine"),
                    "protocol": row.get("protocol"),
                    "modules": len(modules),
                }
            )
        return entries

    # -- Modulzugriff mit Wiederaufbau ------------------------------------- #
    #: Fehler, nach denen ein neuer Dialogaufbau sinnvoll ist
    RETRY_ERRORS = (
        DiagnosticError,
        Kwp1281Error,
        Kwp2000Error,
        UdsError,
        IsoTpError,
        VwtpError,
        TimeoutError,
        OSError,
    )

    @contextmanager
    def with_module(self, address: int):
        """Oeffnet ein Steuergeraet fuer die Dauer einer Anfrage.

        Die Weboberflaeche arbeitet mit einer dauerhaften Fahrzeugsitzung,
        waehrend der Fahrzustand weiterlaeuft. Steuergeraete beenden ihren
        Dialog nach kurzer Untaetigkeit selbst - der Aufbau wird deshalb
        einmal wiederholt, bevor ein Fehler an die Oberflaeche geht. Nach der
        Anfrage wird der Dialog geschlossen, damit kein Kanal am Bus bleibt.
        """
        # Die Sperre gilt fuer den gesamten Dialog: ein Tester kann immer nur
        # mit einem Steuergeraet sprechen. Ohne sie wuerden sich zwei
        # gleichzeitige Anfragen die Kanaele am Bus gegenseitig wegnehmen.
        with self.lock:
            if self.session.module(address) is None:
                # Nicht verbaute Adressen werden nicht wiederholt - der
                # zweite Versuch scheitert garantiert genauso.
                raise DiagnosticError(f"Adresse {address_text(address)} ist nicht verbaut")
            try:
                module = self.session.open_module(address)
            except self.RETRY_ERRORS as exc:
                self.session.close_module(address)
                self.log(f"Dialog {address_text(address)} neu aufbauen ({exc})")
                module = self.session.open_module(address)
            try:
                yield module
            finally:
                # Jede Anfrage schliesst ihren Dialog: sonst sammeln sich alte
                # Kanaele am Bus an, die fremde Dialoge mitlesen und quittieren.
                self.session.close_module(address)

    # -- Aufzeichnung (VCDS: Log) ------------------------------------------ #
    def start_log(
        self,
        address: int,
        groups: list[int],
        *,
        interval: float = 0.5,
        duration: float | None = None,
        basic: bool = False,
        out: Path | None = None,
    ) -> dict:
        """Messwerte im Hintergrund mitschreiben (Datei im VCDS-CSV-Format)."""
        with self.lock:
            self.stop_log()
            target = out or Path("var/logs") / f"messwerte-{address_text(address).replace(' ', '')}.csv"

            def sampler():
                columns, rows = [], []
                with self.with_module(address) as module:
                    for group in groups:
                        block = module.read_block(group, basic_setting=basic)
                        columns.extend(_columns_of(block))
                        rows.append(row_from_block(block))
                return columns, rows

            recorder = LogRecorder(
                sampler,
                interval=max(0.05, float(interval)),
                path=target,
                vehicle=self.session.name,
                module=f"{address_text(address)}",
            )
            recorder.duration = duration if duration and duration > 0 else None
            recorder.sample()  # erste Zeile legt die Spalten fest
            if not recorder.columns:
                raise DiagnosticError("Keine Messwerte gelesen - Aufzeichnung nicht gestartet")
            recorder.start()
            self.recorder = recorder
            self.record_error = ""
            return recorder.as_dict()

    def stop_log(self) -> dict:
        """Laufende Aufzeichnung beenden und speichern."""
        with self.lock:
            recorder = self.recorder
            if recorder is None:
                return {"laufend": False, "zeilen": [], "spalten": [], "gesamt": 0}
            recorder.stop()
            self.recorder = None
            return recorder.as_dict()

    def log_state(self) -> dict:
        with self.lock:
            if self.recorder is None:
                return {"laufend": False, "zeilen": [], "spalten": [], "gesamt": 0, "fehler": []}
            return self.recorder.as_dict()

    def log_csv(self) -> str:
        with self.lock:
            if self.recorder is None:
                return ""
            return self.recorder.csv_text()

    def obd(self, *, service: str = "info") -> dict:
        """OBD-II bedienen - dieselbe Sitzung fuer alle Dienste einer Anfrage."""
        with self.lock:
            with ObdSession(self.session) as obd:
                if service == "clear":
                    cleared = obd.clear_codes()
                    return {"transport": obd.transport_name, "geloescht": cleared}
                payload = obd.as_dict()
                payload["monitors"] = obd.monitors()
                payload["live"] = [entry.as_dict() for entry in obd.live()]
                payload["vag_codes"] = obd.vag_only_codes()
                payload["freeze_frame"] = [entry.as_dict() for entry in obd.freeze_frame()]
                return payload

    def scan(self) -> tuple[dict, str]:
        with self.lock:
            result = self.session.autoscan()
            payload = result.as_dict()
            report = format_autoscan(result)
            self.last_scan = payload
            self._last_report = report
            return payload, report

    @property
    def last_report(self) -> str:
        return self._last_report


def _columns_of(block: dict) -> list:
    """Spalten eines gelesenen Messwerteblocks (fuer die Aufzeichnung)."""
    from ..log import Column

    return [
        Column(
            group=int(block["group"]),
            field=int(entry["field"]),
            text=str(entry.get("text") or f"Feld {entry['field']}"),
            unit=str(entry.get("unit") or ""),
            basic=bool(block.get("basic")),
        )
        for entry in block["fields"]
    ]


class VagdiagRequestHandler(BaseHTTPRequestHandler):
    """HTTP-Handler mit JSON-API und statischer Seite."""

    server_version = f"VAGDiag/{__version__}"
    state: _AppState  # wird von VagdiagServer gesetzt

    # -- Hilfsfunktionen --------------------------------------------------- #
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - Signatur
        if self.state.log:
            self.state.log(f"HTTP {self.address_string()} {format % args}")

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=None).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists():
            self._send_text("Nicht gefunden", status=404)
            return
        data = path.read_bytes()
        content_type, _ = mimetypes.guess_type(path.name)
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    @staticmethod
    def _address(path: str) -> int:
        part = unquote(path)
        part = part[:-1] if part.endswith("/") else part
        return int(part, 16) if part.lower().startswith("0x") else int(part)

    # -- HTTP -------------------------------------------------------------- #
    def do_GET(self) -> None:  # noqa: N802 - von BaseHTTPRequestHandler vorgegeben
        parsed = urlparse(self.path)
        route = parsed.path
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}
        try:
            if route in ("/", "/index.html"):
                self._send_file(STATIC_DIR / "index.html")
                return
            if route == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
                return
            if route == "/api/state":
                self._send_json(self.state.state())
                return
            if route == "/api/vehicles":
                self._send_json({"vehicles": self.state.vehicles()})
                return
            if route == "/api/scan":
                payload, report = self.state.scan()
                self._send_json({"result": payload, "report": report})
                return
            if route == "/api/trace":
                count = int(query.get("count", "60"))
                self._send_json(self.state.session.trace_tail(count))
                return
            if route.startswith("/api/module/"):
                address = self._address(route.split("/api/module/", 1)[1])
                self._module_info(address, query)
                return
            if route.startswith("/api/faults/"):
                address = self._address(route.split("/api/faults/", 1)[1])
                with self.state.with_module(address) as module:
                    self._send_json(
                        {"address": address, "faults": [f.as_dict() for f in module.read_faults()]}
                    )
                return
            if route.startswith("/api/blocks/"):
                address = self._address(route.split("/api/blocks/", 1)[1])
                group = int(query.get("group", "1"))
                basic = query.get("basic") in ("1", "true", "on")
                with self.state.with_module(address) as module:
                    self._send_json(module.read_block(group, basic_setting=basic))
                return
            if route == "/api/obd":
                self._send_json(self.state.obd())
                return
            if route == "/api/log":
                self._send_json(self.state.log_state())
                return
            if route == "/api/log.csv":
                text = self.state.log_csv()
                if not text:
                    self._send_text("Keine Aufzeichnung vorhanden", status=404)
                    return
                self._send_text(
                    text,
                    content_type="text/csv",
                )
                return
            if route.startswith("/api/actuator/"):
                address = self._address(route.split("/api/actuator/", 1)[1])
                with self.state.with_module(address) as module:
                    self._send_json(
                        {
                            "address": address,
                            "address_text": address_text(address),
                            "module": module.module.name,
                            "actuators": module.actuators(),
                            "active": module.actuator_active,
                            "basic": module.basic_settings(),
                            "basic_active": module.basic_setting_group,
                        }
                    )
                return
            if route.startswith("/api/adapt/"):
                address = self._address(route.split("/api/adapt/", 1)[1])
                with self.state.with_module(address) as module:
                    if "channel" in query:
                        self._send_json(module.adaptation_read(int(query["channel"])))
                    else:
                        self._send_json(
                            {"address": address, "channels": module.adaptations()}
                        )
                return
            self._send_text("Unbekannter Pfad", status=404)
        except (DiagnosticError, ValueError, KeyError) as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001 - Oberflaeche soll nie haengen
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_POST(self) -> None:  # noqa: N802 - von BaseHTTPRequestHandler vorgegeben
        route = urlparse(self.path).path
        body = self._read_json()
        try:
            if route == "/api/vehicle":
                self._send_json(self.state.switch_vehicle(str(body.get("name", ""))))
                return
            if route == "/api/scan":
                payload, report = self.state.scan()
                self._send_json({"result": payload, "report": report})
                return
            if route.startswith("/api/faults/"):
                address = self._address(route.split("/api/faults/", 1)[1])
                with self.state.with_module(address) as module:
                    if body.get("clear"):
                        module.clear_faults()
                    self._send_json(
                        {
                            "address": address,
                            "cleared": bool(body.get("clear")),
                            "faults": [f.as_dict() for f in module.read_faults()],
                        }
                    )
                return
            if route.startswith("/api/coding/"):
                address = self._address(route.split("/api/coding/", 1)[1])
                with self.state.with_module(address) as module:
                    if body.get("login"):
                        module.login(int(str(body["login"])))
                    if body.get("value") is not None:
                        result = module.write_coding(str(body["value"]))
                    else:
                        result = {"ok": True}
                    payload = module.coding()
                    payload.update(result)
                    self._send_json(payload)
                return
            if route == "/api/obd":
                self._send_json(self.state.obd(service=str(body.get("dienst", "info"))))
                return
            if route == "/api/log/start":
                self._send_json(
                    self.state.start_log(
                        int(body.get("address", 1)),
                        [
                            int(part)
                            for part in str(body.get("blocks", "1")).split(",")
                            if part.strip()
                        ],
                        interval=float(body.get("interval", 0.5)),
                        duration=float(body.get("duration", 0)) or None,
                        basic=bool(body.get("basic")),
                    )
                )
                return
            if route == "/api/log/stop":
                self._send_json(self.state.stop_log())
                return
            if route.startswith("/api/actuator/"):
                address = self._address(route.split("/api/actuator/", 1)[1])
                with self.state.with_module(address) as module:
                    if body.get("basic") is not None:
                        result = module.basic_setting(
                            int(body["basic"]), bool(body.get("on", True))
                        )
                    else:
                        result = module.output_test(int(body.get("actuator", 0)))
                    payload = dict(result)
                    payload["actuators"] = module.actuators()
                    # "active" ist der Zustand der Stellglieddiagnose,
                    # "basic_on"/"basic_active" der der Grundeinstellung.
                    payload["active"] = module.actuator_active
                    payload["basic_active"] = module.basic_setting_group
                    if "active" in result:
                        payload["basic_on"] = bool(result["active"])
                    self._send_json(payload)
                return
            if route.startswith("/api/adapt/"):
                address = self._address(route.split("/api/adapt/", 1)[1])
                with self.state.with_module(address) as module:
                    channel = int(body.get("channel", 0))
                    if body.get("value") is None:
                        self._send_json(module.adaptation_read(channel))
                    else:
                        module.adaptation_save(channel, int(body["value"]))
                        self._send_json(module.adaptation_read(channel))
                return
            self._send_text("Unbekannter Pfad", status=404)
        except (DiagnosticError, ValueError, KeyError) as exc:
            self._send_json({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    # -- Modulinformationen ------------------------------------------------- #
    def _module_info(self, address: int, query: dict) -> None:
        with self.state.with_module(address) as module:
            ident = module.ident()
            payload = {
                "address": address,
                "address_text": address_text(address),
                "ident": ident,
                "coding": module.coding(),
                "adaptations": module.adaptations(),
                "blocks": module.block_numbers(),
                "label": (module.label() or {}).get("name") or "",
            }
            if query.get("block"):
                payload["block"] = module.read_block(int(query["block"]))
            self._send_json(payload)


class VagdiagServer(ThreadingHTTPServer):
    """HTTP-Server mit gemeinsamem Anwendungszustand."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: _AppState, *, verbose: bool = False):
        handler = type(
            "BoundVagdiagHandler", (VagdiagRequestHandler,), {"state": state, "verbose": verbose}
        )
        super().__init__(address, handler)
        self.state = state


def run_server(
    *,
    db: Database | None = None,
    host: str = "0.0.0.0",
    port: int = 8000,
    vehicle: str | None = None,
    speed: float = 6.0,
    open_browser: bool = False,
    log=None,
) -> int:
    """Startet die Weboberflaeche und blockiert bis zum Abbruch."""
    database = db or Database()
    state = _AppState(database, vehicle=vehicle, speed=speed, log=log)
    httpd = VagdiagServer((host, port), state)
    actual_port = httpd.server_address[1]
    url = f"http://{'localhost' if host in ('0.0.0.0', '::') else host}:{actual_port}/"
    print(f"{APP_NAME} {__version__} - Weboberflaeche auf {url}")
    print(f"   Fahrzeug : {state.session.name}")
    print(f"   Datenbank: {database.path}")
    print("   Beenden mit Strg+C")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nServer beendet.")
    finally:
        state.session.stop()
        httpd.server_close()
    return 0
