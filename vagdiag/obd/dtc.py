"""Fehlercodes: SAE/OBD-II, VAG-5-stellig und UDS.

Drei Kodierungen begegnen uns im VAG-Umfeld:

``KWP1281 (VAG-5-stellig)``
    Zweibyte-Code, angezeigt als fuenfstellige Dezimalzahl (z. B. ``00575``) plus
    Statusbyte. Der Wert ``0xFFFF 0x88`` markiert das Listenende.

``KWP2000/OBD-II (SAE J2012)``
    16-Bit-Code, angezeigt als ``P0420``: zwei Bit fuer den Buchstaben
    (``P``/``C``/``B``/``U``), zwei Bit fuer die erste Ziffer, danach die
    restlichen Zeichen in Hexadezimal-Schreibweise.

``UDS (ISO 14229)``
    Dreibyte-DTC: 16-Bit-Code nach SAE-J2012-Schema plus Fehlerart-Byte (FTB).

Zusaetzlich gibt es eine *heuristische* Umrechnung VAG-5-stellig -> SAE fuer den
Bereich 16384..17407 (``P = VAG - 16384``), der in der Literatur durchgaengig
bestaetigte Beispiele hat (16804 -> P0420, 16795 -> P0411, 17511 -> P1127).
Ausserhalb dieses Bereichs ist die Umrechnung **nicht** eindeutig; hier hilft
nur die Tabelle in der Datenbank (siehe ``docs/DATENBANK.md``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "DtcInfo",
    "decode_sae16",
    "encode_sae16",
    "decode_uds_dtc",
    "vag_to_sae",
    "sae_to_vag_range",
    "format_vag",
    "parse_dtc_text",
    "status_text",
    "DTC_LETTERS",
]

DTC_LETTERS = ("P", "C", "B", "U")

# Heuristikbereich VAG 5-stellig -> P-Code (siehe Modul-Docstring)
VAG_HEURISTIC_MIN = 16384
VAG_HEURISTIC_MAX = 17407

_DTC_TEXT_RE = re.compile(r"^\s*([PCBU])\s*0*([0-9A-F]{4})\s*$", re.IGNORECASE)


def decode_sae16(code: int) -> str:
    """Wandelt einen 16-Bit-Code in die SAE-Schreibweise (z. B. ``P0420``)."""
    code &= 0xFFFF
    letter = DTC_LETTERS[(code >> 14) & 0x03]
    return f"{letter}{code & 0x3FFF:04X}"


def encode_sae16(text: str) -> int:
    """Gegenstueck zu :func:`decode_sae16` (``P0420`` -> ``0x0420``)."""
    match = _DTC_TEXT_RE.match(text)
    if not match:
        raise ValueError(f"Kein gueltiger SAE-Fehlercode: {text!r}")
    letter, digits = match.group(1).upper(), match.group(2).upper()
    return (DTC_LETTERS.index(letter) << 14) | int(digits, 16)


def decode_uds_dtc(data: bytes) -> dict:
    """Zerlegt einen UDS-DTC (3 Bytes) in Code und Fehlerart."""
    if len(data) < 3:
        raise ValueError("UDS-DTC benoetigt 3 Bytes")
    code16 = (data[0] << 8) | data[1]
    return {
        "raw": data[:3].hex(" ").upper(),
        "code16": code16,
        "sae": decode_sae16(code16),
        "ftb": data[2],
        "ftb_hex": f"0x{data[2]:02X}",
    }


def vag_to_sae(vag_code: int) -> str | None:
    """Heuristische Umrechnung eines VAG-Codes in die SAE-Schreibweise.

    Nur fuer den Bereich 16384..17407 belegt (siehe Modul-Docstring); fuer
    alles andere wird ``None`` geliefert - die Datenbank kann dort genauere
    Angaben liefern.
    """
    if VAG_HEURISTIC_MIN <= vag_code <= VAG_HEURISTIC_MAX:
        return f"P{vag_code - VAG_HEURISTIC_MIN:04d}"
    return None


def sae_to_vag_range(sae: str) -> int | None:
    """Umkehrung von :func:`vag_to_sae`, sofern der Code in den Bereich faellt."""
    code16 = encode_sae16(sae)
    if (code16 >> 14) != 0:
        return None
    value = code16 & 0x3FFF
    if 0 <= value <= 1023:
        return VAG_HEURISTIC_MIN + value
    return None


def format_vag(code: int) -> str:
    """VAG-Code in VCDS-Schreibweise (fuenfstellig, dezimal)."""
    return f"{code:05d}"


def status_text(status: int) -> str:
    """Statusbyte als VCDS-String ``AA-BB`` (BB = 10 bei "sporadisch")."""
    return f"{status & 0x7F:02d}-{(status >> 7) * 10:02d}"


def parse_dtc_text(text: str) -> tuple[str, int | None]:
    """Erkennt ``P0420``, ``0420`` oder ``00575`` und liefert (Art, Wert)."""
    raw = text.strip()
    if not raw:
        raise ValueError("Leerer Fehlercode")
    if _DTC_TEXT_RE.match(raw):
        return "sae", encode_sae16(raw)
    if raw.isdigit():
        return "vag", int(raw)
    if raw.upper().startswith("0X"):
        return "raw16", int(raw, 16)
    raise ValueError(f"Fehlercode nicht erkannt: {text!r}")


@dataclass
class DtcInfo:
    """Aufbereiteter Fehlercode fuer Anzeige und Datenbank."""

    vag_code: int | None = None
    sae_code: str | None = None
    status: int | None = None
    text: str = ""
    text_short: str = ""
    source: str = ""
    #: "vag" = KWP1281-Statusbyte (17-10), "uds" = UDS-Statusbits
    status_style: str = "vag"

    @property
    def status_str(self) -> str:
        if self.status_style == "uds":
            return self.text_short or "kein Status"
        return "" if self.status is None else status_text(self.status)

    @property
    def sporadic(self) -> bool:
        return bool(self.status is not None and self.status & 0x80)

    @property
    def display_code(self) -> str:
        if self.vag_code is not None:
            return format_vag(self.vag_code)
        return self.sae_code or ""

    def as_dict(self) -> dict:
        return {
            "code": self.display_code,
            "vag_code": self.vag_code,
            "sae_code": self.sae_code,
            "status": None if self.status is None else f"0x{self.status:02X}",
            "status_text": self.status_str,
            "sporadic": self.sporadic,
            "text": self.text,
            "text_short": self.text_short,
            "source": self.source,
        }
