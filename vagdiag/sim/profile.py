"""Fahrzustand und Fahrprofil der Simulation.

Der Fahrzustand (:class:`VehicleState`) haelt alle Groessen, aus denen die
Messwertebloecke der simulierten Steuergeraete berechnet werden. Ein
:class:`DriveProfile` faehrt in einer Endlosschleife typische Phasen ab
(Leerlauf, Beschleunigen, Konstantfahrt, Schubbetrieb), damit die Anzeige im
VCDS-Nachbau lebendig wirkt.
"""

from __future__ import annotations

import math
import random
import threading
import time
from dataclasses import dataclass, field

__all__ = ["VehicleState", "DriveProfile", "PHASES"]


@dataclass
class VehicleState:
    """Zustand des simulierten Fahrzeugs."""

    # Motor
    rpm: float = 800.0
    coolant_temp: float = 20.0
    intake_temp: float = 22.0
    ambient_temp: float = 18.0
    oil_temp: float = 20.0
    fuel_temp: float = 18.0
    boost_mbar: float = 1000.0
    boost_target_mbar: float = 1000.0
    maf_mgh: float = 250.0
    egr_percent: float = 60.0
    injection_mg: float = 4.5
    injection_deg: float = 2.0
    rail_pressure_bar: float = 300.0
    lambda_value: float = 1.0
    throttle_percent: float = 0.0
    engine_running: bool = True

    # Antrieb / Fahrwerk
    speed_kmh: float = 0.0
    gear: int = 1
    steering_angle: float = 0.0
    yaw_rate: float = 0.0
    lateral_g: float = 0.0
    brake_pressure_bar: float = 0.0
    wheel_speed: dict = field(default_factory=lambda: {0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0})
    brake_switch: bool = False
    handbrake: bool = False

    # Bordnetz / Komfort
    battery_v: float = 13.95
    fuel_level_l: float = 38.0
    odo_km: int = 178450
    trip_km: float = 123.4
    outside_temp: float = 14.5
    interior_temp: float = 21.0
    doors: dict = field(default_factory=lambda: {0: False, 1: False, 2: False, 3: False})
    keys_stored: int = 2
    dtc_set: str = ""

    # Zuendung / Klemmen
    terminal_15: bool = True
    terminal_30: bool = True
    run_seconds: float = 0.0  # Laufzeit seit Simulationsstart (fuer Warmlauf)

    def as_dict(self) -> dict:
        data = {
            key: value
            for key, value in self.__dict__.items()
            if isinstance(value, (int, float, bool, str))
        }
        return data


PHASES: list[tuple[str, float, float, float]] = [
    # (Name, Dauer in s, Ziel-Drehzahl, Ziel-Geschwindigkeit)
    ("Leerlauf", 6.0, 830.0, 0.0),
    ("Anfahren", 5.0, 2200.0, 35.0),
    ("Beschleunigen", 6.0, 3100.0, 95.0),
    ("Konstantfahrt", 8.0, 2050.0, 100.0),
    ("Schubbetrieb", 4.0, 1500.0, 60.0),
    ("Bremsen", 3.0, 900.0, 0.0),
]


