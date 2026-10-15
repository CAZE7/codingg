"""Messwertprotokollierung (VCDS: "Log" / "Messwerte mitschreiben").

VCDS schreibt Messwerte als Textdatei mit Semikolon als Trennzeichen und
deutschem Dezimalkomma mit - eine Zeile je Abtastung, eine Spalte je
Messwert. Genau dieses Format entsteht hier, damit die Dateien in
Tabellenkalkulation oder Auswertung unveraendert weiterverwendet werden
koennen.

Aufbau:

* :func:`block_columns` liest einen Messwerteblock und liefert die
  Spaltenueberschriften (Blockspalte mit Nummer, Text und Einheit).
* :class:`LogRecorder` erzeugt daraus zyklisch Zeilen - entweder einzeln
  (:meth:`LogRecorder.sample`) oder als Hintergrundthread
  (:meth:`LogRecorder.start`).
* :meth:`LogRecorder.save` schreibt die CSV-Datei, :meth:`as_dict` liefert
  den Zustand fuer die Weboberflaeche.

Die Recorder-Klasse greift nie selbst auf den Bus zu: der Aufrufer uebergibt
einen ``sampler`` (Rueckruffunktion), der eine Abtastung als
``(Spalten, Werte)``-Paar liefert. So laesst sich dieselbe Klasse im
CLI (eigene Modulsitzung) und im Webserver (ueber dessen serialisierte
Dialogverwaltung) verwenden.
"""

from __future__ import annotations

import csv
import io
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

#: VCDS schreibt Semikolon-getrennte Dateien in Windows-Zeichensatz.
CSV_DELIMITER = ";"
CSV_ENCODING = "latin-1"
CSV_NEWLINE = "\r\n"

#: Abtastabstand in Sekunden, wenn nichts anderes angegeben wird.
DEFAULT_INTERVAL = 0.5

#: Spalten, die jede Protokollzeile einleitet.
TIME_COLUMNS = ("Zeit", "Sekunden")

#: Obergrenze der im Speicher gehaltenen Zeilen (die Datei waechst weiter).
MAX_ROWS_IN_MEMORY = 20000


@dataclass(frozen=True)
class Column:
    """Eine Messwertspalte: Kopfzeile plus optionale Einheit."""

    group: int
    field: int
    text: str
    unit: str = ""
    basic: bool = False

    @property
    def header(self) -> str:
        """Spaltenkopf wie in VCDS: ``Block 001  Drehzahl (1/min)``."""
        prefix = "Grundeinstellung" if self.basic else "Block"
        head = f"{prefix} {self.group:03d}  {self.text}"
        return f"{head} ({self.unit})" if self.unit else head


def block_columns(module: Any, group: int, *, basic_setting: bool = False) -> list[Column]:
    """Messwerteblock lesen und seine Spalten zurueckgeben."""
    block = module.read_block(group, basic_setting=basic_setting)
    columns = [
        Column(
            group=int(block["group"]),
            field=int(entry["field"]),
            text=str(entry.get("text") or f"Feld {entry['field']}"),
            unit=str(entry.get("unit") or ""),
            basic=bool(block.get("basic")),
        )
        for entry in block["fields"]
    ]
    return columns


