# VAGDiag Studio - VCDS-Nachbau mit Datenbank

Eigenstaendiger, quelloffener Nachbau der VCDS-Diagnose fuer Fahrzeuge des
VW-Konzerns (VW / Audi / Seat / Skoda): Fehlerspeicher, Messwertebloecke,
Codierung, Anpassung, Stellglieddiagnose, Grundeinstellung, OBD-II, Log und
Autoscan - mit Steuergeraete-Datenbank (SQLite), Label-Datei-Parser, den
Protokollen KWP1281, KWP2000, UDS, VW TP 2.0, ISO-TP und ISO 14230 sowie
einem Fahrzeug-Simulator, damit alles ohne Fahrzeug und ohne Interface
ausprobiert werden kann.

```
$ vagdiag scan --vehicle "VW Golf IV 1.9 TDI (2001)"
Donnerstag,15,Oktober,2026,08:46:00:00000

VAGDiag Studio -- VCDS-Nachbau (quelloffen) 0.1.0

Fahrzeug: VW Golf IV 1.9 TDI (2001)
Fahrgestellnummer: WVWZZZ1JZ27537500

Adressplan:

Adresse 01: Motorsteuergerät (038906018G)
   038906018G  1.9l R4 EDC  G00SG  1087  1087
   Labeldatei: 038906018G.lbl
   Codierung: 00002   Werkstattcode: 00000
   2 Fehlercode(s) gefunden:
   00575 - 17-10
      Saugrohrdruck – Regelgrenze überschritten
      Ladedruck zu hoch
...
Autoscan abgeschlossen in 6.3 s - 6/6 Steuergeraete antworten, 7 Fehlercode(s).
```

## Was enthalten ist

| Bereich | Inhalt |
| --- | --- |
| Fehlerspeicher | Lesen und Loeschen; VAG-5-stellig (KWP1281), SAE/OBD-II (KWP2000) und UDS-Codes mit Statusbits ("statisch", "sporadisch", ...) |
| Messwerte | Messwertebloecke als (Sensortyp, A, B)-Tripel, umgerechnet ueber die Formeltabelle der Sensortypen 1-70; Texte, Einheiten und Grenzwerte aus den Label-Dateien |
| Codierung | Codierung lesen und schreiben (inkl. Login), Codierhilfen der Label-Datei, Langcodierung auf Bit-Ebene (Byte/Bit setzen und loeschen, gesetzte Bits mit Text) |
| Anpassung | Kanaele lesen, schreiben, testen (KWP1281 0x21/0x22/0x2A, KWP2000 0x30/0x31, UDS 0x2F/0x31) |
| Stellglieddiagnose | Liste aus der Labeldatei (A-Eintraege), einzelne Stellglieder ansteuern und wieder abschalten (KWP1281 0x04, KWP2000 0x30, UDS 0x31/Routine) |
| Grundeinstellung | Grundstellungsbloecke (B-Eintraege) starten und beenden, Werte dabei live lesen (KWP1281 0x28/0x29, KWP2000/UDS Routine `FC<Gruppe>`) |
| OBD-II / EOBD | Modi 01 bis 0A: Live-Daten, Freeze Frame, Fehlerspeicher (gespeichert/schwebend/permanent), Loeschen, Monitortestergebnisse, Fahrzeuginformation, Bereitschaftsbits (ISO 15765-4 auf CAN 0x7E0/0x7E8, ISO 14230 auf der K-Line) |
| Log | Messwerte mitschreiben: Spalten aus Blockspalte, Text und Einheit, Semikolon-getrennte CSV-Datei mit Zeitstempel - als Kommando und als Reiter der Oberflaeche |
| Kanalmap | Alle Messwertebloecke und Anpassungskanaele eines Steuergeraets als Tabelle oder CSV-Datei (`vagdiag map`, Weboberflaeche) |
| Autoscan | Adressplan abarbeiten, Identifikation + Fehlerspeicher, Bericht im VCDS-Textformat |
| Datenbank | SQLite: Fahrzeuge, Steuergeraete, Label-Dateien, Messwert-/Grundeinstellungs-/Stellglied-/Codier-/Anpassungseintraege, Fehlercodes (VAG, SAE, UDS) |
| Weboberflaeche | Fahrzeugzustand, Live-Messwerte, Modulliste, Fehlerspeicher, Codierung, Anpassung, Stellglieder, OBD-II, Log, Autoscan, Busprotokoll - im VCDS-Look, ohne externe Ressourcen |
| Simulator | Vier Fahrzeuge (Golf IV, Passat B5.5, A4 B6, Golf V) mit virtuellem K-Line- und CAN-Bus (VW TP 2.0, ISO-TP), Fahrprofil, OBD-II-Diensten und realistischen Steuergeraeteantworten |

