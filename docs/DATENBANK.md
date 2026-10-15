# Datenbank

Die Diagnose-Datenbank ist eine einzelne SQLite-Datei. Sie enthaelt den
Fahrzeugbestand, die Steuergeraete, die Label-Dateien samt Eintraegen, die
Fehlercodes und die Anpassungskanaele. Aufgebaut wird sie aus Schema, Seeds und
Label-Dateien - die Datei ist also jederzeit reproduzierbar und nicht die
Quelle der Wahrheit.

```bash
python3 -m vagdiag.cli db build --force     # neu aufbauen
python3 -m vagdiag.cli db stats             # Kennzahlen anzeigen
python3 -m vagdiag.cli db check             # Bestand pruefen
```

Speicherort in dieser Reihenfolge: `--db <pfad>`, `$VAGDIAG_DB`,
`<Repo>/var/vagdiag.sqlite3`. Fehlt die Datei, wird sie beim ersten Zugriff
automatisch angelegt.

## Tabellen

| Tabelle | Inhalt | Stand |
| --- | --- | --- |
| `control_module` | Adresstabelle des VW-Konzerns: Adresse, deutsche und englische Bezeichnung, Kategorie, Beschreibung, OBD-Relevanz | 39 |
| `platform` | Plattformen (PQ34/Golf IV, B5.5/Passat, B6/A4, PQ35/Golf V) mit Jahren, Protokoll und Bus | 4 |
| `vehicle` | Fahrzeuge mit Plattform, Modelljahr, Motor, Markt | 4 |
| `vehicle_module` | Steuergeraete je Fahrzeug: Adresse, Protokoll, Bus, Teilenummer, Bauteil, Software, Codierung, Werkstattcode, Labeldatei, Fehlersatzkennung | 25 |
| `label_file` | Label-Dateien mit Teilenummer und Quelle | 14 |
| `label_redirect` | `Redir:`-Verweise zwischen Label-Dateien | 1 |
| `measuring_block` | Messwert- und Grundeinstellungseintraege (`basic = 1`) mit Text, Kurztext, Einheit und Grenzwerten | 248 |
| `actuator_entry` | Stellgliedliste der Labeldatei (`A`-Eintraege) mit Stellglied-ID, Schrittfolge und Text | 56 |
| `coding_entry` | Codier- und Login-/Anpassungshilfen (`kind = C`/`L`) mit Gruppe, Bit und Wert | 56 |
| `adaptation_channel` | Anpassungskanaele je Labeldatei oder Adresse mit Text, Bereich und Einheit | 18 |
| `dtc` | Fehlercodes: VAG-5-stellig (57), SAE/OBD-II (153) mit deutschem Text, Kurztext, System und Hinweis | 210 |
| `meta` | Schemaversion und Aufbauzeitpunkt | 2 |
| `vehicle_module_view` | Sicht fuer Auswertungen (Fahrzeug, Adressanzeige, Modul, Protokoll, Bus) | - |

Die Adressanzeige der Sicht liefert die zweistellige Schreibweise (`01`, `46`),
also die Nummer eines Steuergeraets so, wie sie im Adressplan und im
Autoscan-Bericht erscheint.

## Seeds

`vagdiag/db/seed/` enthaelt alles, was zum Aufbau gebraucht wird:

```
control_modules.json   Adressen mit Namen, Kategorie, Beschreibung, OBD-Kennzeichen
vehicles.json          Plattformen, Fahrzeuge und ihre Steuergeraete
dtc_vag.json           VAG-5-stellige Fehlercodes (KWP1281-Generation)
dtc_sae.json           SAE/OBD-II-Codes (P/C/B/U) inklusive UDS-Verwendung
adaptations.json       Anpassungskanaele je Labeldatei/Adresse
labels/*.lbl           14 Label-Dateien im VCDS-Format
```

Jede Datei traegt oben eine `source`-Angabe, damit nachvollziehbar bleibt,
woraus die Eintraege stammen. `_seed_vehicles()` prueft beim Aufbau, dass jede
Steuergeraeteadresse eines Fahrzeugs in `control_modules.json` existiert -
fehlt eine, bricht der Aufbau mit einer klaren Meldung ab.

## Label-Dateien

Das VCDS-Format wird von `vagdiag/label/lbl.py` gelesen:

```
; Kommentar
Redir: 038906019.lbl          ; Verweis auf eine weitere Datei
:001,0                        ; Abschnittsueberschrift (Block, Feld)
001,0,Grundeinstellung        ; Blocktitel (Feld 0)
001,1,Motordrehzahl,0.2*A*B,1/min,780,950
B001,1,Grundeinstellung Leerlauf
A001,1,Gluehkerzenrelais        ; Stellglieddiagnose: ID 1, Schritt 1
C01,1,1,2,Schaltgetriebe      ; Codierhilfe: Gruppe 1, Bit 1, Wert 2
L03,Adaption AGR-Menge        ; Kanal 3 (ohne Wert = nur Beschreibung)
```

Fuer Messwerte gilt die erweiterte Spaltenfolge
`NNN,F,Text,Kurztext,Einheit,Min,Max`:

* `Text` ist der Klartext des Feldes, `Kurztext` die Formel bzw. der Hinweis
  (z. B. `0.2*A*B`), `Einheit` die gewuenschte Einheit und `Min`/`Max` die
  Grenzwerte fuer Balkenanzeige und Plausibilitaet.
