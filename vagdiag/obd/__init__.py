"""Fachlogik des VCDS-Nachbaus.

* :mod:`vagdiag.obd.dtc`    Fehlercode-Kodierung (SAE/OBD-II, VAG-5-stellig, UDS)
* :mod:`vagdiag.obd.blocks` Messwertblock-Datentypen und Formatierung
* :mod:`vagdiag.obd.scan`   Autoscan-Planung und Fehlerspeicherbericht
"""

from . import blocks, dtc, scan

__all__ = ["blocks", "dtc", "scan"]