## Schnellstart

```bash
# Datenbank aufbauen (Schema + Seeds + Label-Dateien) und pruefen
python3 -m vagdiag.cli db build --force
python3 -m vagdiag.cli db check

# Fahrzeuge und Steuergeraete ansehen
python3 -m vagdiag.cli vehicles
python3 -m vagdiag.cli info --vehicle "VW Golf IV 1.9 TDI (2001)" --address 01

# Diagnose
python3 -m vagdiag.cli scan   --vehicle "Audi A4 B6 1.8T (2003)"
python3 -m vagdiag.cli faults --address 01 --clear
python3 -m vagdiag.cli blocks --address 01 --group 1 --watch
python3 -m vagdiag.cli coding --address 01 --login 12233 --set 00003 --show-bits
python3 -m vagdiag.cli map    --address 01 --out kanalmap.csv   # alle Werte in eine Tabelle
python3 -m vagdiag.cli adapt  --address 01 --read 4
python3 -m vagdiag.cli actuator --address 01 --sequence     # Stellglieddiagnose
python3 -m vagdiag.cli actuator --address 01 --basic 4      # Grundeinstellung
python3 -m vagdiag.cli log --address 01 --blocks 1,4 --duration 10 --out messwerte.csv
python3 -m vagdiag.cli obd --readiness --codes              # OBD-II / EOBD
python3 -m vagdiag.cli dtc 16804          # Fehlercode nachschlagen (auch P0420)
python3 -m vagdiag.cli label 038906018G   # Label-Datei anzeigen

# Weboberflaeche
python3 -m vagdiag.cli serve --port 8000
```

Installation als Paket (optional, dann heisst das Kommando `vagdiag`):

```bash
pip install -e .
vagdiag serve
```

Voraussetzungen: Python 3.11 oder neuer, keine externen Abhaengigkeiten
(ausschliesslich Standardbibliothek).

## Typische Arbeiten wie in VCDS

| VCDS | Hier |
| --- | --- |
| Auto-Scan (alle Steuergeraete) | `vagdiag scan` bzw. Knopf **Autoscan** |
| Steuergeraet waehlen, Kennung lesen | `vagdiag info --address 01`, Reiter **Steuergeraet** |
| Fehlerspeicher lesen / loeschen | `vagdiag faults --address 01 [--clear]`, Reiter **Fehlerspeicher** |
| Messwerteblock anzeigen (auch live) | `vagdiag blocks --address 01 --group 1 --watch`, Reiter **Messwerte** |
| Messwerte mitschreiben (Log/CSV) | Reiter **Log** (Start/Stop, CSV-Download) - `vagdiag log ... --out datei.csv` |
| Codierung lesen/schreiben | `vagdiag coding --address 01 --login 12233 --set 00003`, Reiter **Codierung** |
| Langcodierung Bit fuer Bit | `vagdiag coding --set-bit 3.2` / `--clear-bit 3.2` / `--show-bits`, Bit-Editor im Reiter **Codierung** |
| Anpassung lesen/schreiben | `vagdiag adapt --read 4` / `--set 4=32768`, Reiter **Anpassung** |
| SRI / Serviceintervall zuruecksetzen | `vagdiag adapt --address 17 --set 2=<Wert>` (Kanal 2 "Servicerueckstellung") |
| Grundeinstellung (Basic Settings) | `vagdiag actuator --basic 4`, Reiter **Stellglieder** unten |
| Stellglieddiagnose (Output Test) | `vagdiag actuator --sequence` bzw. `--test 2`, Reiter **Stellglieder** |
| OBD-II / EOBD | `vagdiag obd --readiness --codes --monitors --freeze --live`, Reiter **OBD-II** |
| Kanalmap / alles ausgeben | `vagdiag map --address 01 --out kanalmap.csv`, Knopf im Reiter **Messwerte** |
| Busprotokoll mitlesen | Reiter **Protokoll**, `-v` fuer den Trace auf der Kommandozeile |

