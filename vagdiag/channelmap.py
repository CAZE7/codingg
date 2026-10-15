"""Kanalmap - alle Werte eines Steuergeraets in einer Tabelle.

VCDS kann den Inhalt eines Steuergeraets vollstaendig ausgeben: jeden
Messwerteblock und jeden Anpassungskanal mit Text, Wert, Einheit und
erlaubtem Bereich. Genau das entsteht hier - als Liste von Zeilen, die sich
als Tabelle oder CSV-Datei ausgeben laesst.

Die Zeilen sind bewusst flach gehalten:

===========================  ==============================================
``art``                      ``Messwerteblock`` oder ``Anpassung``
``gruppe``                   Blocknummer (``001``) bzw. Kanal (``04``)
``feld``                     Feldnummer im Block (leer bei Anpassungen)
``text``                     Klartext aus der Labeldatei
``wert``                     aktueller Wert
``einheit``                  Einheit
``bereich``                  Grenzwerte bzw. Kanalbereich
===========================  ==============================================
"""

from __future__ import annotations

import csv
import io
from typing import Any

from .log import CSV_DELIMITER, CSV_ENCODING, CSV_NEWLINE, format_value

__all__ = ["channel_map", "to_csv", "COLUMNS"]

#: Spalten der Kanalmap
COLUMNS = ("art", "gruppe", "feld", "text", "wert", "einheit", "bereich")

def _safe(func, *arguments, **keywords):  # noqa: ANN001 - kleine Hilfe
    """Aufruf kapseln: ein unlesbarer Kanal beendet die Ausgabe nicht."""
    try:
        return func(*arguments, **keywords)
    except Exception as exc:  # noqa: BLE001 - unlesbare Kanaele nur vermerken
        return {"__error__": f"{type(exc).__name__}: {exc}"}


def channel_map(module, *, groups: list[int] | None = None, basic: bool = False) -> list[dict]:
    """Messwertebloecke und Anpassungskanaele eines Steuergeraets auslesen.

    ``groups`` begrenzt die Bloecke (Standard: alle Bloecke der Labeldatei);
    unlesbare Bloecke werden als Zeile mit Hinweis aufgefuehrt, damit die
    Ausgabe zeigt, was das Steuergeraet nicht beantwortet.
    """
    rows: list[dict] = []
    for group in groups if groups is not None else module.block_numbers():
        block = _safe(module.read_block, group, basic_setting=basic)
        if "__error__" in block:
            rows.append(
                {
                    "art": "Messwerteblock",
                    "gruppe": f"{group:03d}",
                    "feld": "",
                    "text": "nicht verfuegbar",
                    "wert": "",
                    "einheit": "",
                    "bereich": block["__error__"],
                }
            )
            continue
        for field in block["fields"]:
            bounds = ""
            if field.get("min_value") is not None or field.get("max_value") is not None:
                bounds = f"{field.get('min_value', '')} - {field.get('max_value', '')}".strip()
            rows.append(
                {
                    "art": "Messwerteblock",
                    "gruppe": f"{block['group']:03d}",
                    "feld": field["field"],
                    "text": field["text"],
                    "wert": field.get("display_full") or field.get("display") or "",
                    "einheit": field.get("unit") or "",
                    "bereich": bounds,
                }
            )
    for entry in module.adaptations():
        channel = int(entry["channel"])
        current = _safe(module.adaptation_read, channel)
        rows.append(
            {
                "art": "Anpassung",
                "gruppe": f"{channel:02d}",
                "feld": "",
                "text": entry.get("text") or current.get("description", ""),
                "wert": current.get("value", "") if "__error__" not in current else "",
                "einheit": current.get("unit", "") if "__error__" not in current else "",
                "bereich": f"{entry.get('min_value', '')} - {entry.get('max_value', '')}".strip(),
            }
        )
    return rows


def to_csv(rows: list[dict[str, Any]], *, vehicle: str = "", module: str = "") -> str:
    """Kanalmap als CSV-Text im Format der Messwertaufzeichnung."""
    buffer = io.StringIO()
    if vehicle:
        buffer.write(f"Fahrzeug: {vehicle}{CSV_NEWLINE}")
    if module:
        buffer.write(f"Steuergeraet: {module}{CSV_NEWLINE}")
    writer = csv.writer(buffer, delimiter=CSV_DELIMITER, lineterminator=CSV_NEWLINE)
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([format_value(row.get(column, "")) for column in COLUMNS])
    return buffer.getvalue()


def write_csv(path, rows: list[dict[str, Any]], **header) -> str:  # noqa: ANN001
    """Kanalmap in eine Datei schreiben und den Pfad zurueckgeben."""
    text = to_csv(rows, **header)
    with open(path, "w", encoding=CSV_ENCODING, newline="") as handle:
        handle.write(text)
    return str(path)