class DriveProfile:
    """Treibt einen :class:`VehicleState` in einem eigenen Thread."""

    def __init__(self, state: VehicleState | None = None, *, speed: float = 6.0, seed: int | None = None) -> None:
        self.state = state or VehicleState()
        self.speed = max(speed, 0.1)  # Zeitskalierung (6.0 = sechsfach beschleunigt)
        self.random = random.Random(seed if seed is not None else 4711)
        self._thread: threading.Thread | None = None
        self._running = False
        self._phase_index = 0
        self._phase_time = 0.0
        self._target_rpm = 830.0
        self._target_speed = 0.0
        self._start_rpm = self.state.rpm
        self._start_speed = self.state.speed_kmh
        self.phase_name = PHASES[0][0]
        self.warmup_target = 90.0

    # -- Steuerung ----------------------------------------------------------- #
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="drive-profile", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def set_phase(self, name: str) -> None:
        for index, (phase_name, duration, rpm, speed) in enumerate(PHASES):
            if phase_name.lower() == name.lower():
                self._phase_index = index
                self._phase_time = 0.0
                self._start_rpm = self.state.rpm
                self._start_speed = self.state.speed_kmh
                return

    # -- Simulation ---------------------------------------------------------- #
    def _run(self) -> None:
        last = time.time()
        while self._running:
            now = time.time()
            dt = min(now - last, 0.5) * self.speed
            last = now
            self.tick(dt)
            time.sleep(0.05)

    def tick(self, dt: float) -> None:
        """Ein Simulationsschritt (auch direkt aus Tests nutzbar)."""
        name, duration, target_rpm, target_speed = PHASES[self._phase_index]
        self.phase_name = name
        self._phase_time += dt
        if self._phase_time >= duration:
            self._phase_index = (self._phase_index + 1) % len(PHASES)
            self._phase_time = 0.0
            self._start_rpm = self.state.rpm
            self._start_speed = self.state.speed_kmh
            name, duration, target_rpm, target_speed = PHASES[self._phase_index]
            self.phase_name = name

        progress = min(self._phase_time / duration, 1.0)
        ease = progress * progress * (3 - 2 * progress)  # smoothstep
        state = self.state
        state.rpm = self._start_rpm + (target_rpm - self._start_rpm) * ease
        state.rpm += self.random.uniform(-25, 25)
        state.speed_kmh = max(
            0.0, self._start_speed + (target_speed - self._start_speed) * ease
        )
        state.speed_kmh = min(state.speed_kmh, 220.0)

        # Beschleunigung -> Gierrate/Lenkwinkel (nur zur Anzeige)
        delta_v = (target_speed - self._start_speed)
        state.throttle_percent = max(0.0, min(100.0, 15.0 + delta_v * 0.9 * (1 - progress)))
        state.brake_switch = name == "Bremsen"
        state.brake_pressure_bar = 45.0 if state.brake_switch else 0.0
        state.handbrake = False
        state.steering_angle = math.sin(self._phase_time * 0.7) * 12.0 * min(
            state.speed_kmh / 60.0, 1.0
        )
        state.yaw_rate = state.steering_angle * 0.35
        state.lateral_g = abs(state.yaw_rate) * 0.02
        for wheel in range(4):
            state.wheel_speed[wheel] = state.speed_kmh * (1.0 + 0.002 * wheel)

        # Motor: Temperaturen, Ladedruck, Luftmasse, Einspritzung
        state.run_seconds += dt
        warm = 1.0 - math.exp(-state.run_seconds / 90.0)  # Warmlauf bis ~90 °C
        state.coolant_temp = min(
            self.warmup_target, 20.0 + (self.warmup_target - 20.0) * warm
        )
        state.oil_temp = min(105.0, 20.0 + 85.0 * (1.0 - math.exp(-state.run_seconds / 200.0)))
        state.intake_temp = state.ambient_temp + (state.coolant_temp - state.ambient_temp) * 0.35
        state.fuel_temp = state.ambient_temp + 20.0 * warm
        load = min(state.throttle_percent / 100.0 + state.rpm / 6000.0, 1.0)
        state.boost_target_mbar = 1000.0 + 900.0 * load
        state.boost_mbar += (state.boost_target_mbar - state.boost_mbar) * min(dt * 0.6, 1.0)
        state.boost_mbar += self.random.uniform(-15, 15)
        state.maf_mgh = 230.0 + 700.0 * load + self.random.uniform(-10, 10)
        state.egr_percent = max(0.0, 70.0 - 60.0 * load)
        state.injection_mg = 3.5 + 45.0 * load
        state.injection_deg = 0.5 + 12.0 * load
        state.rail_pressure_bar = 250.0 + 1400.0 * load
        state.lambda_value = 0.98 + 0.06 * load
        state.gear = max(1, min(6, int(state.speed_kmh / 22.0) + 1))

        # Bordnetz
        state.battery_v = 13.9 + self.random.uniform(-0.15, 0.15) - load * 0.1
        state.fuel_level_l = max(6.0, state.fuel_level_l - dt * 0.0006 * state.injection_mg)
        state.trip_km += state.speed_kmh * dt / 3600.0
        state.odo_km += int(state.speed_kmh * dt / 3600.0)
        state.outside_temp += self.random.uniform(-0.05, 0.05)
        state.outside_temp = max(-20.0, min(38.0, state.outside_temp))
        state.interior_temp += (21.0 + math.sin(time.time() / 300.0) * 2 - state.interior_temp) * 0.01
