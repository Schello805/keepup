# KeepUp

[![CI](https://github.com/Schello805/keepup/actions/workflows/ci.yml/badge.svg)](https://github.com/Schello805/keepup/actions/workflows/ci.yml)

KeepUp ist eine leichtgewichtige, mobile-optimierte Self-Hosted-Monitoring-App für den lokalen Betrieb. Sie überwacht HTTP(S)-Ziele sowie Hostnamen/IPs per Ping und verschickt bei Statusänderungen Benachrichtigungen über Telegram oder SMTP.
Das gesamte Projekt wurde mittels VibeCoding erstellt und gepflegt. Ich freue mich über Feedback von Usern und / oder professionellen Entwicklern. 

Die App setzt auf:

- `FastAPI` für Backend und UI-Routing
- `SQLite` als portable lokale Datenbank
- `APScheduler` für zeitgesteuerte Checks
- `httpx` für asynchrone HTTP-Checks
- `Tailwind CSS` und `HTMX` für die Weboberfläche

## Funktionen

- HTTP/S- und Ping-Monitore
- Kombinierte PING-/HTTP/S-Monitore mit getrenntem Ping-Ziel
- Modus `PING oder HTTP`: DOWN wird erst gemeldet, wenn beide Prüfwege fehlschlagen
- Modus `PING + HTTP`: DOWN wird gemeldet, sobald einer der beiden Prüfwege fehlschlägt
- Benachrichtigungen bei Statusänderungen per Telegram und SMTP
- Bestätigte DOWN-/UP-Logik mit Schwellwerten
- Flapping-Erkennung
- Incident-Timeline
- Kompakter JSON-Export und -Import für Konfiguration und aktuelle Historie
- Mobile-First-Dashboard mit Live-Updates
- Separate Einstellungsseite
- Testversand für Telegram und SMTP
- In-App-Update-Prüfung
- Automatische GitHub-CI für Python-Checks, Unit-Tests, Dependency-Checks und CSS-Build

## Intervall-Logik

KeepUp unterstützt jetzt drei Ebenen für Prüfintervalle:

- Pro Monitor:
  Jeder Monitor hat weiterhin sein eigenes Feld `Intervall (Sekunden)`.
- Standard-Intervall für neue Monitore:
  In den Einstellungen unter `Allgemein` kannst du festlegen, welcher Wert beim Anlegen neuer Monitore vorausgefüllt wird.
- Globales Intervall für alle Monitore:
  Ebenfalls unter `Allgemein` kannst du optional ein zentrales Override setzen.

Wichtig:

- `0` beim globalen Intervall bedeutet: kein Override
- ab `10` Sekunden erzwingt das globale Intervall denselben Takt für alle aktiven Monitore
- auf den Monitor-Karten wird immer das aktuell effektive Intervall angezeigt

## Projektstruktur

- `main.py`
  FastAPI-App, Routen, HTML-Rendering, Settings-Logik
- `monitor.py`
  Check-Logik, Scheduler-Anbindung, Notifications
- `database.py`
  SQLite-Schema, CRUD, Incident- und Backup-Funktionen
- `templates/`
  Jinja2-Templates für Dashboard, Settings und Incidents
- `static/`
  Statische Assets wie Logo/Favicon
- `scripts/`
  Install-, Update- und Service-Hilfsskripte

## Lokale Entwicklung

Virtuelle Umgebung anlegen und Abhängigkeiten installieren:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install
```

Frontend-CSS lokal bauen:

```bash
npm run build:css
```

App lokal starten:

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Danach ist die Oberfläche unter [http://127.0.0.1:8000](http://127.0.0.1:8000) erreichbar.

## Produktiver Betrieb per systemd

Für Debian-/Ubuntu-ähnliche Systeme liegen Hilfsskripte im Repo:

- `scripts/install_keepup.sh`
  Erstinstallation
- `scripts/check_and_configure.sh`
  Prüft Umgebung und schreibt/aktualisiert die `systemd`-Unit
- `scripts/update_keepup.sh`
  Holt Updates und führt die Konfigurationsprüfung erneut aus

Beispiel:

```bash
sudo ./scripts/install_keepup.sh
```

Danach den Service prüfen:

```bash
sudo systemctl status keepup.service
sudo journalctl -u keepup.service -f
```

## Betrieb ohne systemd

Für einen einfachen Testbetrieb:

```bash
source .venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

Oder im Hintergrund mit `tmux` bzw. `nohup`.

## Backup und Restore

In den Einstellungen kannst du:

- die komplette Konfiguration als JSON exportieren
- eine JSON-Datei wieder importieren

Der Export enthält:

- Monitore
- Einstellungen
- Checks der letzten 24 Stunden
- Incidents der letzten 24 Stunden sowie weiterhin offene Incidents

Telegram Bot Token und SMTP-Passwort werden aus Sicherheitsgründen nicht in den JSON-Export geschrieben. Nach einer Migration musst du diese beiden Felder in den Einstellungen neu eintragen.

Dadurch bleiben Backups auch bei vielen Monitoren handlich. Beim Import wird dieselbe Begrenzung auf ältere Backups angewandt. Zusätzlich akzeptiert KeepUp standardmäßig maximal 25 MB große Importdateien. Bei Bedarf kannst du den Wert über `KEEPUP_MAX_IMPORT_MB` anpassen.

## Benachrichtigungen

Telegram und SMTP werden nur bei echten Statuswechseln ausgelöst, nicht bei jedem einzelnen Check.

Zusätzlich gibt es:

- Testversand für Telegram
- Testversand für SMTP
- Sammelmeldungs-Fenster für mehrere Statusänderungen

## Raspberry Pi

KeepUp läuft grundsätzlich auch auf einem Raspberry Pi, besonders gut für lokale Heimnetz-Setups. Je schwächer die Hardware, desto wichtiger werden sinnvolle Intervalle und knappe Timeouts.

### Raspberry Pi 3B

Praxisnahe Empfehlung:

- etwa `10 bis 30` Monitore
- bevorzugt `60s` Intervall oder mehr
- Dashboard-Refresh eher `15s` bis `30s`
- Timeout eher `3s` bis `5s`
- `Retries` eher `0` oder `1`
- bei HTTP möglichst `HEAD` statt `GET`

Weniger empfehlenswert auf einem Pi 3B:

- viele Monitore mit `10s`-Intervallen
- viele hängende HTTP-Ziele gleichzeitig
- sehr große Historie plus aggressive Live-Refresh-Werte

### Raspberry Pi 4 / 5

Auf einem Pi 4 oder Pi 5 ist KeepUp deutlich entspannter zu betreiben. Dort sind auch größere Setups realistisch, zum Beispiel:

- `30 bis 60+` Monitore
- oft problemlos `30s` bis `60s` Intervall
- UI-Refresh meist `10s` bis `15s`

### Allgemeine Empfehlung für Raspberry Pi

Für einen stabilen Betrieb auf Pi-Systemen:

- gute SD-Karte verwenden, besser A2-Klasse
- wenn möglich SSD statt SD-Karte nutzen
- HTTP-Timeouts knapp halten
- globale Intervalle nicht unnötig aggressiv setzen
- Incident- und Check-Historie regelmäßig bereinigen lassen

Ein guter Startwert für einen Pi 3B ist zum Beispiel:

- `20` Monitore
- `60s` Prüfintervall
- `15s` Dashboard-Refresh
- `4s` Timeout
- `1` Retry
- `HEAD` für einfache Web-Erreichbarkeitschecks

## Performance-Hinweise

KeepUp ist für lokale Self-Hosted-Umgebungen optimiert. Für gute Reaktionszeiten bei vielen Monitoren gelten diese Empfehlungen:

- Intervall pro Monitor eher `60s` oder mehr
- `HEAD` statt `GET`, wenn möglich
- `Retries` niedrig halten
- Timeout eher knapp setzen, z. B. `3s` bis `5s`
- auf schwacher Hardware lieber weniger aggressive Live-Refresh-Intervalle nutzen

## Troubleshooting

- Service startet nicht:
  `sudo journalctl -u keepup.service -n 200`
- UI zeigt alte Version:
  Dienst wirklich neu starten, nicht nur den Browser reloaden
- Permission-Probleme:
  Besitzrechte des Projektverzeichnisses und der Datenbank prüfen
- Import-/Update-Probleme:
  Logs des Services und Browser-Konsole prüfen
- Backup zu groß:
  KeepUp exportiert nur 24 Stunden Check-/Incident-Historie. Wenn du ältere Fremd- oder Legacy-Backups importierst, begrenze die Datei oder erhöhe `KEEPUP_MAX_IMPORT_MB` bewusst.

## Screenshot

<img width="1238" height="787" alt="KeepUp Screenshot" src="https://github.com/user-attachments/assets/7ec23fc3-0afb-4380-becc-e7786fe682e0" />

<img width="1239" height="866" alt="Bildschirmfoto 2026-06-03 um 19 09 58" src="https://github.com/user-attachments/assets/5de61e8d-37ca-42a8-994f-230c5dd9e051" />

<img width="1231" height="890" alt="Bildschirmfoto 2026-06-03 um 19 10 15" src="https://github.com/user-attachments/assets/4ca0e012-04b3-492c-bcc1-1c4035362d35" />

<img width="1259" height="574" alt="Bildschirmfoto 2026-06-03 um 19 10 47" src="https://github.com/user-attachments/assets/b695cd33-5061-4032-b37b-ba987483dd61" />
