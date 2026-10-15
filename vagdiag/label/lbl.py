"""Label-Dateien (VCDS-Format).

VCDS beschreibt Messwertebloecke, Codier- und Anpassungshilfen in Textdateien
(``*.lbl``). Der hier implementierte Parser deckt die oeffentlich dokumentierte
Syntax ab (vgl. Referenzparser ``PyVCDS/label.py``):

``; Kommentar``
    Kommentarzeile.
``Redir: <Name>``
    Verweis auf eine weitere Label-Datei (VCDS-Redirect).
``:NNN,F``
    Abschnittsueberschrift: Block ``NNN``, Feld ``F`` (``0`` = Blocktitel).
``NNN,F,Text``
    Messwertbeschriftung (Block ``NNN``, Feld ``F``).
``BNNN,F,Text``
    Eintrag fuer die Grundeinstellung ("Basic Setting").
``C<Gruppe>,<Wert>,Text``
    Eintrag der Codierhilfe ("Coding").
``C<Gruppe>,<Bit>,<Wert>,Text``
    Codierhilfe mit Bitposition (zweite Spalte = Bit, dritte = Wert).
``L<Gruppe>,<Wert>,Text``
    Eintrag der Login-/Anpassungshilfe. Ohne Wertangabe beschreibt der Text
    den Kanal (``L03,Adaption Foerderbeginn``).

Erweiterung dieser Implementierung (klar als solche gekennzeichnet): Ein
Messwert-Eintrag darf zusaetzliche Spalten fuer Einheit und Grenzwerte
enthalten::

    NNN,F,Text,Kurztext,Einheit,Min,Max

Die Werte werden als Wahrheitstabelle in die Datenbank uebernommen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["LabelFile", "LabelEntry", "LabelCoding", "parse_label_text", "parse_label_file"]

_REDIR_RE = re.compile(r"^Redir\s*:\s*(\S+)\s*$", re.IGNORECASE)
_SECTION_RE = re.compile(r"^:(\d{1,3})(?:,(\d))?\s*$")
_MEASURE_RE = re.compile(r"^(\d{1,3}),(\d)(?:,(.*))?$")
_BASIC_RE = re.compile(r"^B(\d{1,3}),(\d)(?:,(.*))?$", re.IGNORECASE)
_CODING_RE = re.compile(r"^([CL])(\d{1,3}),(.*)$", re.IGNORECASE)


@dataclass
class LabelEntry:
    """Ein Messwert-/Grundeinstellungs-Eintrag einer Label-Datei."""

    block: int
    field: int
    text: str = ""
    short_text: str = ""
    unit: str = ""
    min_value: float | None = None
    max_value: float | None = None

    def as_dict(self) -> dict:
        return {
            "block": self.block,
            "field": self.field,
            "text": self.text,
            "short_text": self.short_text,
            "unit": self.unit,
            "min": self.min_value,
            "max": self.max_value,
        }


@dataclass
class LabelCoding:
    """Ein Eintrag aus Codier- oder Login-/Anpassungshilfe."""

    kind: str  # "C" oder "L"
    block: int
    bit: int | None
    value: int | None
    text: str

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "block": self.block,
            "bit": self.bit,
            "value": self.value,
            "text": self.text,
        }


@dataclass
class LabelFile:
    """Geparste Label-Datei."""

    name: str
    comments: list[str] = field(default_factory=list)
    redirects: list[str] = field(default_factory=list)
    measuring: list[LabelEntry] = field(default_factory=list)
    basic_setting: list[LabelEntry] = field(default_factory=list)
    coding: list[LabelCoding] = field(default_factory=list)

    # -- Abfragen ----------------------------------------------------------- #
    def block_text(self, block: int) -> str:
        for entry in self.measuring:
            if entry.block == block and entry.field == 0 and entry.text:
                return entry.text
        return ""

    def field_text(self, block: int, field_no: int) -> LabelEntry | None:
        for entry in self.measuring:
            if entry.block == block and entry.field == field_no:
                return entry
        return None

    def coding_for(self, block: int) -> list[LabelCoding]:
        return [entry for entry in self.coding if entry.block == block]

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "comments": self.comments,
            "redirects": self.redirects,
            "measuring": [e.as_dict() for e in self.measuring],
            "basic_setting": [e.as_dict() for e in self.basic_setting],
            "coding": [c.as_dict() for c in self.coding],
        }


def _split_columns(rest: str) -> list[str]:
    return [column.strip() for column in rest.split(",")]


def _parse_entry_rest(entry: LabelEntry, rest: str) -> None:
    columns = _split_columns(rest)
    if columns:
        entry.text = columns[0]
    if len(columns) > 1:
        entry.short_text = columns[1]
    if len(columns) > 2:
        entry.unit = columns[2]
    if len(columns) > 3:
        try:
            entry.min_value = float(columns[3])
        except ValueError:
            entry.min_value = None
    if len(columns) > 4:
        try:
            entry.max_value = float(columns[4])
        except ValueError:
            entry.max_value = None


def parse_label_text(text: str, name: str = "label.lbl") -> LabelFile:
    """Parst den Inhalt einer Label-Datei."""
    label = LabelFile(name=name)
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(";"):
            label.comments.append(line[1:].strip())
            continue
        if line.startswith("#"):
            label.comments.append(line[1:].strip())
            continue

        redirect = _REDIR_RE.match(line)
        if redirect:
            label.redirects.append(redirect.group(1))
            continue

        if _SECTION_RE.match(line):
            # Abschnittsueberschrift - die Zuordnung erfolgt ueber die Eintraege
            continue

        basic = _BASIC_RE.match(line)
        if basic:
            entry = LabelEntry(block=int(basic.group(1)), field=int(basic.group(2)))
            if basic.group(3):
                _parse_entry_rest(entry, basic.group(3))
            label.basic_setting.append(entry)
            continue

        coding = _CODING_RE.match(line)
        if coding:
            kind = coding.group(1).upper()
            block = int(coding.group(2))
            rest = _split_columns(coding.group(3))
            bit: int | None = None
            value: int | None = None
            offset = 0
            # Drei- und vierspaltige Form: Bit, Wert, Text ("C01,1,2,Schaltgetriebe")
            if len(rest) > 2 and rest[0].lstrip("-").isdigit() and rest[1].lstrip("-").isdigit():
                bit = int(rest[0])
                value = int(rest[1])
                offset = 2
            elif rest and rest[0].lstrip("-").isdigit():
                value = int(rest[0])
                offset = 1
            text_value = ", ".join(part for part in rest[offset:] if part)
            label.coding.append(
                LabelCoding(kind=kind, block=block, bit=bit, value=value, text=text_value)
            )
            continue

        measure = _MEASURE_RE.match(line)
        if measure:
            entry = LabelEntry(block=int(measure.group(1)), field=int(measure.group(2)))
            if measure.group(3):
                _parse_entry_rest(entry, measure.group(3))
            label.measuring.append(entry)
            continue
        # Unbekannte Zeile: als Kommentar behalten, damit nichts verloren geht
        label.comments.append(f"?? {line}")
    return label


def parse_label_file(path: str | Path) -> LabelFile:
    """Liest und parst eine Label-Datei von der Platte."""
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8", errors="replace")
    return parse_label_text(text, name=file_path.name)
