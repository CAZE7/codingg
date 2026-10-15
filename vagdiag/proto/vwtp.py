"""VW Transport Protokoll 2.0 (VW TP 2.0).

VW TP 2.0 ist das Transportprotokoll, mit dem VAG-Steuergeraete ab etwa Modell-
jahr 2000 ueber CAN angesprochen werden (KWP2000-on-CAN, spaeter UDS-on-CAN).
Es ersetzt ISO-TP und arbeitet mit 7 Byte Nutzlast je CAN-Frame.

Umgesetzter Funktionsumfang (Quellen siehe ``docs/PROTOKOLLE.md``):

* Kommunikationsaufbau (``0xC0``/``0xD0``) auf der Broadcast-ID ``0x200``
* Kanalaufbau (``0xA0``/``0xA1``) inkl. Zeitparametern und Blockgroesse
* Datenframes: Bit 4 = letzter Frame, Bit 5 = keine Quittung noetig,
  Bits 0-3 = Sequenznummer; Quittungen ueber ``0xB0 | Folge-Sequenz``
* Ping (``0xA3``/``0xA4``) und Trennen (``0xA8``/``0xA9``)
* 2-Byte-Laengenpraefix (Big Endian) fuer ``proto = 1`` (KWP), wie in der
  Referenzimplementierung ``PyVCDS``; bei ``proto = 0`` entfaellt es (UDS)

Adresskonvention der Simulation: CAN-ID = ``0x200 + logische Adresse des
Senders``. Der Tester (Standard ``0xF1``) sendet also auf ``0x2F1``, ein
Steuergeraet mit Adresse ``0x01`` auf ``0x201``. Der Kommunikationsaufbau
richtet sich an ``0x200``; die Antwort kommt auf ``0x200 + SG-Adresse``.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .bus import CanFrame, CanNode, VirtualCanBus

__all__ = [
    "VwtpError",
    "VwtpTimeout",
    "VwtpEndpoint",
    "VwtpChannel",
    "VwtpEcuServer",
    "ChannelParams",
    "encode_time_param",
    "decode_time_param",
]

# Steuer-Opcodes
OP_COMM_SETUP = 0xC0
OP_COMM_SETUP_RESP = 0xD0
OP_COMM_SETUP_NAK = 0xD6
OP_CHANNEL_SETUP = 0xA0
OP_CHANNEL_SETUP_RESP = 0xA1
OP_CHANNEL_SETUP_FAIL = 0xA2
OP_PING = 0xA3
OP_PING_RESP = 0xA4
OP_BREAK = 0xA5
OP_DISCONNECT = 0xA8
OP_DISCONNECT_RESP = 0xA9
OP_ACK_BASE = 0xB0
OP_ACK_NOT_READY = 0x90

FLAG_LAST = 0x10
FLAG_NO_ACK = 0x20

BROADCAST_ID = 0x200
TIME_SCALES_MS = (0.1, 1.0, 10.0, 100.0)


class VwtpError(Exception):
    """Allgemeiner VW-TP-Fehler."""


class VwtpTimeout(VwtpError):
    """Zeitueberschreitung im VW-TP-Verkehr."""


def encode_time_param(seconds: float) -> int:
    """Kodiert eine Zeit als (Skala, Wert)-Byte (Skalen: 0.1/1/10/100 ms)."""
    ms = max(seconds * 1000.0, 0.1)
    for index, scale in enumerate(TIME_SCALES_MS):
        if ms / scale <= 0x3F:
            return (index << 6) | int(round(ms / scale))
    return (3 << 6) | 0x3F


def decode_time_param(value: int) -> float:
    """Dekodiert ein (Skala, Wert)-Byte in Sekunden."""
    scale = TIME_SCALES_MS[(value >> 6) & 0x03]
    return (value & 0x3F) * scale / 1000.0


@dataclass
class ChannelParams:
    """Verhandelte Kanalparameter (Blockgroesse, Quittungszeit, Frameabstand)."""

    block_size: int = 16
    ack_timeout: float = 0.1
    interval: float = 0.001

    def encode(self, opcode: int) -> bytes:
        """6-Byte-Kanalparameterrahmen: ``op, Blockgroesse-1, t_ack(2), t_int(2)``."""
        return bytes(
            [
                opcode,
                (self.block_size - 1) & 0x3F,
                encode_time_param(self.ack_timeout),
                0xFF,
                encode_time_param(self.interval),
                0xFF,
            ]
        )

    @classmethod
    def decode(cls, body: bytes) -> "ChannelParams":
        """Liest die Parameter aus dem Rahmenkoerper (ohne Opcode)."""
        params = cls()
        if len(body) >= 1:
            params.block_size = (body[0] & 0x3F) + 1
        if len(body) >= 2:
            params.ack_timeout = decode_time_param(body[1])
        if len(body) >= 4:
            params.interval = decode_time_param(body[3])
        return params


@dataclass
class _State:
    tx_seq: int = 0
    rx_seq: int = 0
    rx_frames: list[bytes] = field(default_factory=list)


class VwtpEndpoint:
    """Transportzustand einer VW-TP-Verbindung.

    Der Endpunkt arbeitet mit injizierter Sendefunktion ``send_fn(can_id, data)``
    und ist damit unabhaengig davon, ob er auf einem virtuellen oder realen
    CAN-Bus laeuft. Senden blockiert bis zur Quittung; der Empfangspfad
    (:meth:`on_frame`) muss aus einem anderen Thread kommen.
    """

    def __init__(
        self,
        *,
        own_can_id: int,
        dest_can_id: int,
        ecu_addr: int,
        tester_addr: int = 0xF1,
        proto: int = 1,
        timeout: float = 0.3,
        params: ChannelParams | None = None,
        send_fn: Callable[[int, bytes], None] | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.own_can_id = own_can_id
        self.dest_can_id = dest_can_id
        self.ecu_addr = ecu_addr
        self.tester_addr = tester_addr
        self.proto = proto
        self.timeout = timeout
        self.params = params or ChannelParams()
        self.send_fn = send_fn
        self.on_event = on_event

        self._state = _State()
        self._messages: "queue.Queue[bytes]" = queue.Queue()
        self._ack_event = threading.Event()
        self._ack_value: int | None = None
        self._send_lock = threading.Lock()
        self.channel_open = False
        self.comm_setup_done = False
        self.closed = False
        self.stats = {"frames_tx": 0, "frames_rx": 0, "retries": 0, "timeouts": 0}

    # ------------------------------------------------------------------ #
    # Hilfsfunktionen
    # ------------------------------------------------------------------ #
    def _send(self, can_id: int, data: bytes) -> None:
        if self.send_fn is None:
            raise VwtpError("Keine Sendefunktion gesetzt")
        self.stats["frames_tx"] += 1
        self.send_fn(can_id, bytes(data))

    def _log(self, text: str) -> None:
        if self.on_event:
            self.on_event(text)

    def build_payload(self, service_data: bytes) -> bytes:
        """Fuegt - falls konfiguriert - das 2-Byte-Laengenpraefix hinzu."""
        if self.proto == 1:
            return len(service_data).to_bytes(2, "big") + bytes(service_data)
        return bytes(service_data)

    def strip_payload(self, payload: bytes) -> bytes:
        if self.proto == 1 and len(payload) >= 2:
            return payload[2:]
        return payload

    # ------------------------------------------------------------------ #
    # Senden
    # ------------------------------------------------------------------ #
    def send_message(self, payload: bytes, can_id: int | None = None) -> bool:
        """Sendet eine Nachricht frame-weise und wartet auf die Quittungen."""
        can_id = self.dest_can_id if can_id is None else can_id
        frames = [payload[i : i + 7] for i in range(0, len(payload), 7)] or [b""]
        with self._send_lock:
            for attempt in range(4):
                index = 0
                ok = True
                block_size = max(self.params.block_size, 1)
                while index < len(frames):
                    block_start = index
                    while index < len(frames) and (index - block_start) < block_size:
                        last = index == len(frames) - 1
                        ack_needed = last or (index - block_start + 1) == block_size
                        op = (
                            (FLAG_LAST if last else 0)
                            | (0 if ack_needed else FLAG_NO_ACK)
                            | (self._state.tx_seq & 0x0F)
                        )
                        self._ack_event.clear()
                        self._send(can_id, bytes([op]) + frames[index])
                        self._state.tx_seq = (self._state.tx_seq + 1) & 0x0F
                        index += 1
                        if ack_needed:
                            break
                    if not self._ack_event.wait(self.timeout):
                        self.stats["timeouts"] += 1
                        self._log(
                            "VW-TP: keine Quittung - Block wird wiederholt "
                            f"(Versuch {attempt + 1}/4)"
                        )
                        self.stats["retries"] += 1
                        # Sequenz zuruecksetzen: Empfaenger erwartet den Block erneut
                        self._state.tx_seq = block_start & 0x0F
                        ok = False
                        break
                if ok:
                    return True
            return False

    # ------------------------------------------------------------------ #
    # Empfangen
    # ------------------------------------------------------------------ #
    def on_frame(self, data: bytes) -> None:
        """Verarbeitet einen empfangenen VW-TP-Frame (im Empfangsthread)."""
        if not data:
            return
        self.stats["frames_rx"] += 1
        op = data[0]
        body = data[1:]

        if OP_ACK_BASE <= op <= OP_ACK_BASE + 0x0F or OP_ACK_NOT_READY <= op <= OP_ACK_NOT_READY + 0x0F:
            self._ack_value = op & 0x0F
            self._ack_event.set()
            return
        if op == OP_COMM_SETUP:
            self._on_comm_setup(body)
            return
        if op == OP_CHANNEL_SETUP:
            self._on_channel_setup(body)
            return
        if op in (OP_COMM_SETUP_RESP, OP_CHANNEL_SETUP_RESP, OP_COMM_SETUP_NAK, OP_CHANNEL_SETUP_FAIL):
            self._on_setup_response(op, body)
            return
        if op == OP_PING:
            self._send(self.dest_can_id, bytes([OP_PING_RESP]))
            return
        if op in (OP_PING_RESP, OP_BREAK):
            return
        if op in (OP_DISCONNECT, OP_DISCONNECT_RESP):
            self.closed = True
            self.channel_open = False
            self._ack_event.set()
            self._log("VW-TP: Verbindung getrennt")
            if op == OP_DISCONNECT:
                self._send(self.dest_can_id, bytes([OP_DISCONNECT_RESP]))
            return
        if op >= 0xF0:
            self._log(f"VW-TP: unbekannter Opcode 0x{op:02X}")
            return

        # Datenframe
        seq = op & 0x0F
        if seq != self._state.rx_seq:
            self._log(
                f"VW-TP: Sequenzfehler (erwartet {self._state.rx_seq}, erhalten {seq}) "
                "- Quittung wird wiederholt"
            )
            self._send(self.dest_can_id, bytes([OP_ACK_BASE | self._state.rx_seq]))
            return
        self._state.rx_seq = (self._state.rx_seq + 1) & 0x0F
        self._state.rx_frames.append(bytes(body))
        if not (op & FLAG_NO_ACK):
            self._send(self.dest_can_id, bytes([OP_ACK_BASE | self._state.rx_seq]))
        if op & FLAG_LAST:
            payload = b"".join(self._state.rx_frames)
            self._state.rx_frames = []
            self._messages.put(self.strip_payload(payload))

    # -- Steuerrahmen ----------------------------------------------------- #
    def _on_comm_setup(self, body: bytes) -> None:
        """Antwortet auf einen Kommunikationsaufbau (nur Steuergeraeteseite)."""
        response = bytes(
            [
                self.ecu_addr,
                OP_COMM_SETUP_RESP,
                self.dest_can_id & 0xFF,          # Adresse, auf der das SG empfaengt
                (self.dest_can_id >> 8) & 0x0F,   # oberes Nibble 0 = gueltig
                self.own_can_id & 0xFF,           # Adresse, auf der das SG sendet
                (self.own_can_id >> 8) & 0x0F,
                self.proto,
            ]
        )
        # Antwort geht an die eigene CAN-ID (siehe PyVCDS: framebuf[0x200 + dest]).
        self._send(BROADCAST_ID + self.ecu_addr, response)
        self._log("VW-TP: Kommunikationsaufbau quittiert")

    def _on_channel_setup(self, body: bytes) -> None:
        """Antwortet auf einen Kanalaufbau (nur Steuergeraeteseite)."""
        self.params = ChannelParams.decode(body)
        # Ein neuer Kanal beginnt immer bei Sequenz 0.
        self._state = _State()
        self.closed = False
        self._messages = queue.Queue()
        self._send(self.dest_can_id, self.params.encode(OP_CHANNEL_SETUP_RESP))
        self.channel_open = True
        self._log(
            f"VW-TP: Kanalaufbau bestaetigt (Blockgroesse {self.params.block_size}, "
            f"Quittungszeit {self.params.ack_timeout * 1000:.1f} ms)"
        )

    def _on_setup_response(self, op: int, body: bytes) -> None:
        if op == OP_CHANNEL_SETUP_RESP:
            if len(body) >= 1:
                body = body[1:] if body[0] == OP_CHANNEL_SETUP_RESP else body
            self.params = ChannelParams.decode(body)
            self._log(
                f"VW-TP: Kanalparameter empfangen (Blockgroesse {self.params.block_size}, "
                f"Quittungszeit {self.params.ack_timeout * 1000:.1f} ms)"
            )
        if op == OP_COMM_SETUP_RESP:
            self.comm_setup_done = True
            self._log("VW-TP: Kommunikationsaufbau bestaetigt")
        self.channel_open = True
        self._ack_event.set()

    # -- Nachrichten -------------------------------------------------------- #
    def take_message(self, timeout: float | None = None) -> bytes:
        """Holt die naechste vollstaendige Nutzlast (ohne Laengenpraefix)."""
        timeout = self.timeout if timeout is None else timeout
        try:
            return self._messages.get(timeout=timeout)
        except queue.Empty as exc:
            self.stats["timeouts"] += 1
            raise VwtpTimeout("Keine VW-TP-Nachricht empfangen") from exc

    def reset_channel(self) -> None:
        """Setzt den Kanalzustand zurueck (vor einem neuen Kanalaufbau)."""
        self._state = _State()
        self.closed = False
        self.channel_open = False
        self._ack_event.clear()
        self.drain()

    def drain(self) -> int:
        count = 0
        while True:
            try:
                self._messages.get_nowait()
                count += 1
            except queue.Empty:
                return count


# --------------------------------------------------------------------------- #
# Tester-Kanal
# --------------------------------------------------------------------------- #
class VwtpChannel(CanNode):
    """Tester-Kanal: oeffnet und betreibt eine VW-TP-Verbindung zu einem SG."""

    def __init__(
        self,
        name: str,
        bus: VirtualCanBus,
        ecu_addr: int,
        *,
        tester_addr: int = 0xF1,
        proto: int = 1,
        timeout: float = 0.3,
        comm_setup: bool = True,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(name, bus)
        self.endpoint = VwtpEndpoint(
            own_can_id=BROADCAST_ID + tester_addr,
            dest_can_id=BROADCAST_ID + ecu_addr,
            ecu_addr=ecu_addr,
            tester_addr=tester_addr,
            proto=proto,
            timeout=timeout,
            send_fn=self.send_can,
            on_event=on_event,
        )
        self.comm_setup = comm_setup
        self.log: list[str] = []

    # Der Tester empfaengt alle IDs und filtert in der Software
    def accept_ids(self):
        return None

    # -- Aufbau ------------------------------------------------------------- #
    def open(self) -> None:
        """Fuehrt Kommunikationsaufbau und Kanalaufbau durch."""
        endpoint = self.endpoint
        if self.comm_setup:
            frame = bytes(
                [
                    endpoint.ecu_addr,
                    OP_COMM_SETUP,
                    endpoint.own_can_id & 0xFF,
                    (endpoint.own_can_id >> 8) & 0x0F,
                    0x00,
                    0x00,
                    endpoint.proto,
                ]
            )
            for _ in range(4):
                endpoint.comm_setup_done = False
                self.send_can(BROADCAST_ID, frame)
                deadline = time.time() + endpoint.timeout
                while time.time() < deadline and not endpoint.comm_setup_done:
                    time.sleep(0.001)
                if endpoint.comm_setup_done:
                    break
            endpoint.channel_open = False
        # Neuer Kanal: Sequenzen und Nachrichtenpuffer zuruecksetzen
        endpoint.reset_channel()
        params = endpoint.params.encode(OP_CHANNEL_SETUP)
        for _ in range(4):
            endpoint.channel_open = False
            self.send_can(endpoint.dest_can_id, params)
            deadline = time.time() + endpoint.timeout
            while time.time() < deadline:
                if endpoint.channel_open:
                    return
                time.sleep(0.001)
        raise VwtpTimeout(
            f"Kanalaufbau mit Steuergeraet 0x{endpoint.ecu_addr:02X} fehlgeschlagen"
        )

    def close(self) -> None:
        try:
            self.send_can(self.endpoint.dest_can_id, bytes([OP_DISCONNECT]))
        except Exception:  # pragma: no cover
            pass
        self.endpoint.closed = True
        self.endpoint.channel_open = False

    # -- Nutzdaten ---------------------------------------------------------- #
    def request(self, service_data: bytes, timeout: float | None = None) -> bytes:
        """Sendet einen Diagnoseauftrag und liefert die Antwortnutzlast."""
        endpoint = self.endpoint
        timeout = endpoint.timeout if timeout is None else timeout
        endpoint.drain()
        if not endpoint.send_message(endpoint.build_payload(service_data)):
            raise VwtpTimeout("VW-TP: Nachricht konnte nicht uebertragen werden")
        return endpoint.take_message(timeout=timeout)

    # -- Frameverarbeitung --------------------------------------------------- #
    def on_frame(self, frame: CanFrame) -> None:
        data = frame.data
        # Aufbauantworten sind adressiert (byte0 = SG-Adresse, byte1 = 0xD0/0xD6)
        if len(data) >= 2 and data[1] in (OP_COMM_SETUP_RESP, OP_COMM_SETUP_NAK):
            data = data[1:]
        self.endpoint.on_frame(data)


# --------------------------------------------------------------------------- #
# Steuergeraeteseite
# --------------------------------------------------------------------------- #
class VwtpEcuServer:
    """Steuergeraeteseite des VW-TP.

    Der Server laeuft in einem eigenen Thread: ``send_message`` wartet auf
    Quittungen und darf deshalb nicht im Empfangspfad des CAN-Knotens liegen.
    ``handler`` erhaelt die Nutzlast ohne Laengenpraefix und liefert die
    Antwortnutzlast (oder ``None`` bei keiner Antwort).
    """

    def __init__(
        self,
        *,
        ecu_addr: int,
        handler: Callable[[bytes], bytes | None],
        tester_addr: int = 0xF1,
        proto: int = 1,
        timeout: float = 0.3,
        send_fn: Callable[[int, bytes], None] | None = None,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self.handler = handler
        self.endpoint = VwtpEndpoint(
            own_can_id=BROADCAST_ID + ecu_addr,
            dest_can_id=BROADCAST_ID + tester_addr,
            ecu_addr=ecu_addr,
            tester_addr=tester_addr,
            proto=proto,
            timeout=timeout,
            send_fn=send_fn,
            on_event=on_event,
        )
        self._inbox: "queue.Queue[bytes | None]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._running = False

    # -- Lebenszyklus -------------------------------------------------------- #
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Empfangs- und Sendepfad laufen getrennt: der Sendepfad wartet auf
        # Quittungen des Testers und darf den Empfang nicht blockieren.
        self._threads = [
            threading.Thread(
                target=self._run, name=f"vwtp-ecu-rx-{self.endpoint.ecu_addr:02X}", daemon=True
            ),
            threading.Thread(
                target=self._send_loop,
                name=f"vwtp-ecu-tx-{self.endpoint.ecu_addr:02X}",
                daemon=True,
            ),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._running = False
        self._inbox.put(None)
        try:
            self.endpoint._messages.put_nowait(None)  # noqa: SLF001 - Wecksignal
        except queue.Full:  # pragma: no cover
            pass
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads = []

    # -- Anbindung an den CAN-Knoten ---------------------------------------- #
    def accepts(self, can_id: int) -> bool:
        # Nur Broadcast (0x200) und die eigene ID (0x200 + SG-Adresse). Die ID,
        # auf der das SG sendet (0x200 + Testeradresse), gehoert dem Tester und
        # darf nicht mitgehoert werden - sonst interpretieren andere
        # Steuergeraete fremde Antworten als eigene Auftraege.
        return can_id in (BROADCAST_ID, self.endpoint.own_can_id)

    def on_frame(self, can_id: int, data: bytes) -> None:
        # Der Kommunikationsaufbau ist adressiert (byte0 = Zieladresse,
        # byte1 = 0xC0); alle anderen Rahmen beginnen mit dem Opcode.
        if len(data) >= 2 and data[1] == OP_COMM_SETUP:
            if data[0] not in (0x00, self.endpoint.ecu_addr):
                return  # Aufbau gilt einem anderen Steuergeraet
            data = data[1:]
        self._inbox.put(bytes(data))

    def _run(self) -> None:
        """Empfangspfad: verarbeitet Rahmen und erzeugt Quittungen sofort."""
        while self._running:
            data = self._inbox.get()
            if data is None:
                break
            self.endpoint.on_frame(data)

    def _send_loop(self) -> None:
        """Sendepfad: ruft den Handler auf und uebertraegt die Antwort."""
        while self._running:
            try:
                payload = self.endpoint._messages.get(timeout=0.1)  # noqa: SLF001
            except queue.Empty:
                continue
            if payload is None:
                break
            response = self.handler(payload)
            if response is None:
                continue
            self.endpoint.send_message(self.endpoint.build_payload(response))
