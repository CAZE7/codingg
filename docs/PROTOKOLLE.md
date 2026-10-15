# Protokollebenen

Dieses Dokument beschreibt, welche Dienste und Formate `vagdiag.proto`
implementiert und wie die virtuelle Verkabelung (`vagdiag.proto.bus`) den
Test ohne Fahrzeug ermoeglicht.

## Uebersicht

| Modul | Inhalt | Fahrzeuge im Simulator |
| --- | --- | --- |
| `proto/bus.py` | Virtueller K-Line- und CAN-Bus, Trace, 5-Baud-Initialisierung, Halbduplex-Quittung | - |
| `proto/kw1281.py` | KWP1281-Blockdialog (Tester und Steuergeraet) | Golf IV, Passat B5.5 (K-Line), A4 B6 (K-Line-Abschnitte) |
| `proto/kwp2000.py` | KWP2000-Dienste ueber K-Line und CAN | Passat B5.5, A4 B6 |
| `proto/uds.py` | UDS (ISO 14229) | Golf V |
| `proto/vwtp.py` | VW TP 2.0 (Transportprotokoll auf CAN) | alle CAN-Module |
| `proto/isotp.py` | ISO-TP (ISO 15765-2) als Rueckfallebene | optional |

## Adressierung

Steuergeraeteadressen werden im VW-Konzern mit zwei hexadezimal notierten
Stellen angegeben: `01` Motor, `03` ABS, `15` Airbag, `17` Kombiinstrument,
`19` Gateway, `46` Komfortsystem, `56` Radio.

* Anzeige: `vagdiag.address_text(46) == "46"`
* Busadresse: `vagdiag.address_wire(46) == 0x46`

Der Simulator setzt auf dem Bus dieselben Adressbytes ein wie ein echtes
Steuergeraet; der Autoscan-Bericht zeigt die vertraute zweistellige
Schreibweise.

## KWP1281 (Blockdialog)

Jeder Block hat den Aufbau

```
LEN | CNT | TITLE | BODY... | 0x03
```

* `LEN` = Laenge einschliesslich Ueberzaehlung, Titel, Rumpf und Endbyte,
  **ohne** das Laengenbyte selbst
* `CNT` = Blockzaehler (Antworten des Tester mit `0x80` markiert)
* Abschluss `0x03` wird nicht quittiert
* Jedes Byte wird vom Empfaenger mit seinem **Einerkomplement** quittiert
  (Halbduplex-Verhalten der echten K-Line)

Unterstuetzte Titel (Auszug):

| Titel | Bedeutung | Antwort |
| --- | --- | --- |
| `0x00` | Identifikation lesen | `0xF6` (ASCII-Daten) |
| `0x07` | Fehlerspeicher lesen | `0xFC` |
| `0x05` | Fehlerspeicher loeschen | `0x09` (ACK) |
| `0x29` | Messwerteblock lesen | `0xE7` (3-Byte-Tripel) |
| `0x28` | Grundeinstellung lesen | `0xE7` |
| `0x21`/`0x22`/`0x2A` | Anpassung lesen/testen/speichern | `0xE6`/`0x09` |
| `0x04` | Stellglieddiagnose | `0xF5` |
| `0x10` | Codierung schreiben | `0xF6` mit Codierung/Werkstattcode |
| `0x2B` | Login | `0x09`/`0x0A` |
| `0x09`/`0x0A` | Quittung / Ablehnung | - |
| `0x06` | Dialogende | - |

Fehlerspeicher: 3 Byte je Eintrag (Code hoch, Code tief, Status), Listenende
`0xFF 0xFF 0x88`. Der Status wird wie bei VCDS als `AA-BB` dargestellt
(`s & 0x7F` und `(s >> 7) * 10`), Bit 7 markiert "sporadisch".

Codierung/Login im Rumpf:

```
SoftwareCoding: 0x10 | (coding*2)>>8 | (coding*2)&0xFF | WSC high | WSC low
Login:          0x2B | code>>8 | code&0xFF | WSC3
Anpassung lesen:    0x21 | Kanal
Anpassung testen:   0x22 | Kanal | Wert high | Wert low
Anpassung speichern:0x2A | Kanal | Wert high | Wert low | WSC3
```

5-Baud-Initialisierung: 300 ms Pause, 5-Baud-Adressbyte, dann Antwort
`0x55`, Keyword niedrig, Keyword hoch; der Tester quittiert mit dem Komplement
des hohen Keyword-Bytes. Aus den Keywords ergibt sich die Protokollversion
(`((kw_hi & 0x7F) << 7) + (kw_lo & 0x7F)`, ueblich 1281).

## KWP2000

| Dienst | Bedeutung |
| --- | --- |
| `0x10` | Sitzung starten |
| `0x11` | Steuergeraet zuruecksetzen |
| `0x14` | Fehlerspeicher loeschen |
| `0x18` | Fehlerspeicher lesen |
| `0x1A` | Kennung lesen (Teilenummer, Bauteil, Software, Codierung, Werkstattcode, Datum) |
| `0x21` | Messwerteblock lesen (Grundeinstellung) |
| `0x22` | Messwerteblock lesen (Messwerte) |
| `0x27` | Security Access |
| `0x2E` | Datensatz schreiben |
| `0x30`/`0x31` | Ein-/Ausgang einlesen bzw. steuern (Anpassung, Stellglieder) |
| `0x3B` | Codierung schreiben |
| `0x3E` | Tester Present |

Negative Antworten (`0x7F`) werden mit ihrem Antwortcode (NRC) gemeldet
(`response_code_text`).

Anpassungskanaele laufen bei VAG ueber den Dienst `0x31` mit eigener
Belegung:

