# Android-Medienbackup für Linux

Lokale grafische USB-Sicherung über ADB. Dateien aus verschiedenen Apps werden
gemeinsam nach Typ abgelegt. Fotos und Videos erhalten ausschließlich
Jahresunterordner. Andere Dateien liegen direkt im jeweiligen Typ-Ordner.
Die Auswahl basiert auf Android-Kategorien statt auf einer vollständigen
Durchsuchung aller App-Ordner. Sie kann von Files by Google abweichen.

## Installation

Benötigt werden Linux mit glibc, Python ab 3.11, Android Platform Tools (`adb`)
und ExifTool. Unter Ubuntu/Debian:

```sh
sudo apt install adb libimage-exiftool-perl python3-venv
```

Im Projektverzeichnis:

```sh
python3 -m venv ~/.local/share/android-media-backup/venv
~/.local/share/android-media-backup/venv/bin/python -m pip install -e .
~/.local/share/android-media-backup/venv/bin/android-backup
```

Die Python-Umgebung liegt bewusst auf dem Linux-Dateisystem, nicht auf dem
Projekt- oder Backup-Datenträger: FAT/exFAT unterstützen die für virtuelle
Umgebungen benötigten symbolischen Links nicht. PySide6-Essentials enthält
die benötigten Qt-Widgets; WebEngine und weitere Qt-Addons sind nicht erforderlich.

Alternativer Start mit derselben Python-Umgebung: `python -m android_backup`.
Falls Qt unter X11 fehlende Bibliotheken meldet, müssen die von Qt genannten
Systembibliotheken installiert werden (unter Ubuntu häufig `libxcb-cursor0`).
Die Anwendung verwendet eine native Qt-Oberfläche, keinen Browser.

## Start über das Anwendungsmenü

Auf diesem Rechner ist **Android-Medienbackup** im Anwendungsmenü eingerichtet.
Die Anwendungssuche öffnen (unter GNOME mit der Super-/Windows-Taste),
„Android-Medienbackup“ eingeben und die App starten. Ein Terminal ist dafür
nicht erforderlich. Über das Kontextmenü kann die App auch als Favorit
an die Dock-/Taskleiste angeheftet werden.

Der benutzerspezifische Starter liegt unter
`~/.local/share/applications/android-media-backup.desktop` und verwendet
die oben eingerichtete Python-Umgebung. Da die Installation mit `pip install -e .`
erfolgt, muss auch das Projektverzeichnis weiterhin verfügbar sein.

### Menüeintrag und Backup-Icon einrichten

Die Vorlage `android-media-backup.desktop` ist im Projekt enthalten.
Das eigene Icon `android_backup/backup.svg` zeigt ein Smartphone, einen
Sicherungspfeil und ein Archiv. Es wird im Anwendungsmenü und im App-Fenster
verwendet; die Desktop-Zuordnung verbindet das Fenster mit dem Menüeintrag.

Nach der Python-Installation folgende Befehle **im Projektverzeichnis**
ausführen, um den Menüeintrag für den aktuellen Benutzer einzurichten:

```sh
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
install -Dm644 android_backup/backup.svg \
  "$data_home/icons/hicolor/scalable/apps/android-media-backup.svg"
mkdir -p "$data_home/applications"
sed "s|^Exec=.*|Exec=\"$HOME/.local/share/android-media-backup/venv/bin/android-backup\"|" \
  android-media-backup.desktop > "$data_home/applications/android-media-backup.desktop"
update-desktop-database "$data_home/applications"
```

`update-desktop-database` gehört unter Ubuntu zum Paket `desktop-file-utils`.
Nach einer Icon-Änderung die App schließen und neu starten. Wenn das Dock noch
das alte Icon zwischenspeichert, den Favoriten entfernen und erneut hinzufügen.
Auf diesem Rechner ist der Menüeintrag mit diesem Backup-Icon bereits eingerichtet.

## Bedienung

1. Auf dem Smartphone Entwickleroptionen und USB-Debugging aktivieren.
2. Per USB verbinden, entsperren und den Computer für USB-Debugging autorisieren.
3. Anwendung starten. Geräte werden automatisch gesucht und das erste autorisierte
   Gerät ausgewählt. Bei Bedarf **Geräte suchen** oder ein anderes Gerät auswählen.