## Aufbau

```
vagdiag/
  __init__.py      Version, App-Name, Adresshilfen (address_text/address_wire)
  cli.py           Kommandozeile
  client.py        Fahrzeug- und Steuergeraetesitzungen (open_module, autoscan, ...)
  db/              SQLite-Datenbank: schema.sql, Seeds, Label-Dateien
  label/lbl.py     Parser fuer VCDS-Label-Dateien (*.lbl)
  log.py           Messwertaufzeichnung (CSV im VCDS-Format)
  obd/             Fehlercodes (dtc.py), Sensorformeln (blocks.py), Autoscan (scan.py),
                   OBD-II-PIDs (obd2.py), OBD-II-Sitzung (session.py)
  proto/           bus.py, kw1281.py, kwp2000.py, uds.py, vwtp.py, isotp.py, iso14230.py
  sim/             Fahrzeugsimulator: profile.py, roles.py, ecu.py, vehicles.py
  web/             HTTP-Server und Oberflaeche (static/index.html)
```

Details: [docs/PROTOKOLLE.md](docs/PROTOKOLLE.md) (Protokollebenen, Dienste,
Leitungsformate, Einheitenregel) und [docs/DATENBANK.md](docs/DATENBANK.md)
(Tabellen, Seeds, Label-Format, Fehlercodes).

## Adressen

Der VW-Konzern nummeriert Steuergeraete mit zwei hexadezimal notierten
Stellen: Motor `01`, ABS `03`, Airbag `15`, Kombiinstrument `17`, Gateway `19`,
Komfortsystem `46`, Radio `56` usw. Angezeigt wird genau diese Schreibweise
(`vagdiag.address_text`), auf dem Bus gilt das entsprechende Byte
(`vagdiag.address_wire`, aus "46" wird `0x46`).

## Datenbank

Die SQLite-Datei liegt standardmaessig unter `var/vagdiag.sqlite3`
(ueberschreibbar mit `--db` oder `$VAGDIAG_DB`) und wird beim ersten Zugriff
automatisch aufgebaut. Sie enthaelt unter anderem:

* `control_module` - Adresstabelle des VW-Konzerns (39 Eintraege)
* `platform`, `vehicle`, `vehicle_module` - 4 Plattformen, 4 Fahrzeuge, 25 Steuergeraete
* `label_file`, `measuring_block`, `actuator_entry`, `coding_entry` - Label-Dateien und ihre Eintraege
  (Messwerte und Grundeinstellung, Stellgliedliste, Codier- und Login-Hilfen)
* `dtc` - Fehlercodes: VAG-5-stellig, SAE/OBD-II und UDS
* `adaptation_channel` - Anpassungskanaele je Steuergeraet/Label-Datei (18 Eintraege)

Der Bestand laesst sich jederzeit pruefen: `vagdiag db check` vergleicht
Adressen, Labeldateien, Messwerte, Codier- und Anpassungshilfen sowie
Fehlercodes auf Vollstaendigkeit und Stimmigkeit (Rueckgabewert 1, sobald ein
Fehler dabei ist - als Pruefschritt in einer Pipeline nutzbar). Befunde als
JSON: `vagdiag db check --json`.

Eigene Daten lassen sich per JSON-Seeds, CSV-Import (`import_dtc_csv`) oder
zusaetzliche Label-Dateien (`load_label_dir`) ergaenzen - siehe
[docs/DATENBANK.md](docs/DATENBANK.md).

## Simulator

