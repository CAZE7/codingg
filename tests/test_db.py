"""Tests fuer die Steuergeraete-Datenbank (Aufbau aus Schema, Seeds, Labeldateien)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from vagdiag import db as db_mod
from vagdiag.db import Database, DatabaseError


class DatabaseBuildTest(unittest.TestCase):
    """Baut die Datenbank einmalig in ein temporaeres Verzeichnis auf."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls._tmp.name) / "vagdiag-test.sqlite3"
        cls.db = Database(cls.path)
        cls.db.build(force=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        cls._tmp.cleanup()

    def test_datei_angelegt(self) -> None:
        self.assertTrue(self.path.exists())
        self.assertGreater(self.path.stat().st_size, 20_000)

    def test_aufbau_ohne_force_laesst_datei_unveraendert(self) -> None:
        stamp = self.path.stat().st_mtime_ns
        from vagdiag.db import Database as Db

        Db(self.path).build(force=False)
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)

    def test_schemaversion_und_stand(self) -> None:
        meta = self.db.meta()
        self.assertEqual(meta["schema_version"], db_mod.SCHEMA_VERSION)
        self.assertTrue(meta["built_at"])

    def test_statistik_vollstaendig(self) -> None:
        stats = self.db.stats()
        for table in (
            "control_module",
            "vehicle",
            "vehicle_module",
            "label_file",
            "measuring_block",
            "coding_entry",
            "dtc",
        ):
            with self.subTest(table=table):
                self.assertGreater(stats[table], 0)
        self.assertGreaterEqual(stats["control_module"], 30)
        self.assertGreaterEqual(stats["vehicle"], 4)
        self.assertGreaterEqual(stats["label_file"], 10)
        self.assertGreaterEqual(stats["dtc"], 150)

    def test_jedes_fahrzeug_hat_module_mit_adresse_01(self) -> None:
        vehicles = self.db.vehicles()
        self.assertGreaterEqual(len(vehicles), 4)
        for vehicle in vehicles:
            with self.subTest(vehicle=vehicle["name"]):
                modules = self.db.modules_for_vehicle(vehicle["name"])
                self.assertGreaterEqual(len(modules), 5)
                self.assertIn(1, [module.address for module in modules])
                self.assertTrue(all(module.protocol for module in modules))

    def test_kontrollmodul_adresse_01(self) -> None:
        module = self.db.control_module(1)
        self.assertIsNotNone(module)
        self.assertIn("Motor", module["name"])

    def test_golf_iv_motorsteuergeraet(self) -> None:
        modules = self.db.modules_for_vehicle("VW Golf IV 1.9 TDI (2001)")
        engine = next(module for module in modules if module.address == 1)
        self.assertEqual(engine.part_number, "038906018G")
        self.assertEqual(engine.protocol, "KWP1281")
        self.assertEqual(engine.coding, "00002")
        self.assertEqual(engine.label_file, "038906018G.lbl")
        self.assertEqual(self.db.block_numbers(engine), [1, 2, 3, 4, 10, 11, 13, 15])

    def test_fehlercode_suche(self) -> None:
        p0420 = self.db.dtc_lookup("P0420")
        self.assertIsNotNone(p0420)
        self.assertIn("Katalysator", p0420["text"])
        vag = self.db.dtc_lookup(16683)
        self.assertIsNotNone(vag)
        self.assertTrue(vag["text"])

    def test_label_resolution_mit_redirect(self) -> None:
        """038906019HJ.lbl verweist per Redir auf die Basisdatei 038906019.lbl."""
        modules = self.db.modules_for_vehicle("VW Passat B5.5 1.9 TDI (2003)")
        engine = next(module for module in modules if module.address == 1)
        self.assertEqual(engine.label_file, "038906019HJ.lbl")
        label = self.db.label_for_module(engine)
        self.assertIsNotNone(label)
        self.assertEqual(label["name"], "038906019.lbl")
        self.assertEqual(label["redirects"], [])
        self.assertGreaterEqual(len(label["measuring"]), 10)
        self.assertEqual(self.db.block_text(label, 1, 1), "Motordrehzahl")

    def test_blocktext_aus_labeldatei(self) -> None:
        modules = self.db.modules_for_vehicle("VW Golf IV 1.9 TDI (2001)")
        engine = next(module for module in modules if module.address == 1)
        label = self.db.label_for_module(engine)
        self.assertEqual(self.db.block_text(label, 1, 0), "Grundeinstellung (Leerlauf)")
        self.assertEqual(self.db.block_text(label, 1, 1), "Motordrehzahl")

    def test_anpassungskanaele(self) -> None:
        modules = self.db.modules_for_vehicle("VW Golf IV 1.9 TDI (2001)")
        engine = next(module for module in modules if module.address == 1)
        channels = self.db.adaptation_channels_for_module(engine)
        self.assertGreaterEqual(len(channels), 3)
        numbers = [channel["channel"] for channel in channels]
        self.assertIn(4, numbers)

    def test_doppelter_aufbau_erzeugt_dieselben_zahlen(self) -> None:
        before = self.db.stats()
        self.db.build(force=True)
        self.assertEqual(self.db.stats(), before)