* Steht in der Einheitenspalte `?`, uebernimmt die Anzeige die Einheit der
  Sensortyp-Formel. Weicht die angegebene Einheit von der Formel ab (z. B.
  `mbar` gegen `bar`), gilt die Formel - die abweichende Angabe erscheint als
  Hinweis (Spalte `unit_hint` der Weboberflaeche bzw. `(Label: ...)` in der
  Kommandozeile).
* `A<ID>,<Schritt>,Text` beschreibt die Stellglieddiagnose. Die Reihenfolge
  ergibt sich aus der Schrittspalte; `vagdiag actuator --sequence` fahert sie
  der Reihe nach ab, `1`-Eintraege ohne Reihenfolge stehen am Ende.
* `B<Block>,<Feld>,Text` beschreibt die Grundeinstellung. Die Anzeige liest
  denselben Block wie im Normalbetrieb, nur mit Blocktitel `0x28` bzw. der
  Routine `FC<Block>` - die Werte stammen damit aus der laufenden
  Grundstellung (Foerderbeginn, Partikelfilter-Regeneration, ...).
* `Redir:`-Ketten loest die Datenbank beim Lesen auf
  (`Database.label_for_module`), sodass Ergaenzungen einer Variante die
  Basiseintraege nicht verdecken.

Eigene Dateien: einfach als `*.lbl` in ein Verzeichnis legen und
`Database.load_label_dir(pfad)` aufrufen oder direkt in
`vagdiag/db/seed/labels/` ablegen und die Datenbank neu aufbauen.

## Fehlercodes

Drei Kodierungen werden unterschieden:

| Art | Beispiel | Vorkommen |
| --- | --- | --- |
| VAG-5-stellig | `00575` | KWP1281-Blockdialog |
| SAE/OBD-II | `P0299` | KWP2000, OBD-II, UDS |
| UDS-DTC | `P2463` + Fehlerartbyte | UDS-Fahrzeuge |

Die Tabelle wird gelesen ueber `Database.dtc_lookup(code)`; dabei sind sowohl
`16804` als auch `P0420` als Suchbegriff moeglich. Die Umrechnung
VAG-16xxx -> P-Code (`P = VAG - 16384`) ist nur im belegten Bereich
16384..17407 zulaessig; ausserhalb bleibt das SAE-Feld absichtlich leer,
statt eine falsche Zuordnung zu erfinden.

Zusaetzliche Codes aus CSV (`code;text;system`, `#` = Kommentar):

```python
from vagdiag.db import Database

database = Database("var/vagdiag.sqlite3")
count = database.import_dtc_csv("meine_codes.csv")
```

Ein grosser Teil der allgemeinen OBD-II-Codes ist bereits als Seed enthalten;
die CSV-Schnittstelle ist der Weg, eine vollstaendige Liste nachzuladen.

## Selbstpruefung

`Database.check()` (Kommando `vagdiag db check`) vergleicht den aufgebauten
Bestand mit dem, was die Diagnose erwartet. Geprueft wird unter anderem:

| Pruefung | Stufe |
| --- | --- |
| Schemaversion entspricht `SCHEMA_VERSION` | Fehler |
| Steuergeraeteadresse existiert in `control_module` | Fehler |
| Labeldatei eines Steuergeraets existiert (auch wenn `generic.lbl` einspringt) | Fehler |
| `Redir:`-Ziel einer Labeldatei existiert | Fehler |
| Codier- und Anpassungshilfen: Art gueltig, Wert im Bereich, Bereich nicht leer | Fehler |
| Fehlercode: Text vorhanden, Art gueltig, VAG-Code 1..65535, SAE-Code im Format `P/C/B/U####` | Fehler |
| Messwertbereich (`min` < `max`) | Fehler |
| Stellglied-ID groesser 0, Text vorhanden, Schrittzahl nicht doppelt | Fehler |
| Steuergeraet mit Messwerten, aber ohne Stellgliedliste | Warnung |
| Codierung eines Steuergeraets vorhanden, numerisch und ungleich 0 | Warnung |
| Messwerteblock ohne Titel, Labeldatei von keinem Fahrzeug verwendet | Warnung |
| Anpassungskanal ohne Beschreibung oder Wertebereich | Warnung |

Der Rueckgabewert ist 1, sobald mindestens ein Fehler gefunden wurde - das
Kommando taugt damit als Pruefschritt. `--json` liefert die Befunde maschinen-
lesbar. Absichtlich nur als Rueckfallebene dienende Labeldateien stehen in
`db.FALLBACK_LABELS` und werden nicht als "unbenutzt" beanstandet.

## Zugriff aus Python

```python
from vagdiag.db import Database

database = Database()

database.vehicles()                                # alle Fahrzeuge
database.modules_for_vehicle("VW Golf IV 1.9 TDI (2001)")
database.control_module(1)                         # Adresstabelle
database.block_numbers(module)                     # Messwertebloecke
database.block_text(label, 1, 1)                   # Feldtext aus der Labeldatei
database.adaptation_channels(module)               # Anpassungskanaele
database.label_for_module(module)                  # Labeldatei inkl. Redir
database.actuator_list(module)                     # Stellgliedliste der Labeldatei
database.dtc_lookup("P0420")
database.stats()
database.check()                                   # Befunde der Selbstpruefung
```

Alle Zugriffe laufen ueber dieselbe Verbindung mit
`sqlite3.Row`-Ergebnissen; Fremdschluessel sind aktiv, das Schema ist
versioniert (`meta.schema_version`).
