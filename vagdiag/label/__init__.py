"""Label-Dateien des VCDS-Nachbaus."""

from .lbl import LabelCoding, LabelEntry, LabelFile, parse_label_file, parse_label_text

__all__ = [
    "LabelCoding",
    "LabelEntry",
    "LabelFile",
    "parse_label_file",
    "parse_label_text",
]
