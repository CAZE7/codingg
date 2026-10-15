"""Fahrzeuge der Simulation: Busverdrahtung und Steuergeraete.

Ein :class:`SimulatedVehicle` baut aus dem Datenbankeintrag eines Fahrzeugs
einen K-Line-Bus und/oder einen CAN-Bus, haengt die passenden Steuergeraete
daran und treibt den Fahrzustand ueber ein :class:`~vagdiag.sim.profile.DriveProfile`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable

from ..db import Database, ModuleInfo
from ..proto.bus import KLineBus, SimClock, TraceRecorder, VirtualCanBus
from .ecu import SimulatedEcu
from .profile import DriveProfile, VehicleState

__all__ = ["SimulatedVehicle", "list_vehicles", "load_vehicle"]


VIN_PREFIXES: tuple[tuple[str, str], ...] = (
    ("Golf V", "WVWZZZ1KZ"),
    ("Golf IV", "WVWZZZ1JZ"),
    ("Passat", "WVWZZZ3BZ"),
    ("A4", "WAUZZZ8EZ"),
    ("A3", "WAUZZZ8PZ"),
)


def build_vin(vehicle_name: str, vehicle_id: int = 0) -> str:
    """Fahrgestellnummer (17-stellig) fuer ein simuliertes Fahrzeug."""
    prefix = next(
        (value for key, value in VIN_PREFIXES if key.lower() in (vehicle_name or "").lower()),
        "WVWZZZ1JZ",
    )
    digits = (abs(int(vehicle_id or 0)) * 7919 + 267456) % 10**6
    return f"{prefix}{digits:06d}00"


@dataclass
class SimulatedVehicle:
    """Komplettes simuliertes Fahrzeug."""

    name: str
    state: VehicleState
    profile: DriveProfile
    kline_bus: KLineBus | None
    can_bus: VirtualCanBus | None
    ecus: list[SimulatedEcu] = field(default_factory=list)
    trace: TraceRecorder = field(default_factory=TraceRecorder)
    clock: SimClock = field(default_factory=lambda: SimClock(0.0))
    modules: list[ModuleInfo] = field(default_factory=list)
    started: bool = False
    vin: str = ""

    # -- Steuerung ----------------------------------------------------------- #
    def start(self) -> None:
        if self.started:
            return
        for ecu in self.ecus:
            ecu.start()
        self.profile.start()
        self.started = True

    def stop(self) -> None:
        self.profile.stop()
        for ecu in self.ecus:
            ecu.stop()
        self.started = False

    # -- Zugriff ------------------------------------------------------------- #
    def ecu(self, address: int) -> SimulatedEcu | None:
        for ecu in self.ecus:
            if ecu.address == address:
                return ecu
        return None

    def ecu_addresses(self) -> list[int]:
        return sorted(ecu.address for ecu in self.ecus)

    def module_info(self, address: int) -> ModuleInfo | None:
        for module in self.modules:
            if module.address == address:
                return module
        return None

    # Kurzform (wird u. a. vom Client und der Weboberflaeche genutzt)
    def module(self, address: int) -> ModuleInfo | None:
        return self.module_info(address)

    def status(self) -> dict:
        state = self.state
        return {
            "vehicle": self.name,
            "vin": self.vin,
            "started": self.started,
            "phase": self.profile.phase_name,
            "speed": round(state.speed_kmh, 1),
            "rpm": round(state.rpm),
            "gear": state.gear,
            "coolant": round(state.coolant_temp, 1),
            "intake": round(state.intake_temp, 1),
            "outside": round(state.outside_temp, 1),
            "boost": round(state.boost_mbar),
            "throttle": round(state.throttle_percent, 1),
            "battery": round(state.battery_v, 2),
            "fuel": round(state.fuel_level_l, 1),
            "odo": state.odo_km,
            "trip": round(state.trip_km, 1),
            "engine_running": state.engine_running,
            "lambda": round(state.lambda_value, 3),
            "modules": [
                {
                    "address": ecu.address,
                    "address_text": ecu.address_text,
                    "name": ecu.name,
                    "protocol": ecu.info.protocol,
                    "bus": ecu.info.bus,
                    "faults": len(ecu.module.faults),
                }
                for ecu in self.ecus
            ],
        }


def list_vehicles(db: Database | None = None) -> list[dict]:
    """Liefert alle in der Datenbank hinterlegten Fahrzeuge."""
    database = db or Database()
    return database.vehicles()


def load_vehicle(
    name_or_id: str | int | None = None,
    *,
    db: Database | None = None,
    speed: float = 1.0,
    clock_speed: float = 0.0,
    trace: TraceRecorder | None = None,
    log: Callable[[str], None] | None = None,
    drive: bool = True,
) -> SimulatedVehicle:
    """Baut ein simuliertes Fahrzeug aus der Datenbank auf."""
    database = db or Database()
    vehicles = database.vehicles()
    if not vehicles:
        raise RuntimeError("Keine Fahrzeuge in der Datenbank - 'vagdiag db build' ausfuehren")
    if name_or_id is None:
        vehicle_row = vehicles[0]
    else:
        row = database.vehicle(name_or_id)
        if row is None:
            raise KeyError(f"Fahrzeug nicht gefunden: {name_or_id}")
        vehicle_row = row

    modules = database.modules_for_vehicle(vehicle_row["id"])
    if not modules:
        raise RuntimeError(f"Keine Steuergeraete fuer {vehicle_row['name']} hinterlegt")

    recorder = trace or TraceRecorder()
    clock = SimClock(clock_speed)
    kline_bus = KLineBus(name="K-Line", trace=recorder, clock=clock) if any(
        (module.bus or "").lower().startswith("k") for module in modules
    ) else None
    can_bus = VirtualCanBus(name="CAN", trace=recorder, clock=clock) if any(
        not (module.bus or "").lower().startswith("k") for module in modules
    ) else None

    state = VehicleState(ambient_temp=14.5, outside_temp=14.5, interior_temp=21.0)
    profile = DriveProfile(state, speed=speed)

    vehicle = SimulatedVehicle(
        name=vehicle_row["name"],
        state=state,
        profile=profile,
        kline_bus=kline_bus,
        can_bus=can_bus,
        trace=recorder,
        clock=clock,
        modules=modules,
    )
    vehicle.vin = build_vin(vehicle_row["name"], vehicle_row.get("id") or 0)
    for module in modules:
        ecu = SimulatedEcu(
            module,
            state,
            kline_bus=kline_bus,
            can_bus=can_bus,
            db=database,
            log=log,
            vin=vehicle.vin,
        )
        vehicle.ecus.append(ecu)
    if not drive:
        profile.stop()
    return vehicle
