"""Kommandozeile des VCDS-Nachbaus.

Beispiele::

    vagdiag db build --force            # Datenbank aus den Seeds neu aufbauen
    vagdiag vehicles                    # Fahrzeuge und Steuergeraete auflisten
    vagdiag scan                        # Autoscan (VCDS-Textbericht)
    vagdiag faults --address 01 --clear # Fehlerspeicher loeschen
    vagdiag blocks --address 01 --group 1 --watch
    vagdiag coding --address 01 --set 00003
    vagdiag adapt --address 01 --read 4
    vagdiag dtc 16804                   # Fehlercode nachschlagen
    vagdiag serve --port 8000           # Weboberflaeche starten
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import APP_NAME, APP_SUBTITLE, __version__, address_text
from .client import DiagnosticError, VehicleSession
from .db import Database
from .log import DEFAULT_INTERVAL as DEFAULT_LOG_INTERVAL
from .obd.scan import format_autoscan, format_fault_list
from .obd.session import OBD_PID_AUSWAHL
from .proto.isotp import IsoTpError
from .proto.kw1281 import Kwp1281Error
from .proto.kwp2000 import Kwp2000Error
from .proto.uds import UdsError

#: Fehler, die ein einzelnes Steuergeraet melden kann, ohne dass die Sitzung stirbt
MODULE_ERRORS = (DiagnosticError, Kwp1281Error, Kwp2000Error, UdsError, IsoTpError, ValueError)

__all__ = ["main", "build_parser"]


# --------------------------------------------------------------------------- #
# Ausgabehelfer
# --------------------------------------------------------------------------- #
def _print_header(text: str) -> None:
    print()
    print("=" * len(text))
    print(text)
    print("=" * len(text))


def _format_module_list(modules) -> str:
    lines = [f"{'Adr.':<5} {'Steuergeraet':<34} {'Teilenummer':<14} {'Protokoll':<16} Bus"]
    lines.append("-" * 88)
    for module in modules:
        lines.append(
            f"{module.address_text:<4}   {module.name[:34]:<34} "
            f"{module.part_number[:14]:<14} {module.protocol[:16]:<16} {module.bus}"
        )
    return "\n".join(lines)


def _print_faults(faults) -> None:
    if not faults:
        print("   Kein Fehlereintrag im Fehlerspeicher.")
        return
    print(f"   {len(faults)} Fehler gefunden:")
    for fault in faults:
        text = fault.text or "(kein Eintrag in der Datenbank)"
        print(f"   {fault.display_code} - {fault.status_str:<7} {text}")
        if fault.text_short and fault.status_style != "uds":
            print(f"              {fault.text_short}")


def _session(args, *, drive: bool = True) -> VehicleSession:
    database = Database(args.db)
    return VehicleSession(
        db=database,
        vehicle_name=args.vehicle,
        drive=drive,
        speed=args.speed,
        clock_speed=args.clock_speed,
        log=(lambda text: print(f"  [{text}]")) if args.verbose else None,
    )


# --------------------------------------------------------------------------- #
# Kommandos
# --------------------------------------------------------------------------- #
def cmd_db_build(args) -> int:
    database = Database(args.db)
    existed = database.path.exists()
    print(f"Datenbank: {database.path}")
    database.build(force=args.force)
    if existed and not args.force:
        print("Datei ist vorhanden - unveraendert uebernommen (--force baut neu auf).")
    else:
        print("Datenbank neu aufgebaut aus Schema, Seeds und Labeldateien.")
    stats = database.stats()
    if args.json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        return 0
    for table, count in sorted(stats.items()):
        print(f"   {table:<20} {count:>6}")
    meta = database.meta()
    print(f"   Schema-Version {meta.get('schema_version')} - Stand {meta.get('built_at')}")
    return 0


def cmd_db_stats(args) -> int:
    database = Database(args.db)
    stats = database.stats()
    meta = database.meta()
    if args.json:
        print(json.dumps({"stats": stats, "meta": meta}, indent=2, ensure_ascii=False))
        return 0
    _print_header(f"{APP_NAME} - Datenbank")
    print(f"Datei          : {database.path}")
    print(f"Schema-Version : {meta.get('schema_version')}")
    print(f"Stand          : {meta.get('built_at')}")
    print()
    for table, count in sorted(stats.items()):
        print(f"   {table:<20} {count:>6}")
    return 0


def cmd_db_check(args) -> int:
    database = Database(args.db)
    findings = database.check()
    errors = [finding for finding in findings if finding["level"] == "fehler"]
    warnings = [finding for finding in findings if finding["level"] != "fehler"]
    if args.json:
        print(
            json.dumps(
                {
                    "database": str(database.path),
                    "errors": len(errors),
                    "warnings": len(warnings),
                    "findings": findings,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1 if errors else 0
    _print_header(f"{APP_NAME} - Datenbankpruefung")
    print(f"Datei   : {database.path}")
    print(f"Befunde : {len(errors)} Fehler, {len(warnings)} Warnungen")
    print()
    if not findings:
        print(
            "Keine Beanstandungen: Adressen, Labeldateien, Messwerte, Codier- und\n"
            "Anpassungshilfen, Stellgliedlisten sowie Fehlercodes sind vollstaendig "
            "und stimmig."
        )
        return 0
    for title, group in (("Fehler", errors), ("Warnungen", warnings)):
        if not group:
            continue
        print(f"{title}:")
        for finding in group:
            print(f"   {finding['scope'][:56]:<56} {finding['message']}")
        print()
    return 1 if errors else 0


def cmd_vehicles(args) -> int:
    database = Database(args.db)
    vehicles = database.vehicles()
    if args.json:
        payload = []
        for vehicle in vehicles:
            modules = database.modules_for_vehicle(vehicle["id"])
            payload.append(
                {
                    **dict(vehicle),
                    "modules": [module.as_dict() for module in modules],
                }
            )
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    _print_header(f"{APP_NAME} - Fahrzeugbestand ({len(vehicles)} Fahrzeuge)")
    for vehicle in vehicles:
        modules = database.modules_for_vehicle(vehicle["id"])
        print()
        print(f"{vehicle['name']}   [{vehicle.get('platform') or '-'}]")
        print(
            f"   Baujahr {vehicle.get('model_year')}   Motor {vehicle.get('engine')}   "
            f"Markt {vehicle.get('market') or '-'}"
        )
        if vehicle.get("notes"):
            print(f"   Hinweis: {vehicle['notes']}")
        print(f"   {len(modules)} Steuergeraete:")
        print(_format_module_list(modules))
    return 0


def cmd_scan(args) -> int:
    session = _session(args)
    try:
        session.start()
        addresses = None
        if args.addresses:
            addresses = [int(part, 16) if part.strip().lower().startswith("0x")
                         else int(part) for part in args.addresses.split(",")]

        def progress(info) -> None:
            if args.verbose:
                print(f"   -> {address_text(info.address)} {info.stage}: {info.message}")

        result = session.autoscan(progress=progress, addresses=addresses)
        if args.json:
            print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        else:
            print(format_autoscan(result))
        return 0 if all(m.status == "ok" for m in result.modules) else 1
    finally:
        session.stop()


def cmd_info(args) -> int:
    session = _session(args)
    try:
        addresses = (
            [int(args.address, 16)] if args.address else [m.address for m in session.modules()]
        )
        _print_header(f"{APP_NAME} - Steuergeraete-Identifikation")
        for address in addresses:
            module = session.module(address)
            if module is None:
                print(f"Adresse {address_text(address)}: nicht verbaut")
                continue
            with session.open_module(address) as session_module:
                ident = session_module.ident()
                print()
                print(f"Adresse {address_text(address)}: {module.name}")
                print(f"   Transport    : {ident.get('protocol') or session_module.transport_name}")
                print(f"   Teilenummer  : {module.part_number}   {module.component}")
                for line in ident.get("lines") or []:
                    print(f"   Identzeile   : {line}")
                text = (ident.get("text") or "").strip()
                if text and args.verbose:
                    print(text)
                print(f"   Codierung    : {ident.get('coding_text') or '-'}")
                print(f"   Werkstattcode: {ident.get('workshop_code_text') or '-'}")
                print(f"   Labeldatei   : {ident.get('label_file') or '-'}")
        return 0
    finally:
        session.stop()


def cmd_faults(args) -> int:
    session = _session(args)
    try:
        session.start()
        address = int(args.address, 16)
        with session.open_module(address) as module:
            if args.clear:
                module.clear_faults()
                print(f"Adresse {address_text(address)}: Fehlerspeicher geloescht.")
            faults = module.read_faults()
            if args.json:
                print(json.dumps([f.as_dict() for f in faults], indent=2, ensure_ascii=False))
                return 0
            _print_header(f"Fehlerspeicher Adresse {address_text(address)} ({module.module.name})")
            _print_faults(faults)
        return 0
    finally:
        session.stop()


def _render_block(reading: dict) -> str:
    lines = [f"Messwerteblock {reading['group']:03d}  {reading['title']}"]
    for field in reading["fields"]:
        unit = f" {field['unit']}" if field["unit"] else ""
        sensor = f"[Sensorart {field['sensor_id']:>2}: {field['a']:>3} {field['b']:>3}]"
        hint = f"   (Label: {field['unit_hint']})" if field.get("unit_hint") else ""
        lines.append(
            f"   Feld {field['field']}: {field['text'][:38]:<38} "
            f"{field['display']:>10}{unit:<8} {sensor}{hint}"
        )
    return "\n".join(lines)


def cmd_blocks(args) -> int:
    session = _session(args)
    try:
        session.start()
        address = int(args.address, 16)
        with session.open_module(address) as module:
            groups = (
                [int(group) for group in args.group.split(",")]
                if args.group
                else (module.block_numbers() if args.all else [1])
            )
            if args.json:
                payload = [
                    module.read_block(group, basic_setting=args.basic) for group in groups
                ]
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return 0
            _print_header(
                f"Messwertebloecke Adresse {address_text(address)} ({module.module.name})"
                + (" - Grundeinstellung" if args.basic else "")
            )
            if not args.watch:
                for group in groups:
                    print()
                    try:
                        print(_render_block(module.read_block(group, basic_setting=args.basic)))
                    except MODULE_ERRORS as exc:
                        # Labeldateien fuehren mehr Bloecke als jede einzelne
                        # Steuergeraetevariante beherrscht - wie VCDS melden
                        # und mit dem naechsten Block weitermachen.
                        print(f"Messwerteblock {group:03d}: nicht verfuegbar ({exc})")
                return 0
            print("Live-Anzeige - Abbruch mit Strg+C")
            try:
                while True:
                    lines = []
                    for group in groups:
                        try:
                            lines.append(_render_block(module.read_block(group, basic_setting=args.basic)))
                        except MODULE_ERRORS as exc:
                            lines.append(f"Messwerteblock {group:03d}: nicht verfuegbar ({exc})")
                    sys.stdout.write("\033[2J\033[H")  # Bildschirm leeren
                    print("\n".join(lines))
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nLive-Anzeige beendet.")
            return 0
    finally:
        session.stop()


def cmd_coding(args) -> int:
    session = _session(args)
    try:
        session.start()
        address = int(args.address, 16)
        with session.open_module(address) as module:
            if args.login is not None:
                ok = module.login(int(args.login))
                print(f"Login {args.login}: {'erfolgreich' if ok else 'abgelehnt'}")
            if args.set is not None:
                result = module.write_coding(args.set)
                print(f"Codierung geschrieben: {result}")
            coding = module.coding()
            _print_header(f"Codierung Adresse {address_text(address)} ({module.module.name})")
            print(f"   Codierung    : {coding['coding']}")
            print(f"   Protokoll    : {coding['protocol']}")
            label = module.label() or {}
            entries = label.get("coding", [])
            if any(entry.get("kind") == "C" for entry in entries):
                print("   Codieroptionen:")
                for entry in entries:
                    if entry.get("kind") != "C":
                        continue
                    value = entry.get("value")
                    marker = "  " if value is not None else ""
                    bit = entry.get("bit")
                    print(
                        f"      Gruppe {entry.get('block'):>2}"
                        f"{'' if bit is None else f', Bit {bit}'}{marker} "
                        f"{'' if value is None else f'{value:>4}'}  {entry.get('text') or ''}"
                    )
            if any(entry.get("kind") == "L" for entry in entries):
                print("   Login-/Adaptionskanaele:")
                for entry in entries:
                    if entry.get("kind") != "L":
                        continue
                    value = entry.get("value")
                    print(
                        f"      Kanal {entry.get('block'):>3} "
                        f"{'' if value is None else f'{value:>6}'}  {entry.get('text') or ''}"
                    )
        return 0
    finally:
        session.stop()


def cmd_adapt(args) -> int:
    session = _session(args)
    try:
        session.start()
        address = int(args.address, 16)
        with session.open_module(address) as module:
            if args.set:
                channel, _, value = args.set.partition("=")
                result = module.adaptation_save(int(channel), int(value))
                print(f"Kanal {channel} gespeichert: {result}")
            channels = module.adaptations()
            _print_header(f"Anpassungskanaele Adresse {address_text(address)} ({module.module.name})")
            if args.read is not None:
                try:
                    result = module.adaptation_read(int(args.read))
                except MODULE_ERRORS as exc:
                    print(f"   Kanal {args.read}: nicht verfuegbar ({exc})")
                    return 2
                print(f"   Kanal {result['channel']}: Wert {result['value']} - {result['description']}")
                return 0
            if args.json:
                print(json.dumps(channels, indent=2, ensure_ascii=False))
                return 0
            for entry in channels:
                try:
                    current = module.adaptation_read(int(entry["channel"]))
                except MODULE_ERRORS as exc:
                    print(f"   Kanal {int(entry['channel']):>3}: nicht verfuegbar ({exc})")
                    continue
                print(
                    f"   Kanal {int(entry['channel']):>3}: {entry['text'][:34]:<34} "
                    f"Wert {current['value']}   Bereich {entry.get('min_value')}-{entry.get('max_value')} "
                    f"{entry.get('unit') or ''}"
                )
        return 0
    finally:
        session.stop()


def cmd_dtc(args) -> int:
    database = Database(args.db)
    if not args.code:
        _print_header(f"{APP_NAME} - Fehlercodes in der Datenbank")
        print(f"   {database.stats().get('dtc', 0)} Eintraege - Suche mit 'vagdiag dtc <Code>'")
        return 0
    result = database.dtc_lookup(args.code)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    if not result:
        print(f"Kein Eintrag zu {args.code}")
        return 1
    _print_header(f"Fehlercode {args.code}")
    print(f"   VAG-Code     : {result.get('vag_code') or '-'}")
    print(f"   SAE-Code     : {result.get('sae_code') or '-'}")
    print(f"   Text         : {result.get('text') or '-'}")
    print(f"   Kurztext     : {result.get('text_short') or '-'}")
    print(f"   System       : {result.get('system') or '-'}")
    print(f"   Hinweis      : {result.get('status_hint') or '-'}")
    print(f"   Quelle       : {result.get('source') or '-'}")
    return 0


def cmd_label(args) -> int:
    database = Database(args.db)
    label = database.label(args.file) if args.file else None
    if label is None and args.address:
        session = _session(args)
        try:
            module = session.module(int(args.address, 16))
            if module is not None:
                label = database.label_for_module(module)
        finally:
            session.stop()
    if label is None:
        print("Keine Labeldatei gefunden (--file oder --address angeben).")
        return 1
    _print_header(f"Labeldatei {label['name']}")
    print(f"   Teilenummer : {label.get('part_number') or '-'}")
    if label.get("redirects"):
        print(f"   Verweis     : {', '.join(label['redirects'])}")
    for entry in label.get("measure_extra", []) or []:
        print(f"   Zusatz      : {entry['block']:03d}/{entry['field']} {entry['text']}")
    for entry in label.get("measuring", []):
        basic = "G" if entry.get("basic") else " "
        print(
            f"   {basic} {entry['block']:03d}.{entry['field']} {entry['text'] or '':<38} "
            f"{entry.get('unit') or '':<6} {entry.get('short_text') or ''}"
        )
    for entry in label.get("coding", []):
        print(f"   C {entry.get('block')} {entry.get('value') or '':>6} {entry.get('text')}")
    return 0


def cmd_actuator(args) -> int:
    """Stellglieddiagnose und Grundeinstellung (VCDS: Stellglieddiagnose)."""
    session = _session(args)
    try:
        session.start()
        address = int(args.address, 16)
        with session.open_module(address) as module:
            _print_header(
                f"Stellglieddiagnose Adresse {address_text(address)} ({module.module.name})"
            )
            actuators = module.actuators()
            if args.json:
                payload = {
                    "stellglieder": actuators,
                    "aktiv": module.actuator_active,
                    "grundeinstellung": module.basic_settings(),
                }
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return 0
            if not actuators:
                print("   Fuer dieses Steuergeraet ist keine Stellgliedliste hinterlegt.")
            for entry in actuators:
                marker = "*" if entry["id"] == module.actuator_active else " "
                print(f"  {marker} {entry['id']:02d}  {entry['text']}")

            if args.basic_off:
                for entry in module.basic_settings():
                    if entry["group"] == args.basic_off:
                        info = module.basic_setting(entry["group"], on=False)
                        print(f"\nGrundeinstellung Block {entry['group']:03d} beendet.")
                        break
                else:
                    print(f"\nGrundeinstellung Block {args.basic_off:03d}: nicht in der Labeldatei.")
            elif args.basic is not None:
                info = module.basic_setting(args.basic)
                print(f"\nGrundeinstellung Block {args.basic:03d}: {info['text']}")
                if info.get("block"):
                    print()
                    print(_render_block(info["block"]))
            elif args.sequence:
                if not actuators:
                    return 1
                print("\nReihenfolge der Stellglieddiagnose - Abbruch mit Strg+C")
                try:
                    for entry in actuators:
                        result = module.output_test(entry["id"])
                        print(
                            f"   {entry['id']:02d} {entry['text'][:44]:<44} "
                            f"{result['response']}"
                        )
                        time.sleep(args.interval)
                    module.output_test(0)
                    print("   Alle Stellglieder wieder abgeschaltet.")
                except KeyboardInterrupt:
                    module.output_test(0)
                    print("\n   Abgebrochen - alle Stellglieder abgeschaltet.")
            elif args.release or args.test == 0:
                result = module.output_test(0)
                print(f"\n{result['response']}")
            elif args.test is not None:
                result = module.output_test(args.test)
                if result["text"]:
                    print(f"\n{result['response']}: {result['text']}")
                else:
                    print(f"\n{result['response']} (unbekannte Kennung)")
            else:
                print("\nMit --test <ID> ansteuern, --test 0 schaltet alle ab,")
                print("--sequence faehrt die Liste durch, --basic <Block> startet die Grundeinstellung.")
        return 0
    finally:
        session.stop()


def cmd_log(args) -> int:
    """Messwerte mitschreiben (VCDS: Log / CSV-Datei)."""
    from .log import LogRecorder, row_from_block

    session = _session(args)
    try:
        session.start()
        address = int(args.address, 16)
        out = Path(args.out) if args.out else Path("var/logs") / f"messwerte-{int(time.time())}.csv"
        groups = [int(group) for group in args.blocks.split(",")]
        with session.open_module(address) as module:

            def sampler():
                collected, rows = [], []
                for group in groups:
                    block = module.read_block(group, basic_setting=args.basic)
                    collected.extend(block_columns_of(block))
                    rows.append(row_from_block(block))
                return collected, rows

            recorder = LogRecorder(
                sampler,
                interval=args.interval,
                path=out,
                vehicle=session.name,
                module=f"{address_text(address)} {module.module.name}",
            )
            recorder.duration = args.duration if args.duration and args.duration > 0 else None
            recorder.sample()  # erste Zeile bestimmt die Spalten
            columns = recorder.columns
            if not columns:
                print("Keine Messwerte gelesen - Aufzeichnung abgebrochen.", file=sys.stderr)
                return 2
            print(
                f"Schreibe {len(columns)} Messwerte nach {out} (Intervall {recorder.interval:.2f} s"
                + (
                    f", Dauer {recorder.duration:.0f} s)"
                    if recorder.duration
                    else ", Abbruch mit Strg+C)"
                )
            )
            for column in columns:
                print(f"   {column.header}")
            recorder.start()
            started = time.monotonic()
            try:
                while recorder.running:
                    if args.duration and time.monotonic() - started >= args.duration:
                        break
                    time.sleep(0.1)
            except KeyboardInterrupt:
                print("\nAbgebrochen.")
            recorder.stop()
            path = recorder.save()
            if args.json:
                print(json.dumps(recorder.as_dict(), indent=2, ensure_ascii=False))
            else:
                print(
                    f"Fertig: {len(recorder.rows)} Zeilen geschrieben ({path}), "
                    f"{len(recorder.errors)} Fehler."
                )
                for message in recorder.errors[:5]:
                    print(f"   Hinweis: {message}")
        return 0
    finally:
        session.stop()


def block_columns_of(block: dict):
    from .log import Column

    return [
        Column(
            group=int(block["group"]),
            field=int(entry["field"]),
            text=str(entry.get("text") or f"Feld {entry['field']}"),
            unit=str(entry.get("unit") or ""),
            basic=bool(block.get("basic")),
        )
        for entry in block["fields"]
    ]


def _print_readiness(readiness, prefix: str = "   ") -> None:
    """Bereitschaftsanzeige wie im VCDS-OBD-Bildschirm."""
    print(
        f"{prefix}MIL: {'an' if readiness.mil_on else 'aus'}   "
        f"Fehlercodes: {readiness.dtc_count}   "
        f"Motorart: {'Diesel (Selbstzuendung)' if readiness.compression else 'Benziner (Fremdzuendung)'}"
    )
    ready = [name for name in readiness.complete]
    print(f"{prefix}Bereitschaft abgeschlossen : {', '.join(ready) or '-'}")
    print(f"{prefix}Bereitschaft offen         : {', '.join(readiness.incomplete) or 'keine'}")


def cmd_obd(args) -> int:
    """OBD-II nach SAE J1979 (VCDS: Reiter "OBD-II")."""
    from .obd.session import ObdSession

    session = _session(args)
    try:
        session.start()
        with ObdSession(session) as obd:
            if args.json:
                payload = obd.as_dict()
                payload["monitors"] = obd.monitors()
                payload["live"] = [entry.as_dict() for entry in obd.live()]
                print(json.dumps(payload, indent=2, ensure_ascii=False))
                return 0
            only_readiness = args.readiness_only
            if not only_readiness:
                _print_header(f"OBD-II ({obd.transport_name})")
            if args.clear:
                obd.clear_codes()
                print("Fehlerspeicher geloescht (Modus 04).")
            if args.clear_only:
                return 0
            info = obd.vehicle_info()
            show_info = args.info or not any(
                (args.codes, args.live, args.monitors, args.freeze, args.readiness_only)
            )
            if show_info or args.info:
                print(f"   Fahrgestellnummer : {info['vin'] or '-'}")
                print(f"   Steuergeraet      : {info['ecu_name'] or '-'}")
                print(f"   Softwarestand     : {info['calibration_id'] or '-'}")
                print(f"   Pruefsumme (CVN)  : {info['cvn'] or '-'}")
            if show_info or args.readiness or args.info or only_readiness:
                readiness = obd.readiness()
                if not only_readiness:
                    print()
                _print_readiness(readiness)
            if only_readiness:
                return 0
            if args.codes or show_info:
                print()
                print("Fehlerspeicher (Modus 03):")
                codes = obd.codes(0x03)
                if not codes:
                    print("   keine gespeicherten Fehler")
                for code in codes:
                    print(f"   {code['code']}  {code['text'] or 'unbekannter Code'}")
                pending = obd.codes(0x07)
                if pending:
                    print("Fehler mit Rahmenbedingungen (Modus 07):")
                    for code in pending:
                        print(f"   {code['code']}  {code['text'] or 'unbekannter Code'}")
                permanent = obd.codes(0x0A)
                if permanent:
                    print("Dauerhafte Fehler (Modus 0A):")
                    for code in permanent:
                        print(f"   {code['code']}  {code['text'] or 'unbekannter Code'}")
                vag_only = obd.vag_only_codes()
                if vag_only:
                    print("VAG-Codes ohne OBD-Schreibweise (nur in der VAG-Diagnose)")
                    for code in vag_only:
                        print(f"   {code['code']}  {code['text']}   Status {code['status']}")
            if args.freeze:
                print()
                print("Freeze Frame (Modus 02):")
                entries = obd.freeze_frame()
                if not entries:
                    print("   kein Freeze Frame gespeichert")
                for entry in entries:
                    print(f"   PID 0x{entry.pid:02X}  {entry.name:<36} {entry.text}")
            if args.monitors:
                print()
                print("Ergebnisse der Bordueberwachung (Modus 06):")
                entries = obd.monitors()
                if not entries:
                    print("   keine Ergebnisse gemeldet")
                for entry in entries:
                    state = "innerhalb" if entry["ok"] else "ausserhalb"
                    print(
                        f"   MID {entry['mid']} ({entry['name'][:28]:<28}) "
                        f"Wert {entry['value']:>6}  erlaubt {entry['min']}-{entry['max']}  {state}"
                    )
            if args.live:
                print()
                print(
                    f"Live-Messwerte (Modus 01), Abstand {args.interval:.1f} s"
                    + (
                        f", Dauer {args.duration:.0f} s"
                        if args.duration
                        else " - Strg+C beendet"
                    )
                )
                started = time.monotonic()
                try:
                    while True:
                        entries = obd.live()
                        sys.stdout.write("\033[2J\033[H")
                        print(f"OBD-II Live ({obd.transport_name})")
                        for entry in entries:
                            unit = f" {entry.unit}" if entry.unit else ""
                            print(f"   PID 0x{entry.pid:02X}  {entry.name:<36} {entry.text}{unit}")
                        if args.duration and time.monotonic() - started >= args.duration:
                            break
                        time.sleep(args.interval)
                except KeyboardInterrupt:
                    print("\nLive-Anzeige beendet.")
        return 0
    finally:
        session.stop()


def cmd_serve(args) -> int:
    from .web.server import run_server

    database = Database(args.db)
    return run_server(
        db=database,
        host=args.host,
        port=args.port,
        vehicle=args.vehicle,
        open_browser=args.browser,
    )


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def _common_options() -> argparse.ArgumentParser:
    """Optionen, die vor *und* nach dem Unterkommando stehen duerfen."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS, help="Pfad zur SQLite-Datenbank")
    common.add_argument("--vehicle", default=argparse.SUPPRESS, help="Fahrzeug (Name oder ID)")
    common.add_argument(
        "--speed", type=float, default=argparse.SUPPRESS, help="Zeitraffer der Simulation"
    )
    common.add_argument(
        "--clock-speed",
        type=float,
        default=argparse.SUPPRESS,
        help="Busgeschwindigkeit (0 = ohne Wartezeit)",
    )
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="Busprotokoll zeigen"
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_options()
    parser = argparse.ArgumentParser(
        prog="vagdiag",
        description=f"{APP_NAME} - {APP_SUBTITLE}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
        epilog=(
            "Messwertebloecke werden als (Sensorart, A, B) uebertragen; die Anzeige "
            "rechnet sie\nueber die Formeltabelle (Sensorarten 1..70) in Wert und Einheit um "
            "und ergaenzt\nden Text aus der Labeldatei der Datenbank."
        ),
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    db_parser = sub.add_parser("db", parents=[common], help="Datenbank verwalten")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    build = db_sub.add_parser("build", parents=[common], help="Datenbank aus Seeds neu aufbauen")
    build.add_argument("--force", action="store_true", help="vorhandene Datei ueberschreiben")
    build.add_argument("--json", action="store_true")
    build.set_defaults(func=cmd_db_build)
    stats = db_sub.add_parser("stats", parents=[common], help="Kennzahlen anzeigen")
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_db_stats)
    check = db_sub.add_parser(
        "check", parents=[common], help="Adressen, Labeldateien und Fehlercodes pruefen"
    )
    check.add_argument("--json", action="store_true")
    check.set_defaults(func=cmd_db_check)

    vehicles = sub.add_parser(
        "vehicles", parents=[common], help="Fahrzeuge und Steuergeraete auflisten"
    )
    vehicles.add_argument("--json", action="store_true")
    vehicles.set_defaults(func=cmd_vehicles)

    scan = sub.add_parser("scan", parents=[common], help="Autoscan durchfuehren (VCDS-Bericht)")
    scan.add_argument("--json", action="store_true")
    scan.add_argument("--addresses", help="nur diese Adressen, z. B. 01,03,2E")
    scan.set_defaults(func=cmd_scan)

    info = sub.add_parser("info", parents=[common], help="Steuergeraete-Identifikation")
    info.add_argument("--address", default=None, help="Adresse hexadezimal, z. B. 01")
    info.set_defaults(func=cmd_info)

    faults = sub.add_parser("faults", parents=[common], help="Fehlerspeicher lesen/loeschen")
    faults.add_argument("--address", default="01")
    faults.add_argument("--clear", action="store_true", help="Fehlerspeicher loeschen")
    faults.add_argument("--json", action="store_true")
    faults.set_defaults(func=cmd_faults)

    blocks = sub.add_parser("blocks", parents=[common], help="Messwertebloecke lesen")
    blocks.add_argument("--address", default="01")
    blocks.add_argument("--group", default=None, help="Blocknummer(n), z. B. 1 oder 1,3,11")
    blocks.add_argument("--all", action="store_true", help="alle Blocke des Steuergeraets")
    blocks.add_argument("--basic", action="store_true", help="Grundeinstellung (B...-Eintraege)")
    blocks.add_argument("--watch", action="store_true", help="Live-Anzeige")
    blocks.add_argument("--interval", type=float, default=0.5)
    blocks.add_argument("--json", action="store_true")
    blocks.set_defaults(func=cmd_blocks)

    coding = sub.add_parser("coding", parents=[common], help="Codierung lesen/schreiben")
    coding.add_argument("--address", default="01")
    coding.add_argument("--set", default=None, help="neue Codierung")
    coding.add_argument("--login", default=None, help="Login-Code vor dem Schreiben")
    coding.set_defaults(func=cmd_coding)

    adapt = sub.add_parser("adapt", parents=[common], help="Anpassungskanaele lesen/schreiben")
    adapt.add_argument("--address", default="01")
    adapt.add_argument("--read", default=None, help="Kanal lesen")
    adapt.add_argument("--set", default=None, help="Kanal schreiben, z. B. 4=32768")
    adapt.add_argument("--json", action="store_true")
    adapt.set_defaults(func=cmd_adapt)

    dtc = sub.add_parser("dtc", parents=[common], help="Fehlercode in der Datenbank nachschlagen")
    dtc.add_argument("code", nargs="?", default=None, help="z. B. 16804, P0420 oder 00575")
    dtc.add_argument("--json", action="store_true")
    dtc.set_defaults(func=cmd_dtc)

    label = sub.add_parser("label", parents=[common], help="Labeldatei anzeigen")
    label.add_argument("--file", default=None, help="Dateiname, z. B. 038906018G.lbl")
    label.add_argument("--address", default=None, help="ueber Steuergeraet suchen")
    label.set_defaults(func=cmd_label)

    actuator = sub.add_parser(
        "actuator", parents=[common], help="Stellglieddiagnose und Grundeinstellung"
    )
    actuator.add_argument("--address", default="01")
    actuator.add_argument("--test", type=int, default=None, help="Stellglied ansteuern (0 = alle aus)")
    actuator.add_argument("--release", action="store_true", help="alle Stellglieder abschalten")
    actuator.add_argument("--sequence", action="store_true", help="Liste der Reihe nach durchfahren")
    actuator.add_argument("--interval", type=float, default=1.0, help="Wartezeit in der Reihenfolge")
    actuator.add_argument("--basic", type=int, default=None, help="Grundeinstellung starten (Block)")
    actuator.add_argument(
        "--basic-off", type=int, default=None, help="Grundeinstellung beenden (Block)"
    )
    actuator.add_argument("--json", action="store_true")
    actuator.set_defaults(func=cmd_actuator)

    log = sub.add_parser(
        "log", parents=[common], help="Messwerte in eine CSV-Datei mitschreiben"
    )
    log.add_argument("--address", default="01")
    log.add_argument("--blocks", default="1", help="Messwertebloecke, z. B. 1 oder 1,3,11")
    log.add_argument("--basic", action="store_true", help="Grundeinstellungsbloecke schreiben")
    log.add_argument("--interval", type=float, default=DEFAULT_LOG_INTERVAL)
    log.add_argument("--duration", type=float, default=0.0, help="Laufzeit in Sekunden (0 = frei)")
    log.add_argument("--out", default=None, help="Zieldatei (.csv)")
    log.add_argument("--json", action="store_true")
    log.set_defaults(func=cmd_log)

    obd = sub.add_parser(
        "obd", parents=[common], help="OBD-II lesen (Fehler, Bereitschaft, Live-Werte)"
    )
    obd.add_argument("--codes", action="store_true", help="Fehlerspeicher (Modus 03/07)")
    obd.add_argument("--clear", action="store_true", help="Fehlerspeicher loeschen (Modus 04)")
    obd.add_argument(
        "--clear-only", action="store_true", help="nur loeschen, nichts weiter ausgeben"
    )
    obd.add_argument("--info", action="store_true", help="Fahrzeuginformation (Modus 09)")
    obd.add_argument("--readiness", action="store_true", help="Bereitschaftsbits (Modus 01 PID 01)")
    obd.add_argument(
        "--readiness-only", action="store_true", help="nur die Bereitschaftsanzeige ausgeben"
    )
    obd.add_argument("--monitors", action="store_true", help="Bordueberwachung (Modus 06)")
    obd.add_argument("--freeze", action="store_true", help="Freeze Frame (Modus 02)")
    obd.add_argument("--live", action="store_true", help="Live-Messwerte (Modus 01)")
    obd.add_argument("--count", type=int, default=len(OBD_PID_AUSWAHL))
    obd.add_argument("--interval", type=float, default=1.0, help="Abstand der Live-Anzeige")
    obd.add_argument(
        "--duration", type=float, default=0.0, help="Laufzeit der Live-Anzeige in Sekunden"
    )
    obd.add_argument("--json", action="store_true")
    obd.set_defaults(func=cmd_obd)

    serve = sub.add_parser("serve", parents=[common], help="Weboberflaeche im VCDS-Look starten")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--browser", action="store_true", help="Browser oeffnen")
    serve.set_defaults(func=cmd_serve)
    return parser


DEFAULTS = {
    "json": False,
    "verbose": False,
    "db": None,
    "vehicle": None,
    "speed": 6.0,
    "clock_speed": 0.0,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    for name, value in DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        return 130
    except (DiagnosticError, TimeoutError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    except (Kwp1281Error, Kwp2000Error, UdsError, IsoTpError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
