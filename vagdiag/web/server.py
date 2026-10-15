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
from ..obd.scan import format_autoscan
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

    # -- Fahrzeug ---------------------------------------------------------- #
    def switch_vehicle(self, name: str) -> dict:
        with self.lock:
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
    @contextmanager
    def with_module(self, address: int):
        """Oeffnet ein Steuergeraet; bei abgelaufenem Dialog wird neu verbunden."""
        with self.lock:
            try:
                module = self.session.open_module(address)
            except (DiagnosticError, TimeoutError, OSError) as exc:
                # Dialog abgelaufen (Steuergeraet wartet nicht endlos) -> neu aufbauen
                self.session.close_module(address)
                self.log(f"Verbindung zu 0x{address:02X} neu aufbauen ({exc})")
                module = self.session.open_module(address)
        yield module

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


__all__ = ["VagdiagServer", "run_server", "load_vehicle", "STATIC_DIR"]
