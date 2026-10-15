"""Virtuelle Fahrzeugbusse.

Dieses Modul stellt die unterste Ebene des VCDS-Nachbaus bereit:

``VirtualCanBus``
    Frame-orientierter CAN-Bus. Teilnehmer ("Nodes") melden sich an und
    erhalten alle Frames, deren CAN-ID sie akzeptieren. Die Zustellung laeuft
    ueber eine Warteschlange und einen Worker-Thread je Teilnehmer, damit sich
    Antworten und Parallelverkehr nicht blockieren.

``KLineBus``
    Ein-Draht-K-Line (KWP1281). Auf der K-Line quittiert der *Empfaenger*
    jedes Byte mit dessen Komplement (Einerkomplement), genau so, wie es
    ``kw1281test`` (siehe ``KwpCommon.ReadComplement`` / ``ReadAndAckByte``)
    und die VAG-Spezifikation vorsehen. Das erledigt der Bus fuer beide Seiten;
    Echo-Bytes werden als solche markiert und nicht erneut beantwortet.

``TraceRecorder``
    Ringpuffer mit Abonnenten. Die Weboberflaeche nutzt ihn fuer die
    Live-Protokollanzeige ("Busmonitor").
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

__all__ = [
    "CanFrame",
    "TraceEntry",
    "TraceRecorder",
    "SimClock",
    "VirtualCanBus",
    "CanNode",
    "KLineBus",
    "KLineNode",
    "KLineEcu",
]


# --------------------------------------------------------------------------- #
# Taktrate der Simulation
# --------------------------------------------------------------------------- #
class SimClock:
    """Skaliert alle simulierten Wartezeiten.

    ``speed=1.0`` entspricht ungefaehr realen Buszeiten (K-Line ~ 1 ms/Byte,
    R6-Pause 2 ms). ``speed=0.0`` fuehrt zu einer vollstaendig verzoegerungs-
    freien Simulation - praktisch fuer Tests. Werte groesser 1 *verlangsamen*
    die Simulation und eignen sich zum Vorfuehren langsamer Busse.
    """

    def __init__(self, speed: float = 1.0) -> None:
        self.speed = float(speed)

    def sleep(self, seconds: float) -> None:
        if self.speed <= 0 or seconds <= 0:
            return
        time.sleep(min(seconds * self.speed, 1.0))

    # Bequeme Kuerzel fuer die im KWP1281 verwendeten Zeiten
    def byte_gap(self) -> None:
        self.sleep(0.001)

    def r6(self) -> None:
        self.sleep(0.002)


# --------------------------------------------------------------------------- #
# Trace
# --------------------------------------------------------------------------- #
@dataclass
class TraceEntry:
    """Ein Eintrag im Busmonitor."""

    ts: float
    bus: str
    source: str
    kind: str  # "can" | "kline" | "event"
    data: bytes = b""
    arbitration_id: int | None = None
    text: str = ""
    direction: str = "rx"

    @property
    def data_hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)

    def as_dict(self) -> dict:
        return {
            "ts": round(self.ts, 4),
            "time": time.strftime("%H:%M:%S", time.localtime(self.ts))
            + f".{int((self.ts % 1) * 1000):03d}",
            "bus": self.bus,
            "source": self.source,
            "kind": self.kind,
            "data": self.data_hex,
            "id": None if self.arbitration_id is None else f"0x{self.arbitration_id:03X}",
            "text": self.text,
            "dir": self.direction,
        }


class TraceRecorder:
    """Ringpuffer fuer Busmitschnitte inkl. Abonnenten fuer Live-Updates."""

    def __init__(self, maxlen: int = 4000) -> None:
        self._entries: deque[TraceEntry] = deque(maxlen=maxlen)
        self._subscribers: list[Callable[[TraceEntry], None]] = []
        self._lock = threading.RLock()
        self.enabled = True

    def record(self, entry: TraceEntry) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._entries.append(entry)
            subs = list(self._subscribers)
        for cb in subs:
            try:
                cb(entry)
            except Exception:  # Abonnenten duerfen den Bus nie stoeren
                pass

    def subscribe(self, cb: Callable[[TraceEntry], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(cb)

        def unsubscribe() -> None:
            with self._lock:
                if cb in self._subscribers:
                    self._subscribers.remove(cb)

        return unsubscribe

    def event(self, source: str, text: str, bus: str = "") -> TraceEntry:
        """Protokolliert ein Ereignis (z. B. Dialogschritt) im Trace."""
        entry = TraceEntry(ts=time.time(), bus=bus, source=source, kind="event", text=text)
        self.record(entry)
        return entry

    def tail(self, count: int = 200) -> list[TraceEntry]:
        with self._lock:
            return list(self._entries)[-count:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# --------------------------------------------------------------------------- #
# CAN
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CanFrame:
    arbitration_id: int
    data: bytes
    extended: bool = False
    source: str = ""
    ts: float = field(default_factory=time.time)

    def hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)

    def __str__(self) -> str:  # pragma: no cover - Komfort
        return f"{self.arbitration_id:03X}#{self.hex()}"


class CanNode(ABC):
    """Teilnehmer am virtuellen CAN-Bus."""

    kind = "can"

    def __init__(self, name: str, bus: "VirtualCanBus") -> None:
        self.name = name
        self.bus = bus
        self._inbox: "queue.Queue[CanFrame]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._running = False
        bus.attach(self)

    # -- Lebenszyklus ------------------------------------------------------- #
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name=f"can-{self.name}", daemon=True
        )
        self._thread.start()
        self.on_start()

    def stop(self, timeout: float = 1.0) -> None:
        self._running = False
        self._inbox.put(CanFrame(0, b"", source="__stop__"))
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self.on_stop()
        self.bus.detach(self)

    def _run(self) -> None:
        while self._running:
            frame = self._inbox.get()
            if frame.source == "__stop__":
                break
            try:
                self.on_frame(frame)
            except Exception as exc:  # pragma: no cover - Diagnose der Simulation
                self.bus.trace_event(self.name, f"Fehler bei Frame {frame}: {exc}")

    # -- API ---------------------------------------------------------------- #
    def accept_ids(self) -> Sequence[int] | None:
        """Welche CAN-IDs dieser Teilnehmer empfaengt (``None`` = alle)."""
        return None

    def accepts(self, arbitration_id: int) -> bool:
        ids = self.accept_ids()
        return True if ids is None else arbitration_id in ids

    def _deliver(self, frame: CanFrame) -> None:
        self._inbox.put(frame)

    def send_can(self, arbitration_id: int, data: bytes, extended: bool = False) -> None:
        self.bus.send(
            CanFrame(arbitration_id, bytes(data), extended=extended, source=self.name)
        )

    # -- Hookpunkte --------------------------------------------------------- #
    def on_start(self) -> None:
        """Wird beim Start des Worker-Threads aufgerufen."""

    def on_stop(self) -> None:
        """Wird beim Beenden aufgerufen."""

    @abstractmethod
    def on_frame(self, frame: CanFrame) -> None:
        """Wird im Worker-Thread fuer jeden empfangenen Frame aufgerufen."""


class VirtualCanBus:
    """Frame-Bus mit Fan-out an alle interessierten Teilnehmer."""

    def __init__(
        self,
        name: str = "CAN",
        trace: TraceRecorder | None = None,
        clock: SimClock | None = None,
    ) -> None:
        self.name = name
        self.trace = trace
        self.clock = clock or SimClock()
        self._nodes: list[CanNode] = []
        self._lock = threading.RLock()
        self._frame_counter = itertools.count(1)

    # -- Teilnehmerverwaltung ------------------------------------------------ #
    def attach(self, node: CanNode) -> None:
        with self._lock:
            if node not in self._nodes:
                self._nodes.append(node)

    def detach(self, node: CanNode) -> None:
        with self._lock:
            if node in self._nodes:
                self._nodes.remove(node)

    @property
    def nodes(self) -> list[CanNode]:
        with self._lock:
            return list(self._nodes)

    # -- Datenverkehr -------------------------------------------------------- #
    def send(self, frame: CanFrame) -> None:
        if self.trace is not None:
            self.trace.record(
                TraceEntry(
                    ts=frame.ts,
                    bus=self.name,
                    source=frame.source,
                    kind="can",
                    data=frame.data,
                    arbitration_id=frame.arbitration_id,
                    text=f"CAN 0x{frame.arbitration_id:03X} [{len(frame.data)}]",
                    direction="tx",
                )
            )
        with self._lock:
            targets = [
                n for n in self._nodes if n.name != frame.source and n.accepts(frame.arbitration_id)
            ]
        for node in targets:
            node._deliver(frame)

    def trace_event(self, source: str, text: str) -> None:
        if self.trace is not None:
            self.trace.record(
                TraceEntry(
                    ts=time.time(), bus=self.name, source=source, kind="event", text=text
                )
            )


# --------------------------------------------------------------------------- #
# K-Line
# --------------------------------------------------------------------------- #
class KLineNode(ABC):
    """Teilnehmer am virtuellen K-Line-Bus."""

    kind = "kline"

    #: Teilnehmer, die gerade adressiert sind, quittieren Bytes mit dem Komplement
    listening = True

    def __init__(self, name: str, bus: "KLineBus") -> None:
        self.name = name
        self.bus = bus
        self._rx: "queue.Queue[tuple[int, bool]]" = queue.Queue()
        self.dropped_echoes: list[int] = []
        self._pending: list[tuple[int, bool]] = []
        bus.attach(self)

    # -- Senden -------------------------------------------------------------- #
    def send_bytes(self, data: bytes) -> None:
        self.bus.send_bytes(self, bytes(data))

    def _receive_byte(
        self, byte: int, *, echo: bool = False, source: str = "", echo_back: bool = True
    ) -> None:
        """Nimmt ein Byte auf. Ohne ``echo_back=False`` quittiert der Empfaenger
        es sofort mit seinem Einerkomplement (KWP1281-Pflicht, siehe
        ``kw1281test``: ``ReadAckByte``/``WriteByteAndReadAck``).

        Teilnehmer, die nicht adressiert sind (``listening = False``), sehen den
        Verkehr auf der Leitung nicht mit - auf der echten K-Line wuerden sie
        die Bytes zwar empfangen, aber nicht beantworten.
        """
        if not self.listening:
            return
        self._rx.put((byte, echo))
        if not echo and echo_back:
            self.bus.send_bytes(
                self, bytes([(~byte) & 0xFF]), echo=True, only=source, echo_back=False
            )

    def read_byte(self, timeout: float | None = 1.0, *, skip_echo: bool = True) -> int:
        """Liest ein Byte. Echo-Bytes (eigene Quittungen) werden optional uebersprungen."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            if remaining == 0.0:
                raise TimeoutError(f"{self.name}: Timeout beim Lesen von der K-Line")
            try:
                byte, echo = self._rx.get(timeout=remaining)
            except queue.Empty as exc:  # pragma: no cover - Zeitverhalten
                raise TimeoutError(f"{self.name}: Timeout beim Lesen von der K-Line") from exc
            if skip_echo and echo:
                self.dropped_echoes.append(byte)
                continue
            return byte

    def read_exact(self, count: int, timeout: float = 1.0) -> bytes:
        return bytes(self.read_byte(timeout=timeout) for _ in range(count))

    def flush(self) -> None:
        while True:
            try:
                self._rx.get_nowait()
            except queue.Empty:
                return

    def expect_echo(self, sent: int, timeout: float = 1.0) -> None:
        """Prueft die Komplement-Quittung fuer ein selbst gesendetes Byte."""
        while True:
            byte, echo = self._rx_get(timeout)
            if echo:
                if byte != (~sent) & 0xFF:
                    raise IOError(
                        f"{self.name}: falsche Byte-Quittung: erwartet "
                        f"{((~sent) & 0xFF):02X}, erhalten {byte:02X}"
                    )
                return
            # Fremdbyte im Puffer - zuruecklegen waere unnoetig, wir merken es uns.
            self._pushback(byte)

    def _pushback(self, byte: int) -> None:
        self._pending.append((byte, False))

    def _rx_get(self, timeout: float) -> tuple[int, bool]:
        if self._pending:
            return self._pending.pop(0)
        try:
            return self._rx.get(timeout=timeout)
        except queue.Empty as exc:  # pragma: no cover
            raise TimeoutError(f"{self.name}: Timeout auf der K-Line") from exc

