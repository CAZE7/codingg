"""OBD-II-Sitzung - der "OBD-II"-Knopf der VCDS-Oberflaeche.

Die generische Abgasdiagnose laeuft ausserhalb der VAG-Diagnose:

* **CAN-Fahrzeuge** sprechen auf ``0x7E0``/``0x7E8`` per ISO-TP
  (ISO 15765-4). Antwortet dort niemand, wird der VW-TP-Kanal zum
  Motorsteuergeraet benutzt (aeltere CAN-Fahrzeuge).
* **K-Line-Fahrzeuge** werden ueber ISO 14230 angesprochen (5-Baud-
  Adressierung, danach KWP2000-Rahmen) - so macht es auch VCDS.

Die Dienste selbst sind SAE J1979 (Modi 01 bis 0A); die Auswertung der
PID-Daten steht in :mod:`vagdiag.obd.obd2`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import address_wire
from ..proto.iso14230 import Kwp2000KLineTester
from ..proto.isotp import IsoTpEndpoint
from ..proto.vwtp import VwtpChannel
from . import obd2
from .dtc import decode_sae16

if TYPE_CHECKING:  # pragma: no cover - nur fuer Typangaben
    from ..client import VehicleSession

__all__ = ["ObdError", "ObdSession", "OBD_PID_AUSWAHL"]

#: PIDs, die VCDS in der Live-Ansicht standardmaessig zeigt
OBD_PID_AUSWAHL = (0x04, 0x05, 0x0B, 0x0C, 0x0D, 0x0F, 0x10, 0x11, 0x1F, 0x2F, 0x42, 0x5C)


class ObdError(Exception):
    """Fehler in der OBD-II-Sitzung (kein Transport, Dienst abgelehnt)."""


@dataclass
class ObdEntry:
    """Ein Wert der Live-Ansicht."""

    pid: int
    name: str
    unit: str
    text: str
    data: bytes

    def as_dict(self) -> dict:
        return {
            "pid": f"0x{self.pid:02X}",
            "pid_int": self.pid,
            "name": self.name,
            "unit": self.unit,
            "value": self.text,
            "data": list(self.data),
        }


class ObdSession:
    """OBD-II-Dialog zu einem Fahrzeug (Motorsteuergeraet, Adresse 01)."""

    def __init__(self, session: "VehicleSession", *, address: int = 1, timeout: float | None = None) -> None:
        self.session = session
        self.address = address
        self.timeout = timeout if timeout is not None else session.timeout
        self.transport_name = ""
        self._isotp: IsoTpEndpoint | None = None
        self._kline: Kwp2000KLineTester | None = None
        self._vwtp: VwtpChannel | None = None
        self._open = False

    # ------------------------------------------------------------------ #
    # Verbindung
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "ObdSession":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def open(self) -> "ObdSession":
        if self._open:
            return self
        errors: list[str] = []
        vehicle = self.session.vehicle
        if getattr(vehicle, "can_bus", None) is not None:
            try:
                self._open_isotp()
                self._open = True
                return self
            except Exception as exc:  # noqa: BLE001 - Rueckfallebene VW-TP
                errors.append(f"ISO-TP: {exc}")
                self._close_isotp()
            try:
                self._open_vwtp()
                self._open = True
                return self
            except Exception as exc:  # noqa: BLE001 - Rueckfallebene K-Line
                errors.append(f"VW-TP: {exc}")
                self._close_vwtp()
        if getattr(vehicle, "kline_bus", None) is not None:
            try:
                self._open_kline()
                self._open = True
                return self
            except Exception as exc:  # noqa: BLE001 - kein Transport moeglich
                errors.append(f"K-Line: {exc}")
                self._close_kline()
        raise ObdError("Keine OBD-II-Verbindung (" + "; ".join(errors) + ")")

    def _open_isotp(self) -> None:
        endpoint = IsoTpEndpoint(
            "VAGDiag-OBD",
            self.session.vehicle.can_bus,
            tx_id=0x7E0,
            rx_id=0x7E8,
            timeout=self.timeout,
        )
        endpoint.start()
        self._isotp = endpoint
        try:
            self.request(bytes([0x01, 0x00]))  # unterstuetzte PIDs abfragen
        except Exception:
            raise
        self.transport_name = "ISO-TP (ISO 15765-4, CAN 0x7E0/0x7E8)"

    def _open_vwtp(self) -> None:
        channel = VwtpChannel(
            "VAGDiag-OBD",
            self.session.vehicle.can_bus,
            address_wire(self.address),
            proto=1,
            timeout=0.6,
        )
        channel.start()
        channel.open()
        self._vwtp = channel
        self.request(bytes([0x01, 0x00]))
        self.transport_name = "VW TP 2.0 (KWP2000, OBD-Dienste)"

    def _open_kline(self) -> None:
        tester = Kwp2000KLineTester("VAGDiag-OBD", self.session.vehicle.kline_bus)
        tester.connect(address_wire(self.address), timeout=self.timeout)
        self._kline = tester
        self.request(bytes([0x01, 0x00]))
        self.transport_name = "K-Line (ISO 14230 / KWP2000)"

    def close(self) -> None:
        self._close_isotp()
        self._close_vwtp()
        self._close_kline()
        self._open = False

    def _close_isotp(self) -> None:
        if self._isotp is not None:
            try:
                self._isotp.stop(0.2)
            except Exception:  # noqa: BLE001
                pass
            self._isotp = None

    def _close_vwtp(self) -> None:
        if self._vwtp is not None:
            try:
                self._vwtp.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._vwtp.stop(0.2)
            except Exception:  # noqa: BLE001
                pass
            self._vwtp = None

    def _close_kline(self) -> None:
        if self._kline is not None:
            try:
                self.session.vehicle.kline_bus.detach(self._kline)
            except Exception:  # noqa: BLE001
                pass
            self._kline = None

    # ------------------------------------------------------------------ #
    # Dienstebene
    # ------------------------------------------------------------------ #
    def request(self, payload: bytes, *, timeout: float | None = None) -> bytes:
        """Sendet einen OBD-Dienst und liefert die Antwortdaten."""
        timeout = self.timeout if timeout is None else timeout
        if self._isotp is not None:
            answer = self._isotp.request(bytes(payload), timeout)
        elif self._vwtp is not None:
            answer = self._vwtp.request(bytes(payload), timeout=timeout)
        elif self._kline is not None:
            answer = self._kline.request(bytes(payload), timeout=timeout)
        else:
            raise ObdError("Kein OBD-Transport aktiv")
        if not answer:
            raise ObdError("Leere Antwort des Steuergeraets")
        if answer[0] == 0x7F:
            reason = answer[2] if len(answer) > 2 else 0x10
            raise ObdError(
                f"Dienst {payload[0]:02X} abgelehnt (Grund 0x{reason:02X})"
            )
        return answer

    # ------------------------------------------------------------------ #
    # Dienste
    # ------------------------------------------------------------------ #
    def supported_pids(self) -> list[int]:
        """Alle PIDs, die das Steuergeraet auf Modus 01 beantwortet."""
        result: list[int] = []
        base = 0x00
        while True:
            answer = self.request(bytes([0x01, base]))
            if len(answer) < 6:
                break
            mask = answer[2:6]
            for index in range(32):
                if mask[index // 8] & (0x80 >> (index % 8)):
                    result.append(base + index + 1)
            if any(mask[index // 8] & (0x80 >> (index % 8)) for index in (31,)):
                base += 0x20
                if base > 0x60:
                    break
                continue
            break
        return result

    def live(self, pids: list[int] | None = None) -> list[ObdEntry]:
        """Live-Werte (Modus 01) fuer die angegebenen PIDs."""
        entries: list[ObdEntry] = []
        for pid in pids if pids is not None else OBD_PID_AUSWAHL:
            try:
                answer = self.request(bytes([0x01, pid]))
            except ObdError:
                continue
            if len(answer) < 3:
                continue
            data = answer[2:]
            info = obd2.pid_info(pid)
            entries.append(ObdEntry(pid=pid, name=info.name, unit=info.unit, text=info.text(data), data=data))
        return entries

    def readiness(self) -> obd2.Readiness:
        """Readiness-Status (Modus 01, PID 01)."""
        answer = self.request(bytes([0x01, 0x01]))
        return obd2.decode_readiness(answer[2:])

    def codes(self, mode: int = 0x03) -> list[dict]:
        """Fehlercodes aus Modus 03 (gespeichert), 07 (schwebend), 0A (permanent)."""
        answer = self.request(bytes([mode]))
        codes: list[str] = []
        for offset in range(2, len(answer) - 1, 2):
            codes.append(decode_sae16((answer[offset] << 8) | answer[offset + 1]))
        entries: list[dict] = []
        for code in codes:
            found = self.session.db.dtc_lookup(code) or {}
            entries.append(
                {
                    "code": code,
                    "text": found.get("text", ""),
                    "system": found.get("system", ""),
                    "source": obd2.OBD_MODES.get(mode, f"Modus {mode:02X}"),
                }
            )
        return entries

    def clear_codes(self) -> bool:
        """Fehlerspeicher loeschen (Modus 04)."""
        return self.request(bytes([0x04]))[0] == 0x44

    def freeze_frame(self, pids: list[int] | None = None) -> list[ObdEntry]:
        """Momentaufnahme zum ersten Fehler (Modus 02)."""
        entries: list[ObdEntry] = []
        for pid in pids if pids is not None else (0x02, 0x04, 0x05, 0x0B, 0x0C, 0x0D):
            try:
                answer = self.request(bytes([0x02, pid]))
            except ObdError:
                continue
            if len(answer) < 3:
                continue
            data = answer[2:]
            if pid == 0x02:
                info = obd2.PidInfo(0x02, "Fehler, der den Freeze Frame ausloeste", "", None, "System")
                entries.append(ObdEntry(pid=pid, name=info.name, unit="", text=decode_sae16((data[0] << 8) | data[1]), data=data))
                continue
            info = obd2.pid_info(pid)
            entries.append(ObdEntry(pid=pid, name=info.name, unit=info.unit, text=info.text(data), data=data))
        return entries

    def monitors(self) -> list[dict]:
        """Monitortestergebnisse (Modus 06)."""
        answer = self.request(bytes([0x06]))
        body = answer[1:]
        entries: list[dict] = []
        for offset in range(0, len(body) - 9, 10):
            mid = body[offset]
            tid = body[offset + 1]
            value = (body[offset + 4] << 8) | body[offset + 5]
            low = (body[offset + 6] << 8) | body[offset + 7]
            high = (body[offset + 8] << 8) | body[offset + 9]
            entries.append(
                {
                    "mid": f"0x{mid:02X}",
                    "tid": f"0x{tid:02X}",
                    "name": MONITOR_NAMES.get(mid, f"MID 0x{mid:02X}"),
                    "value": value,
                    "min": low,
                    "max": high,
                    "ok": low <= value <= high,
                }
            )
        return entries

    def vehicle_info(self) -> dict:
        """Fahrzeuginformationen (Modus 09): VIN, Softwarestand, CVN, SG-Name."""
        result: dict[str, object] = {"vin": "", "calibration_id": "", "cvn": "", "ecu_name": ""}
        for pid, key in ((0x02, "vin"), (0x04, "calibration_id"), (0x06, "cvn"), (0x0A, "ecu_name")):
            try:
                answer = self.request(bytes([0x09, pid]))
            except ObdError:
                continue
            if len(answer) < 3:
                continue
            data = answer[2:]
            if pid == 0x06:
                result[key] = data[1:5].hex(" ").upper()
            else:
                payload = data
                if pid in (0x02, 0x04, 0x0A) and payload:
                    # Erste Stelle ist die Anzahl der Datensaetze (SAE J1979);
                    # bei VIN, Softwarestand und SG-Name steht dort immer 0x01.
                    payload = payload[1:]
                result[key] = payload.decode("latin-1", errors="replace").strip("\x00 ")
        return result

    def vag_only_codes(self) -> list[dict]:
        """Fehlercodes des Motorsteuergeraets ohne SAE-Zuordnung.

        Sie stehen nur in der VAG-Diagnose (Adresse 01); die OBD-Sicht zeigt
        sie nicht, weil keine belegte SAE-Schreibweise vorliegt.
        """
        entries: list[dict] = []
        kline = self._kline
        if kline is not None:
            # Auf der K-Line teilen sich VAG- und OBD-Dialog eine Leitung: der
            # OBD-Tester wird fuer die Dauer des VAG-Dialogs abgehaengt, sonst
            # quittieren sich beide Dialoge gegenseitig die Bytes.
            self._kline = None
            try:
                self.session.vehicle.kline_bus.detach(kline)
            except Exception:  # noqa: BLE001 - Bus kann bereits getrennt sein
                pass
        try:
            with self.session.open_module(self.address) as module:
                for fault in module.read_faults():
                    code = fault.display_code
                    if fault.sae_code:
                        continue
                    entries.append(
                        {
                            "code": code,
                            "text": fault.text,
                            "status": fault.status_str,
                        }
                    )
        except Exception:  # noqa: BLE001 - Zusatzinformation, nie kritisch
            return entries
        finally:
            if kline is not None:
                self._kline = kline
                try:
                    self.session.vehicle.kline_bus.attach(kline)
                    # Nach dem VAG-Dialog wieder per 5-Baud-Adressierung anmelden
                    kline.connect(address_wire(self.address), timeout=self.timeout)
                except Exception as exc:  # noqa: BLE001 - OBD-Sitzung beenden
                    self._kline = None
                    try:
                        self.session.vehicle.kline_bus.detach(kline)
                    except Exception:  # noqa: BLE001
                        pass
                    raise ObdError(f"OBD-Verbindung nach dem VAG-Dialog verloren: {exc}")
        return entries

    def as_dict(self) -> dict:
        """Kurzfassung fuer JSON-Ausgaben."""
        readiness = self.readiness()
        return {
            "transport": self.transport_name,
            "supported_pids": [f"0x{pid:02X}" for pid in self.supported_pids()],
            "readiness": readiness.as_dict(),
            "codes": self.codes(0x03),
            "pending": self.codes(0x07),
            "permanent": self.codes(0x0A),
            "vehicle_info": self.vehicle_info(),
        }


#: Namen der Monitorkennungen (Modus 06) - oeffentliche Zuordnung
MONITOR_NAMES = {
    0x01: "Lambdasonde / Abgassensor",
    0x02: "Lambdasondenheizung",
    0x05: "Lambdasonde (Spannung)",
    0x06: "Lambdasondenheizung (Strom)",
    0x21: "Katalysator",
    0x22: "Katalysator (Bank 1)",
    0x31: "Abgasrueckfuehrung (AGR)",
    0x32: "AGR (Bank 1)",
    0x35: "VVT / Nockenwellenverstellung",
    0x39: "Partikelfilter (DPF)",
    0x3D: "Ladedruckregelung",
    0xA1: "Fehlzuendung (Zylinder)",
    0xA2: "Fehlzuendung (allgemein)",
}
