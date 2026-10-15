"""ISO 15765-2 (ISO-TP) - Segmentierung fuer CAN.

Die Implementierung deckt den in VAG-Fahrzeugen ueblichen Normal-Addressing-
Betrieb ab:

* Einzelframe (SF), Erstframe (FF), Folgeframes (CF), Flow-Control (FC)
* Blockgroesse (BS) und Mindestabstand (STmin) im Flow-Control-Frame
* Warteframes (FC.Wait) und Ueberlauf (FC.Overflow)

Ein :class:`IsoTpEndpoint` kann beide Rollen einnehmen:

* **Tester** - :meth:`IsoTpEndpoint.request` sendet eine Nutzlast und wartet
  blockierend auf die Antwort.
* **Steuergeraet** - ``handler`` wird fuer jede empfangene Nutzlast im
  Worker-Thread aufgerufen; die Antwort erfolgt ueber
  :meth:`IsoTpEndpoint.send_payload`.

Die Klasse ist als :class:`~vagdiag.proto.bus.CanNode` an einen
:class:`~vagdiag.proto.bus.VirtualCanBus` angeschlossen.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

from .bus import CanFrame, CanNode, VirtualCanBus

__all__ = ["IsoTpError", "IsoTpTimeout", "IsoTpEndpoint", "decode_pci", "MAX_PAYLOAD"]

MAX_PAYLOAD = 4095  # 12 Bit Laengenfeld im Erstframe

# PCI-Typen
PCI_SF = 0x0
PCI_FF = 0x1
PCI_CF = 0x2
PCI_FC = 0x3

# Flow-Control-Status
FC_CTS = 0x0  # continue to send
FC_WAIT = 0x1
FC_OVERFLOW = 0x2


class IsoTpError(Exception):
    """Allgemeiner ISO-TP-Fehler."""


class IsoTpTimeout(IsoTpError):
    """Zeitueberschreitung beim Warten auf Frames."""


def decode_pci(data: bytes) -> tuple[int, int, int]:
    """Zerlegt den ersten Frame und liefert ``(pci_type, laenge/sequenz, header_len)``."""
    if not data:
        raise IsoTpError("Leerer CAN-Frame")
    pci = data[0] >> 4
    if pci == PCI_SF:
        return pci, data[0] & 0x0F, 1
    if pci == PCI_FF:
        if len(data) < 2:
            raise IsoTpError("Erstframe zu kurz")
        return pci, ((data[0] & 0x0F) << 8) | data[1], 2
    if pci == PCI_CF:
        return pci, data[0] & 0x0F, 1
    if pci == PCI_FC:
        status = data[0] & 0x0F
        block_size = data[1] if len(data) > 1 else 0
        st_min = data[2] if len(data) > 2 else 0
        return pci, (status << 16) | (block_size << 8) | st_min, 3
    raise IsoTpError(f"Unbekannter PCI-Typ 0x{pci:X}")


def stmin_seconds(st_min: int) -> float:
    """Wandelt den STmin-Rohwert in Sekunden um (ISO 15765-2 Tabelle 3)."""
    if st_min <= 0x7F:
        return st_min / 1000.0
    if 0xF1 <= st_min <= 0xF9:
        return (st_min - 0xF0) / 10000.0
    return 0.0


class IsoTpEndpoint(CanNode):
    """ISO-TP-Endpunkt auf dem virtuellen CAN-Bus."""

    def __init__(
        self,
        name: str,
        bus: VirtualCanBus,
        tx_id: int,
        rx_id: int,
        *,
        handler: Callable[[bytes], bytes | None] | None = None,
        block_size: int = 0,
        st_min_ms: float = 0.0,
        timeout: float = 1.0,
        padding: int | None = None,
        accept_all: bool = False,
    ) -> None:
        super().__init__(name, bus)
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.handler = handler
        self.block_size = block_size
        self.st_min = st_min_ms / 1000.0
        self.timeout = timeout
        self.padding = padding
        self.accept_all = accept_all

        # Empfangsseite
        self._rx_buffer: bytearray | None = None
        self._rx_expected = 0
        self._rx_seq = 0
        self._rx_last_frame = 0.0
        self._responses: "queue.Queue[bytes]" = queue.Queue()
        # Sendeseite
        self._fc_event = threading.Event()
        self._fc_status = FC_CTS
        self._fc_block_size = 0
        self._fc_st_min = 0.0
        self._send_lock = threading.Lock()
        self.stats = {"frames_tx": 0, "frames_rx": 0, "timeouts": 0, "n_cr": 0}
        # Steuergeraeteseite: Auftraege laufen in einem eigenen Thread, damit
        # das Senden einer mehrteiligen Antwort nicht den Empfang blockiert
        # (der Tester schickt waehrenddessen seine Flow-Control-Rahmen).
        self._requests: "queue.Queue[bytes | None]" = queue.Queue()
        self._reply_thread: threading.Thread | None = None

    # -- Busanbindung -------------------------------------------------------- #
    def accept_ids(self):
        if self.accept_all:
            return None
        return {self.rx_id}

    # -- Senden -------------------------------------------------------------- #
    def send_payload(self, payload: bytes, timeout: float | None = None) -> None:
        """Zerlegt die Nutzlast und sendet sie (blockiert nur bei Erstframes)."""
        timeout = self.timeout if timeout is None else timeout
        payload = bytes(payload)
        if len(payload) > MAX_PAYLOAD:
            raise IsoTpError(f"Nutzlast zu gross ({len(payload)} Bytes)")
        with self._send_lock:
            if len(payload) <= 7:
                data = bytes([(PCI_SF << 4) | len(payload)]) + payload
                self._tx_frame(data)
                return
            # Erstframe + Flow-Control abwarten
            self._fc_event.clear()
            frame = bytes([(PCI_FF << 4) | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]
            self._tx_frame(frame)
            if not self._fc_event.wait(timeout):
                self.stats["timeouts"] += 1
                raise IsoTpTimeout("Kein Flow-Control-Frame empfangen")
            if self._fc_status == FC_OVERFLOW:
                raise IsoTpError("Empfaenger meldet Overflow")
            while self._fc_status == FC_WAIT:
                self._fc_event.clear()
                if not self._fc_event.wait(timeout):
                    self.stats["timeouts"] += 1
                    raise IsoTpTimeout("Empfaenger sendet nur Warteframes")
            block = self._fc_block_size or self.block_size
            st_min = self._fc_st_min or self.st_min
            seq = 1
            sent_in_block = 0
            offset = 6
            while offset < len(payload):
                chunk = payload[offset : offset + 7]
                self._tx_frame(bytes([(PCI_CF << 4) | (seq & 0x0F)]) + chunk)
                offset += 7
                seq = (seq + 1) & 0x0F
                sent_in_block += 1
                if st_min:
                    time.sleep(st_min)
                if block and sent_in_block >= block and offset < len(payload):
                    self._fc_event.clear()
                    if not self._fc_event.wait(timeout):
                        self.stats["timeouts"] += 1
                        raise IsoTpTimeout("Kein Flow-Control-Frame fuer naechsten Block")
                    if self._fc_status == FC_OVERFLOW:
                        raise IsoTpError("Empfaenger meldet Overflow")
                    sent_in_block = 0

    def _tx_frame(self, data: bytes) -> None:
        if self.padding is not None and len(data) < self.padding:
            data = data + b"\x00" * (self.padding - len(data))
        self.stats["frames_tx"] += 1
        self.send_can(self.tx_id, data)

    # -- Empfangen ----------------------------------------------------------- #
    def on_frame(self, frame: CanFrame) -> None:
        self.stats["frames_rx"] += 1
        try:
            pci, value, header = decode_pci(frame.data)
        except IsoTpError as exc:
            self.bus.trace_event(self.name, f"ISO-TP: {exc}")
            return

        if pci == PCI_SF:
            payload = frame.data[header : header + value]
            self._handle_payload(payload)
        elif pci == PCI_FF:
            self._rx_buffer = bytearray(frame.data[header:])
            self._rx_expected = value
            self._rx_seq = 1
            self._rx_last_frame = time.time()
            # Flow-Control zuruecksenden
            self._tx_frame(
                bytes(
                    [
                        (PCI_FC << 4) | FC_CTS,
                        self.block_size & 0xFF,
                        int(min(self.st_min * 1000, 127)),
                    ]
                )
            )
        elif pci == PCI_CF:
            if self._rx_buffer is None:
                self.bus.trace_event(self.name, "ISO-TP: Folgeframes ohne Erstframe")
                return
            if value != self._rx_seq:
                self.stats["n_cr"] += 1
                self.bus.trace_event(
                    self.name,
                    f"ISO-TP: Sequenzfehler (erwartet {self._rx_seq}, erhalten {value})",
                )
                self._rx_buffer = None
                return
            self._rx_seq = (self._rx_seq + 1) & 0x0F
            self._rx_buffer.extend(frame.data[header:])
            if len(self._rx_buffer) >= self._rx_expected:
                payload = bytes(self._rx_buffer[: self._rx_expected])
                self._rx_buffer = None
                self._handle_payload(payload)
        elif pci == PCI_FC:
            status = (value >> 16) & 0xFF
            self._fc_status = status
            self._fc_block_size = (value >> 8) & 0xFF
            self._fc_st_min = stmin_seconds(value & 0xFF)
            self._fc_event.set()

    # -- Lebenszyklus (Steuergeraeteseite) ------------------------------------ #
    def start(self) -> None:
        super().start()
        if self.handler is not None and self._reply_thread is None:
            self._reply_thread = threading.Thread(
                target=self._reply_loop, name=f"isotp-tx-{self.name}", daemon=True
            )
            self._reply_thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        if self._reply_thread is not None:
            self._requests.put(None)
            self._reply_thread.join(timeout=timeout)
            self._reply_thread = None
        super().stop(timeout)

    def _reply_loop(self) -> None:
        while self._running or not self._requests.empty():
            try:
                payload = self._requests.get(timeout=0.1)
            except queue.Empty:
                continue
            if payload is None:
                break
            reply = self.handler(payload) if self.handler else None
            if reply:
                try:
                    self.send_payload(reply)
                except IsoTpError as exc:  # pragma: no cover - Simulation
                    self.bus.trace_event(self.name, f"ISO-TP: Antwort nicht gesendet ({exc})")

    def _handle_payload(self, payload: bytes) -> None:
        if self.handler is not None:
            self._requests.put(bytes(payload))
        else:
            self._responses.put(payload)

    # -- Tester-API ---------------------------------------------------------- #
    def request(self, payload: bytes, timeout: float | None = None) -> bytes:
        """Sendet eine Nutzlast und wartet auf die Antwort des Steuergeraets."""
        timeout = self.timeout if timeout is None else timeout
        while True:  # alte Antworten verwerfen
            try:
                self._responses.get_nowait()
            except queue.Empty:
                break
        self.send_payload(payload, timeout=timeout)
        try:
            return self._responses.get(timeout=timeout)
        except queue.Empty as exc:
            self.stats["timeouts"] += 1
            raise IsoTpTimeout(
                f"Keine Antwort auf {payload.hex(' ')} innerhalb {timeout:.2f}s"
            ) from exc