Ohne Fahrzeug und ohne Interface arbeitet `vagdiag.sim` als Gegenstelle: ein
virtueller K-Line-Bus mit 5-Baud-Initialisierung, ein virtueller CAN-Bus mit
ISO-TP und VW TP 2.0 sowie Steuergeraete, die aus derselben Fachlogik
antworten wie die echten Protokolle es erwarten. Ein Fahrprofil
(Leerlauf, Beschleunigen, Konstantfahrt, Schubbetrieb, Bremsen) sorgt fuer
lebendige Messwerte.

```python
from vagdiag.client import VehicleSession

with VehicleSession(vehicle_name="VW Golf IV 1.9 TDI (2001)") as session:
    result = session.autoscan(progress=lambda p: print(p.stage, p.message))
    print(result.report)
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

149 Tests: KWP1281-Blockcodec samt Dialog, Label-Datei-Parser, Datenbankaufbau
samt Selbstpruefung (inkl. Redir-Ketten, Seed-Pruefung, CSV-Import, absichtlich
kaputte Seeds), Autoscan-Formatierung, Adress- und Codierungsanzeige, die
Zusammenfuehrung von Formel- und Label-Einheiten, komplette Diagnosedialoge
gegen den Simulator (K-Line, VW TP 2.0, KWP2000, UDS, Autoscan), die
Kanalhygiene der Busse (beendete Kanaele werden abgemeldet, ein Testerkanal
hoert nur den eigenen Dialog mit), die OBD-II-Auswertung und -Sitzung auf
K-Line und CAN, Stellglieddiagnose und Grundeinstellung auf allen drei
Transportwegen, die Messwertaufzeichnung samt CSV-Format sowie die
JSON-Schnittstelle der Oberflaeche (inkl. OBD-II, Stellglieder und Log) und die
Kommandozeile der neuen Befehle `actuator`, `log`, `obd` und `map` sowie die
Langcodierung auf Bit-Ebene.

```bash
python3 -m unittest discover -s tests -t . -v
```

## Grenzen und Ehrlichkeit

* Dieses Projekt ist eine **eigenstaendige Neuentwicklung**. Es enthaelt
  keinen Code von Ross-Tech und keine Bestandteile der Originalsoftware.
  Als Unterstuetzung beim Verstaendnis der Protokolle dienten die
  offenen Referenzumsetzungen [kw1281test](https://github.com/gmenounos/kw1281test)
  und [PyVCDS](https://github.com/baconwaifu/PyVCDS) sowie oeffentliche
  Messwertblock-Listen.
* "VCDS" ist eine Marke von Ross-Tech; der Name wird hier nur beschreibend
  verwendet ("Nachbau", "VCDS-Look").
* Die Fehlercode-, Label- und Messwertdaten sind eine **kuratierte Auswahl**,
  keine vollstaendige Reproduktion der Originaldaten. Wo Zuordnungen nicht
  belegbar sind (z. B. VAG-Codes ausserhalb des 16xxx-Bereichs), bleibt das
  SAE-Feld absichtlich leer, statt zu raten.
* Ein Betrieb an echten Fahrzeugen ist moeglich, aber nicht Teil dieses
  Repositoriums: Der Simulator antwortet auf denselben Protokollebenen wie ein
  Steuergeraet, ein physischer Transport (K-Line ueber `pyserial`, CAN ueber
  `python-can`) muss fuer Hardware ergaenzt werden.
* Die OBD-II-Auswertung deckt die PIDs ab, die das Projekt fuehrt (33 PIDs in
  `obd/obd2.py`, u. a. Bereitschaftsbits, Drehzahl, Geschwindigkeit,
  Kuehlmittel-, Luft- und Kraftstoffwerte, Lambdawerte, Bordspannung).
  Unbekannte PIDs werden als Rohdaten angezeigt, nicht geraten.
* Die Freischaltung (Security Access) ist implementiert, aber nur mit den
  Login-Codes, die in den Labeldateien hinterlegt sind (`L`-Eintraege,
  z. B. 12233).

## Lizenz

MIT - siehe [LICENSE](LICENSE).