4. Einen eigenen Backup-Zielordner auswählen.
5. Der Bestand wird automatisch eingelesen. Anzahl, Datenmenge, Speicher und
   Hinweise in der Vorschau prüfen. **Bestand neu einlesen** wiederholt die Erfassung.
6. **Sicherung starten** und Ziel bestätigen.
7. Die Ergebnis-Karte prüfen. Nur „Vollständig im erfassten, zugänglichen Umfang“
   bedeutet, dass alle erfassten Dateien ohne gemeldete Probleme gesichert wurden.

Der Bestätigungsdialog zeigt Umfang, Zielordner, Prüfmodus und Erfassungshinweise
im gleichen Hell-/Dunkeldesign wie das Dashboard. **Sicherung starten** ist die
Standardaktion (Enter); **Abbrechen**, Escape oder Schließen startet keine Sicherung.
Beim Schließen während eines Vorgangs bleibt dagegen **Abbrechen** vorausgewählt,
damit Enter nicht versehentlich die laufende Sicherung beendet. Qt-Standarddialoge
werden mit der mitgelieferten deutschen Übersetzung angezeigt.

Das Dashboard ordnet Gerät, Ziel und Vorschau in Karten. Es folgt automatisch dem
hellen oder dunklen Farbschema des Systems, auch bei einem Wechsel während der
Laufzeit. Die Oberfläche benötigt keine zusätzlichen Design-Pakete.

Solange kein Vorgang läuft, prüft die Anwendung alle drei Sekunden im Hintergrund
die ADB-Geräteliste. Nachträglich angeschlossene Geräte sowie eine nachträglich
bestätigte USB-Debugging-Freigabe werden automatisch erkannt und eingelesen.
Eine unveränderte Geräteauswahl behält ihre Vorschau; das Abziehen des ausgewählten
Geräts verwirft sie. Während einer Bestandsaufnahme oder Sicherung pausiert die
Erkennung. Bei einem Erkennungsfehler erscheint eine Mitteilung; **Geräte suchen**
startet die Erkennung erneut.

Während einer Gerätesuche, Bestandsaufnahme oder Sicherung wird der Startknopf zu
**Abbrechen**. Der Fortschrittsbereich zeigt Phase, Dateizähler und Laufzeit.
Lange Dateinamen werden gekürzt; der vollständige Pfad steht im Tooltip.
Geräteauswahl, Ziel und Prüfmodus sind während eines Vorgangs gesperrt.
Ein Gerätewechsel verwirft die alte Vorschau und liest den neuen Bestand ein.
Nach einer abgeschlossenen Sicherung wird der Bestand automatisch erneut erfasst;
das Ergebnis der Sicherung bleibt dabei sichtbar. Nach Abbruch oder gestopptem
Backup gibt es keinen automatischen neuen Versuch; bei Bedarf **Bestand neu
einlesen** wählen. Eine Sicherung startet nie automatisch.

Die Zielkarte vergleicht freien Speicherplatz mit dem Platzbedarf einer
vollständigen neuen Kopie einschließlich 16 MiB Reserve. Dies ist eine vorsichtige
Schätzung: Bereits vorhandene Dateien können den tatsächlichen Bedarf reduzieren.
Ein wiederhergestellter Zielpfad garantiert nicht, dass ein externer Datenträger
eingehängt ist; den Zielordner vor dem Start prüfen.

Fehler erscheinen als sichtbare Mitteilung statt als blockierender Fehlerdialog.
Hinweise zu fehlender Gerätefreigabe stehen direkt in der Gerätekarte. Details
der Bestandsaufnahme sowie das **Technische Protokoll** sind aufklappbar.
Beim Aufklappen erhält das Protokoll den Tastaturfokus und wird ins Sichtfeld
gescrollt. Beim Einklappen wird die vorherige Scrollposition wiederhergestellt;
der Fokus kehrt zum Ergebnisbereich beziehungsweise zur Bestandsaufnahme zurück.
Während eines laufenden Vorgangs erhält stattdessen der aktive Abbrechen-Button
den Fokus; ohne verfügbare Aktion bleibt der Protokoll-Schalter erreichbar.
**Protokoll kopieren** übernimmt den vollständigen Text mit Zeitstempeln in die
Zwischenablage. **Info** erklärt Umfang, Ausschlüsse und Integritätsprüfung in
einem Dialog mit den gleichen Karten, Farben und Buttons wie das Dashboard.
**Schließen**, Enter oder Escape schließen die Information.