def format_value(value: Any) -> str:
    """Zahl mit Komma und ohne ueberfluessige Nullen schreiben."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return (text or "0").replace(".", ",")
    return str(value)


def write_csv(
    path: str | Path,
    columns: Sequence[Column],
    rows: Iterable[dict[str, Any]],
    *,
    vehicle: str = "",
    module: str = "",
    extra_headers: Sequence[str] = (),
) -> Path:
    """CSV-Datei schreiben (Semikolon, Kopfzeilen wie in VCDS).

    ``rows`` sind Dictionaries mit ``Zeit``, ``Sekunden`` und je Spaltenschluessel
    (``f"{group}-{field}"``) einem Wert.
    """
    target = Path(path)
    if target.parent and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding=CSV_ENCODING, newline="") as handle:
        header = list(TIME_COLUMNS) + [column.header for column in columns]
        if vehicle:
            handle.write(f"Fahrzeug: {vehicle}{CSV_NEWLINE}")
        if module:
            handle.write(f"Steuergeraet: {module}{CSV_NEWLINE}")
        for line in extra_headers:
            handle.write(f"{line}{CSV_NEWLINE}")
        writer = csv.writer(handle, delimiter=CSV_DELIMITER, lineterminator=CSV_NEWLINE)
        writer.writerow(header)
        for row in rows:
            writer.writerow(
                [format_value(row.get(name, "")) for name in TIME_COLUMNS]
                + [format_value(row.get(column_key(column))) for column in columns]
            )
    return target


def column_key(column: Column) -> str:
    """Schluessel eines Messwerts in einer Protokollzeile."""
    suffix = "b" if column.basic else ""
    return f"{column.group}-{column.field}{suffix}"


def row_from_block(block: dict[str, Any]) -> dict[str, Any]:
    """Zeile aus einem gelesenen Messwerteblock bauen."""
    values: dict[str, Any] = {}
    for entry in block["fields"]:
        column = Column(
            group=int(block["group"]),
            field=int(entry["field"]),
            text=str(entry.get("text") or ""),
            unit=str(entry.get("unit") or ""),
            basic=bool(block.get("basic")),
        )
        values[column_key(column)] = entry.get("value")
        values[column.header] = entry.get("display_full") or entry.get("display") or ""
    return values


@dataclass
class LogRecorder:
    """Abtastung mehrerer Messwertebloecke in eine CSV-Datei.

    ``sampler`` liefert je Aufruf ``(columns, rows)``; ``rows`` ist eine Liste
    von Zeilen-Dictionaries (siehe :func:`row_from_block`). Der Recorder
    ergaenzt Zeitstempel und kann entweder einzeln abtasten oder als
    Hintergrundthread laufen.
    """

    sampler: Callable[[], tuple[Sequence[Column], list[dict[str, Any]]]]
    interval: float = DEFAULT_INTERVAL
    path: Path | None = None
    vehicle: str = ""
    module: str = ""
    #: Nach dieser Laufzeit stoppt der Thread von selbst (None = unbegrenzt).
    duration: float | None = None
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(default_factory=time.time)
    started_clock: float = field(default_factory=time.monotonic)
    rows: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    columns: list[Column] = field(default_factory=list)
    _thread: threading.Thread | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    # -- Aufzeichnung ------------------------------------------------------ #
    def sample(self, value_time: float | None = None) -> dict[str, Any] | None:
        """Eine Abtastung durchfuehren und die Zeile zurueckgeben."""
        try:
            columns, rows = self.sampler()
        except Exception as exc:  # noqa: BLE001 - Fehler nur mitschreiben
            with self._lock:
                self.errors.append(f"{type(exc).__name__}: {exc}")
            return None
        if columns:
            with self._lock:
                self.columns = list(columns)
        now = value_time if value_time is not None else self.clock()
        stamp = time.strftime("%H:%M:%S", time.localtime())
        seconds = now - self.started_clock
        # Alle Bloecke einer Abtastung stehen - wie in VCDS - in *einer* Zeile.
        line: dict[str, Any] = {"Zeit": stamp, "Sekunden": f"{seconds:.2f}"}
        for row in rows:
            line.update(row)
        with self._lock:
            self.rows.append(line)
            if len(self.rows) > MAX_ROWS_IN_MEMORY:
                del self.rows[: len(self.rows) - MAX_ROWS_IN_MEMORY]
        return line

    def start(self) -> None:
        """Hintergrundthread starten (mehrfacher Aufruf ist wirkungslos)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self.started_at = time.time()
        self.started_clock = self.clock()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="VAGDiag-Log", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Aufzeichnung beenden und - falls ein Pfad gesetzt ist - speichern."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None
        if self.path is not None:
            try:
                self.save()
            except OSError as exc:  # noqa: BLE001 - Speichern darf nicht werfen
                with self._lock:
                    self.errors.append(f"Speichern fehlgeschlagen: {exc}")

    @property
    def running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    def _run(self) -> None:
        started = self.started_clock
        first = True
        while not self._stop.is_set():
            if self.duration is not None and self.clock() - started >= self.duration:
                break
            try:
                self.sample()
            except Exception as exc:  # noqa: BLE001 - Thread darf nie sterben
                with self._lock:
                    self.errors.append(f"{type(exc).__name__}: {exc}")
            if first:
                first = False
            if self._stop.wait(self.interval):
                break
        if self.path is not None:
            try:
                self.save()
            except OSError as exc:  # noqa: BLE001
                with self._lock:
                    self.errors.append(f"Speichern fehlgeschlagen: {exc}")

    # -- Ausgabe ------------------------------------------------------------ #
    def save(self, path: str | Path | None = None) -> Path:
        """Zeilen als CSV schreiben und den Pfad zurueckgeben."""
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("Kein Pfad fuer die Protokolldatei angegeben")
        with self._lock:
            rows = list(self.rows)
            columns = list(self.columns)
        write_csv(
            target,
            columns,
            rows,
            vehicle=self.vehicle,
            module=self.module,
        )
        self.path = target
        return target

    def csv_text(self) -> str:
        """Aktuellen Stand als CSV-Text (fuer die Weboberflaeche)."""
        buffer = io.StringIO()
        header = list(TIME_COLUMNS) + [column.header for column in self.columns]
        writer = csv.writer(buffer, delimiter=CSV_DELIMITER, lineterminator=CSV_NEWLINE)
        writer.writerow(header)
        with self._lock:
            rows = list(self.rows)
        for row in rows:
            writer.writerow(
                [format_value(row.get(name, "")) for name in TIME_COLUMNS]
                + [format_value(row.get(column_key(column))) for column in self.columns]
            )
        return buffer.getvalue()

    def as_dict(self, *, row_limit: int = 200) -> dict[str, Any]:
        """Zustand fuer die Weboberflaeche (nur die letzten Zeilen)."""
        with self._lock:
            rows = list(self.rows[-row_limit:])
            errors = list(self.errors)
            total = len(self.rows)
        return {
            "laufend": self.running,
            "intervall": self.interval,
            "dauer": self.duration,
            "spalten": [column.header for column in self.columns],
            "zeilen": [dict(row) for row in rows],
            "gezeigt": len(rows),
            "gesamt": total,
            "fehler": errors,
            "datei": str(self.path) if self.path else "",
        }
