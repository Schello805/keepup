# KeepUp

KeepUp ist eine leichtgewichtige, mobile-optimierte Self-Hosted-Monitoring-App mit FastAPI, SQLite, APScheduler und einer Tailwind-UI ohne Build-Tooling. Überwacht werden HTTP/S-Ziele sowie IP-Adressen oder Hostnamen per Ping. Bei Statusänderungen können Telegram- und SMTP-Benachrichtigungen ausgelöst werden.

## Funktionen

- Asynchrone HTTP/S-Checks mit `httpx.AsyncClient`
- Asynchrone Ping-Checks über `asyncio.create_subprocess_exec`
- SQLite als portable lokale Datenbank
- Mobile-First-Dashboard mit Tailwind CSS per CDN und HTMX-Live-Updates
- Separate Einstellungsseite für Telegram-, SMTP- und globale App-Einstellungen
- Uptime-Historie als kompakte Balkenanzeige
- Klappbare technische Fehlerlogs pro Monitor
- Monitore direkt im Dashboard bearbeiten sowie pausieren/fortsetzen
- Sofortiger Erst-Check nach dem Anlegen eines Monitors
- Testversand für Telegram und SMTP direkt aus den Einstellungen
- Zeitzonen-Auswahl für UI, Logs und Benachrichtigungen
- Klickbare Summary-Karten mit Status-Filter für die Monitor-Ansicht
- JSON-Export und JSON-Import für Migration und Backup
- Telegram- und SMTP-Benachrichtigungen bei Down/Recovery

## Projektstruktur

- `main.py`: FastAPI-App, Routen, UI-Rendering, Import/Export
- `monitor.py`: Check-Logik, Scheduler-Registrierung, Notifications
- `database.py`: SQLite-Schema, CRUD, Historie, Settings, Backup/Restore
- `templates/index.html`: Mobile-optimiertes Dashboard via Jinja2 + Tailwind CDN
- `templates/settings.html`: Separate Einstellungsseite für Benachrichtigungen
- `templates/_shared.html`: Gemeinsamer Header für Dashboard und Einstellungen
- `static/logo.png`: Logo und Favicon
- `requirements.txt`: Pip-Abhängigkeiten

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Danach ist die App unter [http://localhost:8000](http://localhost:8000) erreichbar.

## Bedienung

- Monitore können direkt im Dashboard angelegt, bearbeitet, pausiert, manuell geprüft oder gelöscht werden.
- Der erste Check eines neuen Monitors läuft sofort nach dem Anlegen.
- Die Summary-Karten im oberen Bereich filtern die darunterliegenden Monitor-Karten nach Status.
- Detailansichten bleiben auch bei Live-Updates offen.
- Telegram- und SMTP-Tests können auf der Einstellungsseite sofort ausgelöst werden.

## Hinweise zu Checks

- HTTP/S-Monitore erwarten eine Antwort kleiner als HTTP 400.
- Ping-Monitore nutzen den lokalen `ping`-Befehl des Hosts.
- Direkt nach dem Start werden alle aktiven Monitore einmal asynchron geprüft, danach übernimmt APScheduler die Intervalle.
- Benachrichtigungen werden nur bei Statuswechseln versendet, nicht bei jedem einzelnen Check.

## JSON-Backup und Migration

- Auf der Einstellungsseite kann die komplette Konfiguration inklusive Monitore, Historie und Benachrichtigungseinstellungen als JSON exportiert werden.
- Der Import ersetzt die bestehende Datenbank-Konfiguration vollständig und plant anschließend alle Jobs neu ein.

## Entwicklung

Syntax-Check:

```bash
PYTHONPYCACHEPREFIX=/tmp/keepup-pyc python3 -m py_compile main.py database.py monitor.py
```

Lokaler Start für Entwicklung:

```bash
./venv/bin/uvicorn main:app --host 127.0.0.1 --port 8123
```

## Start als Hintergrund-Prozess mit systemd

Beispiel für `/etc/systemd/system/keepup.service`:

```ini
[Unit]
Description=KeepUp Monitoring Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=keepup
Group=keepup
WorkingDirectory=/opt/keepup
ExecStart=/opt/keepup/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Service aktivieren:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now keepup
sudo systemctl status keepup
```

Logs ansehen:

```bash
journalctl -u keepup -f
```