Der zuletzt gewählte Backup-Ordner wird benutzerspezifisch gespeichert und beim
nächsten Start automatisch eingesetzt. Dies gilt auch für manuell eingegebene
Pfade; ein geleertes Zielfeld löscht die gespeicherte Auswahl. Unter Linux liegt
die Einstellung normalerweise in
`~/.config/AndroidMediaBackup/AndroidMediaBackup.conf` (Qt QSettings).
Das Wiederherstellen des Pfades startet keine Sicherung und bindet einen
entfernten Datenträger nicht automatisch ein.

### Fehler und Warnungen im Abschlussbericht

Die Ergebnisanzeige unterscheidet ausdrücklich:

- **Fehler bei Erfassung/Sicherung:** Eine vollständige Sicherung ist nicht
  bestätigt. Fehlgeschlagene und nicht bearbeitete Dateien werden separat gezählt.
- **Metadatenwarnungen:** Die Dateien wurden erfolgreich gesichert, ExifTool
  meldet aber Auffälligkeiten wie ungültige Aufnahmedaten oder Farbprofile.
  Die Anzeige nennt sowohl Meldungsanzahl als auch betroffene Dateianzahl und
  gruppiert die Details pro Datei. Ungültige Aufnahmedaten führen zur bisherigen
  Ersatzregel (Datei-Änderungsdatum, sonst `Unbekannt`).
- **Leere Quelldateien:** Die Datei hatte bereits auf dem Smartphone 0 Byte.
  Ihre erfolgreiche Kopie enthält deshalb keine nutzbaren Bild-/Videodaten.

Die Ergebnis-Karte zeigt neue, bereits vorhandene, fehlgeschlagene und nicht
bearbeitete Dateien als getrennte Kennzahlen. Fehler, leere Quelldateien und
Metadatenwarnungen sind in eigenen aufklappbaren Gruppen aufgelistet.
Prüfmodus und Anzahl ohne erneute Inhaltsprüfung übersprungener Dateien bleiben
auch bei eingeklappten Details sichtbar. Farbe wird immer durch einen
ausgeschriebenen Status ergänzt. **Probleme anzeigen** öffnet eine übersichtliche
Liste mit Meldung, betroffener Datei beziehungsweise Bereich und Auswirkung.
Sie unterscheidet fehlgeschlagene oder nicht bearbeitete Dateien, Erfassungsfehler,
leere Quelldateien und Metadatenwarnungen. Bekannte Warnungen erklären etwa eine
abweichende Jahresablage, mögliche Farbabweichungen oder unlesbare EXIF-Texte.
Unbekannte Meldungen bleiben unverändert sichtbar, ohne eine ungesicherte Ursache
zu behaupten. Auch ohne Probleme lässt sich die Übersicht öffnen; sie bestätigt
dann ausdrücklich nur den erfassten, zugänglichen Umfang.

Bei ausschließlich solchen Warnungen lautet der Status
**„Alle ausgewählten Dateien gesichert – mit Warnungen“**, nicht „fehlgeschlagen“.
Neue JSON-Berichte speichern `errors`, `metadata_warnings` und `empty_files`
getrennt; die bisherige Liste `issues` bleibt für Kompatibilität erhalten.
Bereits vorhandene Dateien werden weiterhin auf Integrität geprüft, ihre
Metadaten werden bei Folgeläufen nicht erneut ausgewertet.

Mit **Bericht öffnen** können auch frühere JSON-Abschlussberichte unter
`.backup` angezeigt werden, ohne die Sicherung zu wiederholen.
Nach der Auswahl wird die Ergebnis-Karte aktualisiert und ihr Anfang mit Status
und Berichtspfad ins Sichtfeld gescrollt. Der Bericht erhält den Tastaturfokus,
auch wenn seine Details höher als das Fenster sind.
Bei älteren Berichten werden bekannte Metadatenmeldungen zugeordnet;
unbekannte Meldungen gelten vorsichtshalber als Fehler. Das Öffnen verändert
weder den Bericht noch die Backup-Dateien.

Während der Bestandsaufnahme werden Dateizähler, bisher erfasste Datenmenge
und der zuletzt erfasste Pfad laufend aktualisiert. Die Gesamtzahl ist noch
unbekannt, deshalb wird keine erfundene Prozentangabe angezeigt.
Eine zusätzliche Statuszeile zeigt Laufzeit und Zeit seit der letzten
Rückmeldung. Ab zehn Sekunden ohne neue Rückmeldung wird das ausdrücklich
angezeigt; eine weiterlaufende Uhr allein bestätigt keinen Datei-Fortschritt.