class KLineEcu(KLineNode):
    """Steuergeraet am K-Line-Bus (VAG-Adresse, antwortet auf die 5-Baud-Adressierung)."""

    def __init__(self, name: str, bus: "KLineBus", address: int) -> None:
        super().__init__(name, bus)
        self.address = int(address)
        # Ein Steuergeraet hoert erst mit, nachdem es per 5-Baud-Init adressiert wurde.
        self.listening = False

    def wakeup_keywords(self, tester_address: int) -> tuple[int, int]:
        """Keywords fuer die 5-Baud-Adressierung, z. B. ``(0x01, 0x8A)`` = KWP1281."""
        return (0x01, 0x8A)

    def begin_session(self) -> None:
        """Wird aufgerufen, sobald die Keywords uebertragen wurden."""


class KLineBus:
    """Virtueller K-Line-Bus (KWP1281).

    Die Uebertragung erfolgt byteweise. Das Komplement-Echo erzeugt der jeweils
    empfangende Teilnehmer (``KLineNode._receive_byte``); damit verhalten sich
    Simulation und Protokollcode genau wie auf der echten Leitung, wo jedes
    Byte vom Empfaenger mit seinem Einerkomplement bestaetigt wird.
    """

    def __init__(
        self,
        name: str = "K-Line",
        trace: TraceRecorder | None = None,
        clock: SimClock | None = None,
    ) -> None:
        self.name = name
        self.trace = trace
        self.clock = clock or SimClock()
        self._nodes: list[KLineNode] = []
        self._lock = threading.RLock()

    def attach(self, node: KLineNode) -> None:
        with self._lock:
            if node not in self._nodes:
                self._nodes.append(node)

    def detach(self, node: KLineNode) -> None:
        with self._lock:
            if node in self._nodes:
                self._nodes.remove(node)

    @property
    def nodes(self) -> list[KLineNode]:
        with self._lock:
            return list(self._nodes)

    def send_bytes(
        self,
        source: KLineNode,
        data: bytes,
        *,
        echo: bool = False,
        only: str | None = None,
        echo_back: bool = True,
    ) -> None:
        """Sendet Bytes vom Teilnehmer ``source`` an alle anderen Teilnehmer.

        ``only`` schraenkt den Empfaengerkreis auf einen Teilnehmernamen ein
        (fuer die Komplement-Quittung, die nur dem Sender gilt).
        """
        with self._lock:
            targets = [n for n in self._nodes if n.name != source.name]
        if only is not None:
            targets = [n for n in targets if n.name == only]
        for byte in data:
            if self.trace is not None:
                self.trace.record(
                    TraceEntry(
                        ts=time.time(),
                        bus=self.name,
                        source=source.name,
                        kind="kline",
                        data=bytes([byte]),
                        text=f"K-Line {'Echo ' if echo else ''}0x{byte:02X}",
                        direction="tx" if not echo else "rx",
                    )
                )
            for node in targets:
                node._receive_byte(
                    byte, echo=echo, source=source.name, echo_back=echo_back
                )
            self.clock.byte_gap()

    # -- Initialisierung ----------------------------------------------------- #
    def wakeup(self, tester: KLineNode, ecu_address: int) -> int:
        """5-Baud-Adressierung + Keyword-Austausch. Liefert die Protokollversion.

        Ablauf (siehe ``kw1281test``, ``KwpCommon.WakeUpNoRetry``):

        1. Tester adressiert das Steuergeraet per 5-Baud-Init.
        2. Steuergeraet antwortet mit ``0x55``, Keyword-LSB, Keyword-MSB.
        3. Protokollversion = ``((kw_msb & 0x7F) << 7) + (kw_lsb & 0x7F)``.
        4. Tester quittiert mit dem Komplement des Keyword-MSB.
        """
        targets = [
            n for n in self.nodes if isinstance(n, KLineEcu) and n.address == ecu_address
        ]
        if not targets:
            raise TimeoutError(f"Kein Steuergeraet mit Adresse 0x{ecu_address:02X} am Bus")
        ecu = targets[0]
        tester.flush()
        if self.trace is not None:
            self.trace.record(
                TraceEntry(
                    ts=time.time(),
                    bus=self.name,
                    source=tester.name,
                    kind="event",
                    text=f"5-Baud-Initialisierung, Adresse 0x{ecu_address:02X}",
                )
            )
        self.clock.sleep(0.300)  # Wartezeit vor der Adressierung
        kw_lsb, kw_msb = ecu.wakeup_keywords(tester.address or 0xF1)
        # Die Keywords sendet das Steuergeraet; der Tester liest sie (ohne Quittung).
        self.send_bytes(ecu, bytes([0x55, kw_lsb, kw_msb]), echo_back=False)
        protocol_version = ((kw_msb & 0x7F) << 7) + (kw_lsb & 0x7F)
        ecu.begin_session()
        return protocol_version
