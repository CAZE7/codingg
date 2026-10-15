"""VAGDiag Studio - quelloffener VCDS-Nachbau.

Paketstruktur
-------------
``vagdiag.db``      Steuergeraete-/Label-/Fehlercode-Datenbank (SQLite)
``vagdiag.proto``   Protokollebenen: KWP1281, KWP2000, UDS, ISO-TP, VW TP 2.0
``vagdiag.obd``     Fachlogik: Fehlercodes, Messwertblock-Formate, Autoscan-Plan
``vagdiag.sim``     Fahrzeug-/Steuergeraete-Simulator (virtueller CAN- und K-Line-Bus)
``vagdiag.web``     Weboberflaeche im VCDS-Look (stdlib HTTP + WebSocket)
``vagdiag.cli``     Kommandozeile
"""

__version__ = "0.1.0"
APP_NAME = "VAGDiag Studio"
APP_SUBTITLE = "VCDS-Nachbau (quelloffen)"

__all__ = [
    "__version__",
    "APP_NAME",
    "APP_SUBTITLE",
    "address_text",
    "address_wire",
]


def address_text(address: int) -> str:
    """Steuergeraeteadresse in VCDS-Schreibweise ("01", "15", "46").

    Der VW-Konzern nummeriert Steuergeraete mit zwei hexadezimal notierten
    Ziffern; angezeigt werden sie genau so, wie sie im Adressplan stehen.
    """
    return f"{int(address):02d}"


def address_wire(address: int) -> int:
    """Adressbyte auf dem Bus: die Adresse "46" ist das Byte ``0x46``.

    VCDS-Adressen sind hexadezimal notiert - die Anzeige "46" entspricht auf
    der Leitung dem Byte ``0x46`` (70 dezimal), nicht ``0x2E``.
    """
    try:
        return int(address_text(address), 16)
    except ValueError:  # pragma: no cover - nur bei unsinnigen Adressen
        return int(address)
