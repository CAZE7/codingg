"""Fahrzeugsimulator des VCDS-Nachbaus.

Der Simulator stellt eine komplette Fahrzeugelektronik bereit, ohne dass
Hardware angeschlossen werden muss:

* :mod:`vagdiag.sim.profile` - Fahrzustand (Drehzahl, Temperatur, ...) mit
  Fahrprofil
* :mod:`vagdiag.sim.roles`   - Messwertebloecke und Fehlersaetze je Steuergeraeterolle
* :mod:`vagdiag.sim.ecu`     - simuliertes Steuergeraet mit KWP1281-, KWP2000-,
  UDS-, ISO-TP- und VW-TP-2.0-Server
* :mod:`vagdiag.sim.vehicles`- Fahrzeuge inklusive Busverdrahtung
"""

from .ecu import SimulatedEcu
from .profile import DriveProfile, VehicleState
from .vehicles import SimulatedVehicle, list_vehicles, load_vehicle

__all__ = [
    "SimulatedEcu",
    "SimulatedVehicle",
    "VehicleState",
    "DriveProfile",
    "list_vehicles",
    "load_vehicle",
]
