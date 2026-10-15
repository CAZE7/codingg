"""Tests fuer den KWP1281-Dialog (Blockcodec, Quittungen, Kommandos)."""

from __future__ import annotations

import unittest

from vagdiag.proto.bus import KLineBus, SimClock, TraceRecorder
from vagdiag.proto.kw1281 import (
    Block,
    BlockTitle,
    FaultEntry,
    Kwp1281EcuServer,
    Kwp1281Tester,
    Kwp1281Error,
    status_text,
)


class DummyModule:
    """Minimales Steuergeraet fuer Tests (erfuellt die ModuleFunctions-Schnittstelle)."""

    def __init__(self) -> None:
        self.coding_value = 2
        self.wsc = 12345
        self.cleared = False
        self.logged_in = False
        self.adaptations = {1: 128, 2: 100}
        self.faults = [FaultEntry(575, 0x91), FaultEntry(500, 0x11)]

    def ident_blocks(self):
        return [b"1J0906018A  1.9l R4 EDC G00SG  1087"]

    def coding(self):
        return (self.coding_value, self.wsc)

    def set_coding(self, coding, workshop_code):
        self.coding_value = coding
        self.wsc = workshop_code
        return True

    def fault_codes(self):
        return list(self.faults)

    def clear_fault_codes(self):
        self.faults = []
        self.cleared = True
        return []

    def group_read(self, group, basic_setting=False):
        if group == 1:
            return [(1, 25, 80), (5, 90, 145), (2, 10, 30), (7, 0, 0)]
        if group == 3:
            return [(2, 30, 41), (12, 0, 0), (0, 0, 0), (0, 0, 0)]
        return None

    def adaptation_read(self, channel):
        return self.adaptations.get(channel)

    def adaptation_save(self, channel, value, workshop_code):
        if channel not in self.adaptations:
            return None
        self.adaptations[channel] = value
        return value

    def actuator_test(self, value):
        return bytes([value, 0x00])

    def login(self, code):
        self.logged_in = code == 12233
        return self.logged_in


class BlockCodecTest(unittest.TestCase):
    def test_roundtrip(self) -> None:
        block = Block(counter=0x03, title=BlockTitle.GROUP_READ, body=bytes([0x01]))
        raw = block.to_bytes()
        self.assertEqual(raw, bytes([0x04, 0x03, 0x29, 0x01, 0x03]))
        again = Block.from_bytes(raw)
        self.assertEqual(again.counter, 0x03)
        self.assertEqual(again.title, BlockTitle.GROUP_READ)
        self.assertEqual(again.body, b"\x01")

    def test_length_check(self) -> None:
        with self.assertRaises(Kwp1281Error):
            Block.from_bytes(bytes([0x09, 0x01, 0x07, 0x00, 0x03]))

    def test_status_text(self) -> None:
        self.assertEqual(status_text(0x91), "17-10")  # sporadisch
        self.assertEqual(status_text(0x11), "17-00")  # statisch
        self.assertEqual(status_text(0x88), "08-10")


class DialogueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.trace = TraceRecorder()
        self.bus = KLineBus(trace=self.trace, clock=SimClock(0))
        self.module = DummyModule()
        self.ecu = Kwp1281EcuServer("SG-01", self.bus, 0x01, self.module)
        self.ecu.start()
        self.tester = Kwp1281Tester("VAGDiag", self.bus)
        self.addCleanup(self.ecu.stop)

    def test_connect_reads_ident_and_coding(self) -> None:
        ident = self.tester.connect(0x01, timeout=2.0)
        self.assertIn("1J0906018A", ident.text)
        self.assertEqual(ident.coding, 2)
        self.assertEqual(ident.workshop_code, 12345)
        self.tester.end()

    def test_read_fault_codes(self) -> None:
        self.tester.connect(0x01, timeout=2.0)
        entries = self.tester.read_fault_codes(timeout=2.0)
        self.assertEqual([e.code for e in entries], [575, 500])
        self.assertTrue(entries[0].sporadic)
        self.assertFalse(entries[1].sporadic)
        self.tester.end()

    def test_clear_fault_codes(self) -> None:
        self.tester.connect(0x01, timeout=2.0)
        self.tester.clear_fault_codes(timeout=2.0)
        self.assertTrue(self.module.cleared)
        self.assertEqual(self.tester.read_fault_codes(timeout=2.0), [])
        self.tester.end()

    def test_group_read(self) -> None:
        self.tester.connect(0x01, timeout=2.0)
        reading = self.tester.group_read(1, timeout=2.0)
        self.assertEqual(reading.values[0], (1, 25, 80))
        self.assertEqual(len(reading.values), 4)
        with self.assertRaises(Kwp1281Error):
            self.tester.group_read(99, timeout=2.0)
        self.tester.end()

    def test_adaptation_and_coding_and_login(self) -> None:
        self.tester.connect(0x01, timeout=2.0)
        self.assertEqual(self.tester.adaptation_read(1, timeout=2.0).value, 128)
        self.assertEqual(self.tester.adaptation_save(1, 200, 12345, timeout=2.0).value, 200)
        self.assertEqual(self.module.adaptations[1], 200)
        self.assertFalse(self.tester.login(99999, timeout=2.0))
        self.assertTrue(self.tester.login(12233, timeout=2.0))
        ident = self.tester.write_coding(42, 12345, timeout=2.0)
        self.assertEqual(ident.coding, 42)
        self.assertEqual(self.module.coding_value, 42)
        self.assertEqual(self.tester.actuator_test(0x01, timeout=2.0), b"\x01\x00")
        self.tester.end()

    def test_multiple_dialogs_and_trace(self) -> None:
        for _ in range(3):
            self.tester.connect(0x01, timeout=2.0)
            self.tester.read_fault_codes(timeout=2.0)
            self.tester.end()
        self.assertEqual(self.ecu.dialogs, 3)
        kline_entries = [e for e in self.trace.tail(2000) if e.kind == "kline"]
        self.assertGreater(len(kline_entries), 40)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
