"""Steuergeraete-/Label-/Fehlercode-Datenbank (SQLite).

Aufbau
------
Die Datenbank wird aus den Seed-Dateien unter ``vagdiag/db/seed/`` erzeugt
(:meth:`Database.build`). Sie ist eine reine Datei-Datenbank, benoetigt also
keine Installation und laesst sich jederzeit neu aufbauen::

    python -m vagdiag.cli db build --force

Speicherort (in dieser Reihenfolge):

1. ``$VAGDIAG_DB``
2. ``<Repo>/var/vagdiag.sqlite3``

Erweiterbarkeit
---------------
* ``import_dtc_csv()`` liest Fehlercodelisten aus CSV (``code;text;system``).
* ``load_label_dir()`` uebernimmt zusaetzliche VCDS-Label-Dateien (``*.lbl``)
  aus einem Verzeichnis - inklusive ``Redir:``-Verweisen.
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from .. import address_text
from ..label.lbl import LabelFile, parse_label_file
from ..obd import dtc as dtc_mod

__all__ = ["Database", "DatabaseError", "repo_root", "default_db_path"]

SCHEMA_VERSION = "3"


class DatabaseError(Exception):
    """Fehler beim Aufbau oder Lesen der Datenbank."""


def repo_root() -> Path:
    """Ermittelt das Projektverzeichnis (zwei Ebenen ueber diesem Modul)."""
    return Path(__file__).resolve().parents[2]


def seed_dir() -> Path:
    return Path(__file__).resolve().parent / "seed"


def default_db_path() -> Path:
    env = os.environ.get("VAGDIAG_DB")
    if env:
        return Path(env)
    return repo_root() / "var" / "vagdiag.sqlite3"


@dataclass
class ModuleInfo:
    """Steuergeraet eines Fahrzeugs."""

    address: int
    vehicle: str = ""
    protocol: str = ""
    bus: str = ""
    part_number: str = ""
    component: str = ""
    software: str = ""
    coding: str = ""
    wsc: str = ""
    label_file: str = ""
    dtc_set: str = ""
    name: str = ""
    category: str = ""
    description: str = ""

    @property
    def address_text(self) -> str:
        """Adresse in VCDS-Schreibweise (z. B. ``46`` fuer das Komfortsystem)."""
        return address_text(self.address)

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "address_text": self.address_text,
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "vehicle": self.vehicle,
            "protocol": self.protocol,
            "bus": self.bus,
            "part_number": self.part_number,
            "component": self.component,
            "software": self.software,
            "coding": self.coding,
            "wsc": self.wsc,
            "label_file": self.label_file,
            "dtc_set": self.dtc_set,
        }


class Database:
    """Zugriff auf die VAGDiag-Datenbank."""

    def __init__(self, path: str | Path | None = None, *, auto_build: bool = True) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self._connection: sqlite3.Connection | None = None
        if auto_build and not self.path.exists():
            self.build()

    # ------------------------------------------------------------------ #
    # Verbindung / Aufbau
    # ------------------------------------------------------------------ #
    def connect(self) -> sqlite3.Connection:
        if self._connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def build(self, *, force: bool = False, verbose: bool = False) -> Path:
        """Erzeugt die Datenbank aus den Seed-Dateien."""
        if self.path.exists():
            if not force:
                return self.path
            self.close()
            self.path.unlink()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        schema = (Path(__file__).resolve().parent / "schema.sql").read_text(encoding="utf-8")
        connection.executescript(schema)

        seed = seed_dir()
        self._seed_control_modules(connection, seed / "control_modules.json")
        self._seed_vehicles(connection, seed / "vehicles.json")
        self._seed_dtcs(connection, seed / "dtc_vag.json", seed / "dtc_sae.json")
        self._seed_labels(connection, seed / "labels")
        self._seed_adaptations(connection, seed / "adaptations.json")

        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", ("schema_version", SCHEMA_VERSION)
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("built_at", time.strftime("%Y-%m-%d %H:%M:%S")),
        )
        connection.commit()
        if verbose:
            print(f"Datenbank aufgebaut: {self.path}")
        return self.path

    # -- Seed-Schritte --------------------------------------------------- #
    def _seed_control_modules(self, connection: sqlite3.Connection, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        source = data.get("source", "")
        connection.executemany(
            """INSERT OR REPLACE INTO control_module
               (address, name, name_en, category, description, obd_relevant, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    int(module["address"]),
                    module["name"],
                    module.get("name_en", ""),
                    module.get("category", ""),
                    module.get("description", ""),
                    int(module.get("obd_relevant", 0)),
                    source,
                )
                for module in data["modules"]
            ],
        )
        connection.commit()

    def _seed_vehicles(self, connection: sqlite3.Connection, path: Path) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        source = data.get("source", "")
        platform_ids: dict[str, int] = {}
        for platform in data["platforms"]:
            cursor = connection.execute(
                """INSERT OR REPLACE INTO platform (name, years, protocol, bus, notes, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    platform["name"],
                    platform.get("years", ""),
                    platform.get("protocol", ""),
                    platform.get("bus", ""),
                    platform.get("notes", ""),
                    platform.get("source", source),
                ),
            )
            platform_ids[platform["name"]] = int(cursor.lastrowid)
        known = {
            int(row["address"])
            for row in connection.execute("SELECT address FROM control_module").fetchall()
        }
        for vehicle in data["vehicles"]:
            missing = sorted(
                {int(module["address"]) for module in vehicle.get("modules", [])} - known
            )
            if missing:
                raise DatabaseError(
                    f"{vehicle['name']}: Steuergeraeteadresse(n) "
                    + ", ".join(f"0x{address:02X}" for address in missing)
                    + " fehlen in control_modules.json"
                )
            cursor = connection.execute(
                """INSERT OR REPLACE INTO vehicle
                   (name, platform_id, model_year, engine, market, notes, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    vehicle["name"],
                    platform_ids.get(vehicle["platform"]),
                    vehicle.get("model_year"),
                    vehicle.get("engine", ""),
                    vehicle.get("market", ""),
                    vehicle.get("notes", ""),
                    vehicle.get("source", source),
                ),
            )
            vehicle_id = int(cursor.lastrowid)
            for module in vehicle.get("modules", []):
                connection.execute(
                    """INSERT OR REPLACE INTO vehicle_module
                       (vehicle_id, address, protocol, bus, part_number, component,
                        software, coding, wsc, label_file, dtc_set)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        vehicle_id,
                        int(module["address"]),
                        module.get("protocol", ""),
                        module.get("bus", ""),
                        module.get("part_number", ""),
                        module.get("component", ""),
                        module.get("software", ""),
                        module.get("coding", ""),
                        module.get("wsc", ""),
                        module.get("label_file", ""),
                        module.get("dtc_set", ""),
                    ),
                )
        connection.commit()

    def _seed_dtcs(self, connection: sqlite3.Connection, vag_path: Path, sae_path: Path) -> None:
        rows: list[tuple] = []
        vag = json.loads(vag_path.read_text(encoding="utf-8"))
        vag_source = vag.get("source", "")
        for entry in vag.get("codes", []):
            code = f"{int(entry['vag']):05d}"
            rows.append(
                (
                    code,
                    "vag",
                    int(entry["vag"]),
                    dtc_mod.vag_to_sae(int(entry["vag"])),
                    entry["text"],
                    entry.get("short", ""),
                    entry.get("system", ""),
                    entry.get("status_hint", ""),
                    vag_source,
                )
            )
        for entry in vag.get("vag16_codes", []):
            code = f"{int(entry['vag']):05d}"
            rows.append(
                (
                    code,
                    "vag",
                    int(entry["vag"]),
                    entry.get("sae") or dtc_mod.vag_to_sae(int(entry["vag"])),
                    entry["text"],
                    entry.get("short", ""),
                    entry.get("system", ""),
                    entry.get("status_hint", ""),
                    vag_source,
                )
            )
        sae = json.loads(sae_path.read_text(encoding="utf-8"))
        sae_source = sae.get("source", "")
        for entry in sae.get("codes", []):
            rows.append(
                (
                    entry["code"],
                    "sae",
                    dtc_mod.sae_to_vag_range(entry["code"]),
                    entry["code"],
                    entry["text"],
                    entry.get("short", ""),
                    entry.get("system", ""),
                    entry.get("status_hint", ""),
                    sae_source,
                )
            )
        connection.executemany(
            """INSERT OR REPLACE INTO dtc
               (code, kind, vag_code, sae_code, text, text_short, system, status_hint, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        connection.commit()

    def _seed_labels(self, connection: sqlite3.Connection, directory: Path) -> None:
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.lbl")):
            label = parse_label_file(path)
            self._insert_label(connection, label)
        connection.commit()

    def _seed_adaptations(self, connection: sqlite3.Connection, path: Path) -> None:
        if not path.exists():
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        source = data.get("source", "")
        for entry in data.get("channels", []):
            label_id = None
            if entry.get("label_file"):
                row = connection.execute(
                    "SELECT id FROM label_file WHERE name = ?", (entry["label_file"],)
                ).fetchone()
                label_id = row["id"] if row else None
            connection.execute(
                """INSERT INTO adaptation_channel
                   (label_id, module_address, channel, text, min_value, max_value, unit, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    label_id,
                    entry.get("address"),
                    int(entry["channel"]),
                    entry.get("text", ""),
                    entry.get("min"),
                    entry.get("max"),
                    entry.get("unit", ""),
                    entry.get("source", source),
                ),
            )
        connection.commit()

    def _insert_label(self, connection: sqlite3.Connection, label: LabelFile) -> int:
        connection.execute(
            """INSERT OR REPLACE INTO label_file (name, part_number, notes, source)
               VALUES (?, ?, ?, ?)""",
            (label.name, label.name.replace(".lbl", ""), "", "Seed-Labeldatei"),
        )
        row = connection.execute(
            "SELECT id FROM label_file WHERE name = ?", (label.name,)
        ).fetchone()
        label_id = int(row["id"])
        connection.execute("DELETE FROM measuring_block WHERE label_id = ?", (label_id,))
        connection.execute("DELETE FROM coding_entry WHERE label_id = ?", (label_id,))
        connection.execute("DELETE FROM label_redirect WHERE label_id = ?", (label_id,))
        for target in label.redirects:
            connection.execute(
                "INSERT OR REPLACE INTO label_redirect(label_id, target) VALUES (?, ?)",
                (label_id, target),
            )
        connection.executemany(
            """INSERT OR REPLACE INTO measuring_block
               (label_id, block, field, basic, text, short_text, unit, min_value, max_value)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    label_id,
                    entry.block,
                    entry.field,
                    basic,
                    entry.text,
                    entry.short_text,
                    entry.unit,
                    entry.min_value,
                    entry.max_value,
                )
                for basic, entries in ((0, label.measuring), (1, label.basic_setting))
                for entry in entries
            ],
        )
        connection.executemany(
            """INSERT INTO coding_entry (label_id, kind, block, bit, value, text)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [(label_id, c.kind, c.block, c.bit, c.value, c.text) for c in label.coding],
        )
        return label_id

    # ------------------------------------------------------------------ #
    # Import (Erweiterung)
    # ------------------------------------------------------------------ #
    def import_dtc_csv(self, path: str | Path) -> int:
        """Importiert Fehlercodes aus CSV (``code;text;system``, ``#`` = Kommentar)."""
        connection = self.connect()
        count = 0
        with open(path, newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle, delimiter=";")
            for row in reader:
                if not row or row[0].strip().startswith("#"):
                    continue
                code_text = row[0].strip()
                if not code_text:
                    continue
                try:
                    kind, value = dtc_mod.parse_dtc_text(code_text)
                except ValueError:
                    continue
                text = row[1].strip() if len(row) > 1 else ""
                system = row[2].strip() if len(row) > 2 else ""
                if kind == "sae":
                    sae = dtc_mod.decode_sae16(value)
                    connection.execute(
                        """INSERT OR REPLACE INTO dtc
                           (code, kind, vag_code, sae_code, text, system, source)
                           VALUES (?, 'sae', ?, ?, ?, ?, ?)""",
                        (sae, dtc_mod.sae_to_vag_range(sae), sae, text, system, f"CSV-Import {path}"),
                    )
                else:
                    connection.execute(
                        """INSERT OR REPLACE INTO dtc
                           (code, kind, vag_code, sae_code, text, system, source)
                           VALUES (?, 'vag', ?, ?, ?, ?, ?)""",
                        (
                            f"{value:05d}",
                            value,
                            dtc_mod.vag_to_sae(value),
                            text,
                            system,
                            f"CSV-Import {path}",
                        ),
                    )
                count += 1
        connection.commit()
        return count

    def load_label_dir(self, directory: str | Path) -> int:
        """Uebernimmt zusaetzliche ``*.lbl``-Dateien aus einem Verzeichnis."""
        connection = self.connect()
        count = 0
        for path in sorted(Path(directory).glob("*.lbl")):
            label = parse_label_file(path)
            self._insert_label(connection, label)
            count += 1
        connection.commit()
        return count

    # ------------------------------------------------------------------ #
    # Abfragen
    # ------------------------------------------------------------------ #
    def stats(self) -> dict[str, int]:
        connection = self.connect()
        tables = [
            "control_module",
            "platform",
            "vehicle",
            "vehicle_module",
            "label_file",
            "measuring_block",
            "coding_entry",
            "dtc",
            "adaptation_channel",
        ]
        return {
            table: int(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
            for table in tables
        }

    def meta(self) -> dict[str, str]:
        connection = self.connect()
        return {
            row["key"]: row["value"]
            for row in connection.execute("SELECT key, value FROM meta").fetchall()
        }

    def vehicles(self) -> list[dict]:
        connection = self.connect()
        rows = connection.execute(
            """SELECT v.id, v.name, v.model_year, v.engine, v.market, v.notes,
                      p.name AS platform, p.protocol, p.bus
               FROM vehicle v LEFT JOIN platform p ON p.id = v.platform_id
               ORDER BY v.name"""
        ).fetchall()
        return [dict(row) for row in rows]

    def vehicle(self, name_or_id: str | int) -> dict | None:
        connection = self.connect()
        if isinstance(name_or_id, int) or str(name_or_id).isdigit():
            row = connection.execute(
                "SELECT * FROM vehicle WHERE id = ?", (int(name_or_id),)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM vehicle WHERE name = ? OR name LIKE ?",
                (str(name_or_id), f"%{name_or_id}%"),
            ).fetchone()
        return dict(row) if row else None

    def modules_for_vehicle(self, vehicle_name_or_id: str | int) -> list[ModuleInfo]:
        connection = self.connect()
        vehicle = self.vehicle(vehicle_name_or_id)
        if vehicle is None:
            return []
        rows = connection.execute(
            """SELECT vm.*, cm.name AS cm_name, cm.category AS cm_category,
                      cm.description AS cm_description
               FROM vehicle_module vm
               LEFT JOIN control_module cm ON cm.address = vm.address
               WHERE vm.vehicle_id = ?
               ORDER BY vm.address""",
            (vehicle["id"],),
        ).fetchall()
        return [
            ModuleInfo(
                address=int(row["address"]),
                vehicle=vehicle["name"],
                protocol=row["protocol"] or "",
                bus=row["bus"] or "",
                part_number=row["part_number"] or "",
                component=row["component"] or "",
                software=row["software"] or "",
                coding=row["coding"] or "",
                wsc=row["wsc"] or "",
                label_file=row["label_file"] or "",
                dtc_set=row["dtc_set"] or "",
                name=row["cm_name"] or "",
                category=row["cm_category"] or "",
                description=row["cm_description"] or "",
            )
            for row in rows
        ]

    def control_modules(self) -> list[dict]:
        connection = self.connect()
        rows = connection.execute(
            "SELECT * FROM control_module ORDER BY address"
        ).fetchall()
        return [dict(row) for row in rows]

    def control_module(self, address: int) -> dict | None:
        connection = self.connect()
        row = connection.execute(
            "SELECT * FROM control_module WHERE address = ?", (int(address),)
        ).fetchone()
        return dict(row) if row else None

    def dtc_lookup(self, code: str | int, *, kind: str | None = None) -> dict | None:
        """Sucht einen Fehlercode (VAG-5-stellig oder SAE) inkl. Heuristik."""
        connection = self.connect()
        text = str(code).strip()
        if text.upper().startswith(("P", "C", "B", "U")) or text.upper().startswith("0X"):
            parsed_kind, value = dtc_mod.parse_dtc_text(text)
            if parsed_kind == "sae":
                sae = dtc_mod.decode_sae16(value)
                row = connection.execute(
                    "SELECT * FROM dtc WHERE sae_code = ? OR code = ?", (sae, sae)
                ).fetchone()
                if row:
                    return dict(row)
                return {
                    "code": sae,
                    "kind": "sae",
                    "sae_code": sae,
                    "vag_code": dtc_mod.sae_to_vag_range(sae),
                    "text": "",
                    "source": "Heuristik (nicht in der Datenbank vorhanden)",
                }
            code_int = value
        else:
            code_int = int(text)
        row = connection.execute(
            "SELECT * FROM dtc WHERE vag_code = ? OR code = ?",
            (code_int, f"{code_int:05d}"),
        ).fetchone()
        if row:
            return dict(row)
        sae = dtc_mod.vag_to_sae(code_int)
        if sae:
            return {
                "code": f"{code_int:05d}",
                "kind": "vag",
                "vag_code": code_int,
                "sae_code": sae,
                "text": "",
                "source": "Heuristik P = VAG - 16384 (nicht in der Datenbank vorhanden)",
            }
        return None

    def label(self, name: str) -> dict | None:
        """Liefert Label-Datei inklusive Messwerteblocken und Codierhilfen."""
        connection = self.connect()
        row = connection.execute("SELECT * FROM label_file WHERE name = ?", (name,)).fetchone()
        if row is None:
            return None
        label_id = int(row["id"])
        blocks = connection.execute(
            """SELECT block, field, basic, text, short_text, unit, min_value, max_value
               FROM measuring_block WHERE label_id = ? ORDER BY basic, block, field""",
            (label_id,),
        ).fetchall()
        coding = connection.execute(
            """SELECT kind, block, bit, value, text FROM coding_entry
               WHERE label_id = ? ORDER BY kind, block, bit, value""",
            (label_id,),
        ).fetchall()
        redirects = connection.execute(
            "SELECT target FROM label_redirect WHERE label_id = ?", (label_id,)
        ).fetchall()
        return {
            "name": row["name"],
            "part_number": row["part_number"],
            "redirects": [r["target"] for r in redirects],
            "measuring": [dict(b) for b in blocks],
            "coding": [dict(c) for c in coding],
        }

    def label_for_module(self, module: ModuleInfo) -> dict | None:
        """Ermittelt die Label-Datei zu einem Steuergeraet (mit Redir-Aufloesung)."""
        for candidate in (module.label_file, f"{module.part_number}.lbl", "generic.lbl"):
            if not candidate:
                continue
            label = self.label(candidate)
            if label is None:
                continue
            seen: set[str] = set()
            while label and label["redirects"]:
                target = label["redirects"][0]
                if target in seen:
                    break
                seen.add(target)
                follow = self.label(target)
                if follow is None:
                    break
                merged = dict(follow)
                merged["measure_extra"] = label["measuring"]
                label = merged
            return label
        return None

    def block_text(self, label: dict | None, block: int, field: int, *, basic: bool = False) -> str:
        """Text zu einem Messwerteblockfeld (``field = 0`` = Blocktitel)."""
        if not label:
            return ""
        entries = [*label.get("measuring", []), *label.get("measure_extra", [])]
        for entry in entries:
            if (
                entry["block"] == block
                and entry["field"] == field
                and bool(entry.get("basic")) == bool(basic)
            ):
                return entry["text"] or ""
        return ""

    def adaptation_channels(self, module: ModuleInfo | None = None) -> list[dict]:
        connection = self.connect()
        if module is None:
            rows = connection.execute(
                "SELECT * FROM adaptation_channel ORDER BY channel"
            ).fetchall()
            return [dict(row) for row in rows]
        # Vorrang hat die Label-Datei des Steuergeraets (inklusive der per
        # Redir: verketteten Dateien); erst danach wird auf die Adresse
        # zurueckgegriffen - dann entdoppelt, damit Kanaele mit gleicher
        # Nummer nicht mehrfach erscheinen.
        seen: set[str] = set()
        collected: list[dict] = []
        for name in self._label_chain(module.label_file):
            rows = connection.execute(
                """SELECT ac.* FROM adaptation_channel ac
                   JOIN label_file lf ON lf.id = ac.label_id
                   WHERE lf.name = ? ORDER BY ac.channel""",
                (name,),
            ).fetchall()
            for row in rows:
                entry = dict(row)
                key = str(entry["channel"])
                if key in seen:
                    continue
                seen.add(key)
                collected.append(entry)
        if not collected:
            rows = connection.execute(
                """SELECT * FROM adaptation_channel
                   WHERE module_address = ? ORDER BY channel""",
                (module.address,),
            ).fetchall()
            for row in rows:
                entry = dict(row)
                key = str(entry["channel"])
                if key in seen:
                    continue
                seen.add(key)
                collected.append(entry)
        collected.sort(key=lambda entry: int(entry["channel"]))
        return collected

    def _label_chain(self, name: str) -> list[str]:
        """Label-Datei samt der Dateien, auf die per ``Redir:`` verwiesen wird."""
        if not name:
            return []
        connection = self.connect()
        chain = [name]
        seen = {name}
        while True:
            row = connection.execute(
                """SELECT target FROM label_redirect lr
                   JOIN label_file lf ON lf.id = lr.label_id
                   WHERE lf.name = ?""",
                (chain[-1],),
            ).fetchone()
            if row is None or row["target"] in seen:
                break
            seen.add(row["target"])
            chain.append(row["target"])
        return chain

    def adaptation_channels_for_module(self, module: ModuleInfo) -> list[dict]:
        """Anpassungskanaele eines Steuergeraets (Label-Datei, sonst Adresse)."""
        return self.adaptation_channels(module)

    def block_numbers(self, module: ModuleInfo) -> list[int]:
        """Messwertebloecke, die im Label des Steuergeraets hinterlegt sind."""
        connection = self.connect()
        rows = connection.execute(
            """SELECT DISTINCT mb.block FROM measuring_block mb
               JOIN label_file lf ON lf.id = mb.label_id
               WHERE lf.name = ? AND mb.block > 0 ORDER BY mb.block""",
            (module.label_file,),
        ).fetchall()
        if not rows and module.part_number:
            rows = connection.execute(
                """SELECT DISTINCT mb.block FROM measuring_block mb
                   JOIN label_file lf ON lf.id = mb.label_id
                   WHERE lf.part_number = ? AND mb.block > 0 ORDER BY mb.block""",
                (module.part_number,),
            ).fetchall()
        return [int(row["block"]) for row in rows]

    def dtc_list(self, *, kind: str | None = None, limit: int = 500, search: str = "") -> list[dict]:
        connection = self.connect()
        query = "SELECT * FROM dtc"
        params: list[Any] = []
        clauses: list[str] = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if search:
            clauses.append("(text LIKE ? OR code LIKE ? OR sae_code LIKE ?)")
            params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY code LIMIT ?"
        params.append(limit)
        rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]
