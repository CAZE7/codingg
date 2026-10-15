"""Weboberflaeche des VCDS-Nachbaus (Python-Standardbibliothek).

``vagdiag.web.server`` liefert die Seite ``static/index.html`` (CSS und
JavaScript inline) sowie die JSON-Schnittstelle fuer Fahrzeugzustand,
Steuergeraete, Messwertebloecke, Fehlerspeicher, Codierung und Anpassung.
"""

from .server import VagdiagServer, run_server

__all__ = ["VagdiagServer", "run_server"]