```
31 00 <Kanal>               Anpassung lesen      -> 71 <Kanal> <Wert hi> <Wert lo>
31 02 <Kanal>               Anpassung pruefen    -> 71 <Kanal> <Wert hi> <Wert lo>
31 01 <Kanal> <Hi> <Lo>     Anpassung speichern  -> 71 <Kanal> <Wert hi> <Wert lo>
```

Der kurze Aufruf `31 01 <Routine hi> <Routine lo>` (RoutineByLocalIdentifier)
ist vom Speichern eines Kanals durch die Laenge unterschieden.

## UDS (ISO 14229)

| Dienst | Bedeutung |
| --- | --- |
| `0x10` | Diagnosesitzung starten |
| `0x11` | Steuergeraet zuruecksetzen |
| `0x14` | Fehlerspeicher loeschen |
| `0x19` | Fehlerinformation lesen (Subfunktionen `0x02`/`0x0A`) |
| `0x22`/`0x23` | Daten lesen (Kennung bzw. Speicherbereich) |
| `0x27` | Security Access |
| `0x2E`/`0x2F` | Datensatz schreiben / Ein-/Ausgang steuern |
| `0x31` | Routine starten/stoppen/ergebnis |
| `0x3E` | Tester Present |

Kennungen (RIDs), Auszug: `F190` Fahrgestellnummer, `F187`-`F18A`
Teilenummer/Bauteil/Software/Hardware, `F191` Seriennummer, `F1A0` Codierung,
`F400` Messwerteblock, `F500` Anpassung.

Fehlercodes sind drei Byte lang (16-Bit-Code nach SAE-Schema plus
Fehlerart-Byte). Das Statusbyte wird bitweise ausgewertet:

| Bit | Bedeutung |
| --- | --- |
| 0 | Fehler aktiv |
| 1 | Fehler in diesem Fahrzyklus |
| 2 | ausstehend |
| 3 | bestaetigt |
| 4 | Test seit Loeschen nicht gelaufen |
| 5 | Fehler seit Loeschen aufgetreten |
| 6 | in diesem Zyklus nicht gelaufen |
| 7 | Warnleuchte angefordert |

Die Anzeige fasst zusammen: Bit 0+3 -> "statisch", nur Bit 0 -> "sporadisch",
Bit 2 -> "ausstehend", sonst "gespeichert".

## VW TP 2.0

Transportprotokoll fuer die CAN-Diagnose. Kanalaufbau:

1. Tester sendet an CAN-ID `0x200`:
   `[Zieladresse, 0xC0, RX_ID high, RX_ID low, TX_ID high, TX_ID low, Protokoll]`
2. Steuergeraet antwortet mit `0xD0` auf `0x200 + eigene Adresse`
   (Tester-ID im oberen Nibble, z. B. `0xF1` -> `0x2F1`).
3. Das erste Datenbyte traegt die Sekundaerparameter (`0xA0`): Blockgroesse,
   Quittungszeit und Paketintervall (`Skalierung [0,1/1/10/100] ms`).
4. Nutzdaten: Opcode-Nibble, `0x10` = letzter Frame, `0x20` = keine Quittung.
   Quittungen sind `0xB0|Sequenznummer`; `0xA3`/`0xA4` sind Ping/BRK,
   `0xA8`/`0xA9` bauen den Kanal ab.

KWP-Nutzdaten (Protokollversion 1) beginnen mit einer 16-Bit-Big-Endian-Laenge;
pro Frame passen 7 Nutzbytes. Die Simulation benutzt dieselben Frames, der
Trace der Weboberflaeche zeigt sie im Rohformat.

## ISO-TP

Fuer Fahrzeuge, die Diagnose direkt per ISO-TP abwickeln (0x7E0/0x7E8):
Single- und Multi-Frame-Uebertragung mit Flow-Control und Zeitueberwachung.
Der Client probiert VW TP 2.0 zuerst und faellt bei Bedarf auf ISO-TP
zurueck (`ModuleSession._probe_transport`).

## Messwerte und Einheiten

Messwerte werden als Tripel (Sensortyp, A, B) uebertragen. Der Sensortyp
bestimmt die Formel (`vagdiag/obd/blocks.py`, Typen 1-70); die Labeldatei
liefert Text, Einheit und Grenzwerte. Regel fuer die Anzeige:

1. Nennt die Labeldatei eine Einheit, die zur Formel passt (z. B. `bar` zu
   Sensortyp 69), wird sie verwendet.
2. Steht dort `?` oder nichts, gilt die Einheit der Formel.
3. Weicht die Angabe von der Formel ab (z. B. `mbar` gegen `bar`), gilt die
   Einheit der Formel - die Zahl stammt schliesslich aus ihr -, und die
   abweichende Angabe wird als Hinweis gezeigt.

Nicht unterstuetzte Bloecke (Labeldateien fuehren mehr Bloecke, als eine
bestimmte Steuergeraetevariante beherrscht) werden wie bei VCDS gemeldet und
uebersprungen: `Messwerteblock 003: nicht verfuegbar (...)`.

## Virtueller Bus

`proto/bus.py` bildet die Leitung nach:

* `SimClock` erlaubt Zeitraffer (`clock_speed = 0` bedeutet "ohne Wartezeit").
* `TraceRecorder` sammelt Frames und Ereignisse fuer die Anzeige.
* `KLineBus`/`KLineNode` uebertragen byteweise inklusive Komplement-Quittung.
* `VirtualCanBus`/`CanNode` stellen Frames mit ID und Nutzdaten zu.
* `KLineBus.wakeup()` sendet die 5-Baud-Adressierung und liefert die
  Protokollversion.

Damit laufen dieselben Protokollimplementierungen, die spaeter mit Hardware
sprechen, gegen den Simulator - inklusive aller Quittungen, Zeitueberwachungen
und Fehlerpfade.
