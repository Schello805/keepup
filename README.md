# KeepUp

KeepUp ist eine leichtgewichtige, mobile-optimierte Self-Hosted-Monitoring-App für den lokalen Betrieb. Sie überwacht HTTP(S)-Ziele sowie Hostnamen/IPs per Ping und verschickt bei Statusänderungen Benachrichtigungen über Telegram oder SMTP.

Die App setzt auf:

- `FastAPI` für Backend und UI-Routing
- `SQLite` als portable lokale Datenbank
- `APScheduler` für zeitgesteuerte Checks
- `httpx` für asynchrone HTTP-Checks
- `Tailwind CSS` und `HTMX` für die Weboberfläche

## Funktionen

- HTTP/S- und Ping-Monitore
- Benachrichtigungen bei Statusänderungen per Telegram und SMTP
- Bestätigte DOWN-/UP-Logik mit Schwellwerten
- Flapping-Erkennung
- Incident-Timeline
- JSON-Export und -Import für komplettes Backup / Restore
- Mobile-First-Dashboard mit Live-Updates
- Separate Einstellungsseite
- Testversand für Telegram und SMTP
- In-App-Update-Prüfung

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
- Checks
- Incidents
- Einstellungen

## Benachrichtigungen

Telegram und SMTP werden nur bei echten Statuswechseln ausgelöst, nicht bei jedem einzelnen Check.

Zusätzlich gibt es:

- Testversand für Telegram
- Testversand für SMTP
- Sammelmeldungs-Fenster für mehrere Statusänderungen

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

## Screenshot

<img width="1238" height="787" alt="KeepUp Screenshot" src="https://github.com/user-attachments/assets/7ec23fc3-0afb-4380-becc-e7786fe682e0" />
