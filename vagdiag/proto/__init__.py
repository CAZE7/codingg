"""Protokollimplementierungen des VCDS-Nachbaus.

Enthaltene Ebenen (von unten nach oben)::

    bus.py      virtuelle Bussysteme: CAN (Frame-Ebene) und K-Line (Byte-Ebene)
    isotp.py    ISO 15765-2 (ISO-TP) Segmentierung fuer CAN
    vwtp.py     VW Transport Protokoll 2.0 (KWP-on-CAN / UDS-on-CAN)
    kw1281.py   KWP1281 (VAG-Blockprotokoll, K-Line, 5-Baud-Init)
    kwp2000.py  KWP2000 / ISO 14230 inkl. VAG-Dienstvariante
    uds.py      UDS / ISO 14229 (Teilmenge)

Alle Implementierungen arbeiten gegen die simulierten Busse in :mod:`vagdiag.proto.bus`,
koennen aber auch gegen reale Adapter (seriell/CAN-Hardware) verwendet werden, sofern
ein passender "Bus"-Adapter mit gleicher Schnittstelle bereitgestellt wird.
"""

from .bus import CanFrame, KLineBus, SimClock, TraceEntry, TraceRecorder, VirtualCanBus

__all__ = [
    "CanFrame",
    "KLineBus",
    "SimClock",
    "TraceEntry",
    "TraceRecorder",
    "VirtualCanBus",
]