class DatabaseImportTest(unittest.TestCase):
    def test_csv_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "codes.sqlite3"
            csv_path = Path(tmp) / "codes.csv"
            csv_path.write_text(
                "# Kommentar\nP0123;Drosselklappensensor A - hoher Eingang;SAE\n"
                "U0100;Verbindung zum Motorsteuergeraet verloren;SAE\n",
                encoding="utf-8",
            )
            database = Database(path)
            imported = database.import_dtc_csv(csv_path)
            self.assertEqual(imported, 2)
            entry = database.dtc_lookup("P0123")
            self.assertIsNotNone(entry)
            self.assertIn("Drosselklappe", entry["text"])
            database.close()

    def test_fehlende_adresse_wird_gemeldet(self) -> None:
        """Der Seed-Prueflauf erkennt Steuergeraete, die in der Tabelle fehlen."""
        with tempfile.TemporaryDirectory() as tmp:
            seed_path = Path(tmp) / "vehicles.json"
            seed_path.write_text(
                json.dumps(
                    {
                        "platforms": [
                            {"name": "Testplattform", "protocol": "KWP1281", "bus": "K-Line"}
                        ],
                        "vehicles": [
                            {
                                "name": "Testfahrzeug",
                                "platform": "Testplattform",
                                "modules": [{"address": 0x99, "part_number": "X000000"}],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            database = Database(Path(tmp) / "pruef.sqlite3")
            connection = database.connect()
            with self.assertRaises(DatabaseError):
                database._seed_vehicles(connection, seed_path)
            database.close()


if __name__ == "__main__":
    unittest.main()


class LabelChainTest(unittest.TestCase):
    """Redir-Ketten und Anpassungskanaele."""

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        cls._tmp = tempfile.TemporaryDirectory()
        cls.db = Database(Path(cls._tmp.name) / "kette.sqlite3")
        cls.db.build(force=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()
        cls._tmp.cleanup()

    def test_redir_kette(self) -> None:
        self.assertEqual(
            self.db._label_chain("038906019HJ.lbl"), ["038906019HJ.lbl", "038906019.lbl"]
        )
        self.assertEqual(self.db._label_chain("038906018G.lbl"), ["038906018G.lbl"])
        self.assertEqual(self.db._label_chain(""), [])

    def test_kanaele_der_basisdatei_ueber_redir(self) -> None:
        modules = self.db.modules_for_vehicle("VW Passat B5.5 1.9 TDI (2003)")
        engine = next(module for module in modules if module.address == 1)
        channels = self.db.adaptation_channels(engine)
        numbers = [channel["channel"] for channel in channels]
        self.assertEqual(numbers, sorted(set(numbers)))
        self.assertIn(4, numbers)

    def test_a4_motor_hat_eigene_kanaele(self) -> None:
        modules = self.db.modules_for_vehicle("Audi A4 B6 1.8T (2003)")
        engine = next(module for module in modules if module.address == 1)
        channels = self.db.adaptation_channels(engine)
        texts = {channel["channel"]: channel["text"] for channel in channels}
        self.assertIn(1, texts)
        self.assertEqual(texts[1], "Leerlaufdrehzahl Sollwert")

    def test_adresstext(self) -> None:
        from vagdiag import address_text, address_wire

        self.assertEqual(address_text(1), "01")
        self.assertEqual(address_text(46), "46")
        self.assertEqual(address_wire(46), 0x46)
        self.assertEqual(address_wire(1), 0x01)