Bei fehlendem Gerät: `adb devices -l` prüfen. `unauthorized` bedeutet, dass die
Autorisierung auf dem Smartphone noch fehlt. Bei USB-Berechtigungsproblemen
die udev-/Platform-Tools-Einrichtung der Linux-Distribution verwenden; die
Anwendung nicht als root starten.

Eine leere ADB-Geräteliste ergibt auch eine leere Geräteauswahl; das Eingabefeld
zeigt dafür einen Hinweis auf USB-Debugging. Ein USB-Datenkabel verwenden
(reine Ladekabel reichen nicht) und bei Bedarf einen anderen USB-Port testen.
„Geräte suchen“ kann nach dem Verbinden erneut betätigt werden.

## Ablage

```text
Backup/
├── Fotos/
│   ├── 2023/
│   ├── 2024/
│   └── Unbekannt/
├── Videos/
│   ├── 2023/
│   └── Unbekannt/
├── Audio/
├── Dokumente/
├── Archive/
├── Sonstige/
└── .backup/
    ├── index.sqlite3
    ├── lock
    └── report-<ID>.json
```

Ordner werden erst bei Bedarf angelegt. Downloads sind kein eigener Dateityp:
ein heruntergeladenes PDF landet unter `Dokumente`, ein Video unter `Videos`.
Dateiendungen und die von ExifTool erkannte MIME-Art bestimmen den Typ;
unbekannte Dateien kommen nach `Sonstige` und werden nicht verworfen.

Bei Fotos und Videos gilt in dieser Reihenfolge:

1. Gültiges `DateTimeOriginal`, `CreateDate`, `MediaCreateDate` oder `TrackCreateDate`.
2. Ursprüngliches Datei-Änderungsdatum des Smartphones.
3. `Unbekannt`, wenn kein verlässlich bestimmbares Jahr vorliegt.

Ungültige Metadaten werden im Bericht genannt. Akzeptiert werden gültige
Kalenderdaten mit Jahren zwischen 1900 und dem nächsten Kalenderjahr.
Aufnahmedaten werden mit ihrem gespeicherten Kalenderjahr verwendet;
Datei-Änderungsdaten werden in der lokalen Zeitzone des Linux-Rechners ausgewertet.
Es gibt keine Monat-, Tag- oder App-Unterordner.

## Datenintegrität und wiederholte Sicherungen

- Die Quelle wird ausschließlich gelesen. Am Ziel werden keine Nutzdateien gelöscht.
- Jede neue Datei wird zunächst im Ziel unter `.backup/staging-<ID>` übertragen.
- Quellgröße, Änderungszeit und SHA-256 werden vor und nach dem Transfer geprüft;
  die lokale Kopie muss ebenfalls mit der Quellprüfsumme übereinstimmen.
- Sich verändernde Dateien werden einmal erneut versucht und bei erneuter
  Änderung als fehlgeschlagen gemeldet.
- Erst danach wird die Datei mit Linux `renameat2(RENAME_NOREPLACE)` atomar
  veröffentlicht. Bestehende Dateien werden nicht überschrieben. Das Ziel muss
  dieses Verfahren, `fsync` und SQLite-Dateisperren unterstützen; Fehler werden
  gemeldet, nicht durch unsichere Kopierverfahren umgangen.
- Identischer Inhalt wird global einmal abgelegt. Die erste gesicherte Quelle
  bestimmt Dateiname, Typ und Jahresordner; weitere Ursprünge werden dokumentiert.
- Bei gleichen Namen mit unterschiedlichem Inhalt wird ein Prüfsummen-Zusatz
  angehängt. Sehr lange Namen werden verkürzt; der Originalname bleibt im Index.
- Der SQLite-Index enthält Gerät, Originalpfad, Inhaltshash, Größe, ursprüngliche
  Änderungszeit, Zielpfad und Zeitpunkt der ersten/letzten Sichtung. Änderungen
  einer Quelldatei bleiben als zusätzliche Inhaltsversion erhalten.
- Standardmäßig ist der **Schnelllauf** aktiv: Für bekannte Geräte-/Quellpfade
  werden Größe und Änderungszeit direkt auf dem Smartphone neu abgefragt.
  Stimmen sie mit einer gesicherten Version überein und ist die lokale Datei
  vorhanden und gleich groß, wird ohne erneute Inhaltsprüfung übersprungen.
  Dadurch entfallen die vollständigen Smartphone-/Ziel-Lesevorgänge.
  Sichtungszeitpunkte werden für solche Dateien in Gruppen von 100 gespeichert.
- **Grenze des Schnelllaufs:** Inhaltsänderungen oder Beschädigungen bei gleicher
  Größe und Änderungszeit werden nicht erkannt. Die Option **Vollständige
  Integritätsprüfung (langsamer)** aktiviert die bisherige SHA-256-Prüfung auch
  für bekannte Dateien. Regelmäßig und vor dem Löschen von Quelldateien verwenden.
- Neue und geänderte Dateien werden in beiden Modi vollständig geprüft.
  Fehlende oder erkennbar beschädigte indexierte Zieldateien werden gemeldet,
  nicht automatisch ersetzt. Prüfmodus und Zahl der ohne Inhaltsprüfung
  übersprungenen Dateien erscheinen in der App und im JSON-Bericht
  (`verification`, `quick_skipped`). Alte Berichte gelten als vollständig geprüft.
- Ein zweiter Prozess wird für denselben Backup-Zielordner ausgeschlossen.
- Abbruch beendet laufende Kindprozesse und räumt den eigenen temporären Bereich
  auf. Bereits vollständig gesicherte Dateien bleiben erhalten.
- Nach einem Prozessabsturz können temporäre `staging-<ID>`-Ordner zurückbleiben.
  Sie sind kein fertiges Backup. Nur bei geschlossener Anwendung gezielt entfernen.
- JSON-Berichte dokumentieren Ergebnis, Dateizähler und Fehler. SQLite-Index und
  Berichte sollten zusammen mit den Nutzdateien aufbewahrt werden.

Die Vorschau ist eine Bestandsaufnahme, kein eingefrorener Android-Snapshot.
Dateien, die danach neu entstehen, werden erst beim nächsten Einlesen erfasst.
Vor jedem weiteren Sicherungslauf ist deshalb eine neue Bestandsaufnahme nötig.
Die Platzanzeige schätzt konservativ anhand aller Dateien; Dubletten können
den tatsächlich erforderlichen Platz reduzieren.

## Dateiauswahl nach Android-Kategorien

Die Anwendung liest aus dem Android-Medienindex (MediaStore) die Kategorien
**Bilder, Videos, Audio, Dokumente und Downloads**. Dokumente werden anhand
des Index-Dateityps `media_type=6` ausgewählt. Zusätzlich werden nur die
Ordner `Download` und `Downloads` auf den erkannten gemeinsamen Speichern
rekursiv durchsucht, um dort auch noch nicht indexierte Dateien zu erfassen.
Mehrfach vorkommende Quellpfade werden zusammengeführt.

Es gibt keine pauschale rekursive Suche mehr durch WhatsApp oder andere
App-Ordner. Medien aus diesen Ordnern werden aber gesichert, wenn sie im
Android-Index in einer der Kategorien enthalten sind – **auch Audiodateien**.
Die früheren App-Audio-Ausschlüsse sind damit aufgehoben.

Die internen Kategorien von **Files by Google werden nicht exakt reproduziert**.
Der Android-Index kann fehlen, veraltet sein oder andere Dateien umfassen.
Nicht indexierte Medien außerhalb der Download-Ordner werden nicht erfasst.
Noch ausstehende und im Papierkorb liegende Indexeinträge sind ausgeschlossen.
Fehlende Größen oder Änderungszeiten werden direkt an der Datei nachgeprüft;
nicht überprüfbare Einträge erscheinen als Fehler im Bericht. Provider-Fehler
oder nicht eindeutig lesbare Datensätze stoppen die Bestandsaufnahme ausdrücklich.
Insbesondere Dateinamen mit Zeilenumbrüchen sind über die textuelle
MediaStore-Abfrage nicht zuverlässig lesbar; im Download-Scan bleiben sie unterstützt.
Vorschau und JSON-Bericht nennen die verwendete Auswahl und die mögliche
Abweichung von Files by Google.

## Ausschluss technischer App-Daten

Gesichert werden persönliche Dateien, nicht technische App-Bestände:

- `Android/data` und `Android/obb` werden nicht durchsucht, auch auf SD-Karten.
- Verzeichnisse namens `cache`, `.cache`, `code_cache` und `.thumbnails`
  werden unabhängig von Groß-/Kleinschreibung nicht durchsucht.
- Installations- und Laufzeitdateien mit Endungen `.apk`, `.apks`, `.xapk`,
  `.aab`, `.dex`, `.odex` und `.vdex` werden ausgeschlossen.
- App-Audios sind nicht pauschal ausgeschlossen: Entscheidend ist die
  Zugehörigkeit zu den Android-Kategorien oder den Download-Ordnern.

Die Ausschlussregeln greifen bei der Bestandsaufnahme und erneut vor der
Sicherung, auch bei älteren Dateilisten. Vorschau und Abschlussbericht weisen
auf den Ausschluss hin. Der Dateizähler für ausgeschlossene Dateien umfasst nur
einzeln erkannte Dateien, nicht die Inhalte der bewusst nicht durchsuchten Ordner.
Andere technische App-Dateien außerhalb dieser klar definierten Regeln können
weiterhin enthalten sein; eine sichere Erkennung allein aus dem Dateinamen ist
nicht möglich.

Bereits gesicherte App-Dateien werden nicht nachträglich gelöscht. Nach Änderung
der Regeln eine laufende alte Anwendung abbrechen, schließen, neu starten und
den Bestand neu einlesen; laufende Prozesse übernehmen Codeänderungen nicht.

## Zugriffsgrenzen

Verwendet werden Indexeinträge unter `/storage/emulated/0` und zugänglichen
Speichern unter `/storage/????-????` (typischerweise SD-Karten). Nur deren
Download-Ordner werden zusätzlich einschließlich versteckter Dateien durchsucht.
Aufgelöste identische Speicherpfade werden nur einmal berücksichtigt.
Symbolischen Links wird beim Durchsuchen nicht gefolgt.

ADB ohne Root kann keine privaten App-Daten sichern. `Android/data` und
`Android/obb` werden bewusst ausgeschlossen und nicht auf Lesbarkeit geprüft.
Lesefehler in anderen, nicht ausgeschlossenen Bereichen machen den Lauf
ausdrücklich unvollständig. Weitere Android-Nutzer,
Arbeitsprofile, nicht bereitgestellte/adoptierte Speicher und reine Cloud-Inhalte
sind nicht automatisch enthalten. Es handelt sich nicht um ein Systembackup.

Backup-Dateien und Herkunftsindex sind **nicht verschlüsselt**. Für sensible
Inhalte einen verschlüsselten Ziel-Datenträger verwenden. Den Zielordner
während der Sicherung nicht durch andere Programme ändern. Manche Android-Namen
sind auf FAT/exFAT nicht darstellbar; solche Dateifehler werden im Bericht genannt.

## Tests

Mit der installierten Python-Umgebung im Projektverzeichnis:

```sh
~/.local/share/android-media-backup/venv/bin/python -m unittest discover -v
```

Die Tests prüfen Ablage, Dubletten, Inhaltsversionen, Namenskonflikte,
Metadaten, ADB-Shell-Quoting, Sonderzeichen, beschädigte Backups, vollen Speicher,
Parallelzugriff, Abbruch und den GUI-Ablauf mit simuliertem Smartphone.
GUI-Tests verwenden Qt im Offscreen-Modus. Echte ExifTool-Integrationstests laufen
nur, wenn das Programm verfügbar ist; sonst werden sie ausdrücklich übersprungen.
Die GUI-Tests prüfen auch automatisches Einlesen bei Start und Gerätewechsel,
erneute Erfassung nach einer Sicherung, Abbruch ohne automatische Wiederholung,
Inline-Fehler, aufklappbare Ergebnisdetails, Speicherwarnungen und Theme-Wechsel.
Unbehandelte Ausnahmen in Qt-Rückrufen lassen die Tests fehlschlagen. Testfenster,
Hintergrundvorgänge und simulierte Abhängigkeiten werden auch bei einem Fehler im
Testaufbau aufgeräumt. Eigene Tests prüfen diese Fehlererfassung und Aufräumlogik.

Zusätzlich am echten Gerät prüfen: Erkennung des internen Speichers und der
SD-Karte, Bilder/Videos über einen Jahreswechsel, Messenger-Dateien, gesperrte
Verzeichnisse, USB-Trennung während eines Videos und ein unveränderter Folgelauf.
Während einer vollständigen Sicherung das Smartphone möglichst nicht verwenden.
