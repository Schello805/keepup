# KeepUp

KeepUp ist eine leichtgewichtige, mobile-optimierte Self-Hosted-Monitoring-App mit FastAPI, SQLite, APScheduler und einer Tailwind-UI ohne Build-Tooling. Überwacht werden HTTP/S-Ziele sowie IP-Adressen oder Hostnamen per Ping. Bei Statusänderungen können Telegram- und SMTP-Benachrichtigungen ausgelöst werden.

## Funktionen

- Asynchrone HTTP/S-Checks mit `httpx.AsyncClient`
- Asynchrone Ping-Checks über `asyncio.create_subprocess_exec`
- SQLite als portable lokale Datenbank
- Mobile-First-Dashboard mit Tailwind CSS per CDN und HTMX-Live-Updates
- Separate Einstellungsseite für Telegram-, SMTP- und globale App-Einstellungen
# KeepUp

KeepUp ist eine leichtgewichtige, mobile-optimierte Self-Hosted‑Monitoring‑App (lokale Nutzung) mit FastAPI, SQLite und APScheduler. Überwacht werden HTTP(S)‑Ziele sowie Hostnamen/IPs per Ping. Bei Statusänderungen können Telegram‑ und SMTP‑Benachrichtigungen verschickt werden.

## Wichtiger Hinweis

Dieses Projekt ist für den lokalen Betrieb gedacht — es läuft also normalerweise nur auf einem einzelnen Host (z. B. einer LXC‑Instanz auf deinem Server). Die folgenden Schritte beschreiben, wie du es lokal installierst, als systemd‑Service betreibst und Updates automatisiert prüfst.

## Projektstruktur (Kurz)

- `main.py`: FastAPI‑App, Routen, UI‑Rendering
- `monitor.py`: Check‑Logik, Scheduler, Notifications
- `database.py`: SQLite‑Schema, CRUD, Backup/Restore
- `templates/`, `static/`: UI‑Dateien
- `scripts/`: Hilfs‑Skripte (`install_keepup.sh`, `update_keepup.sh`, `check_and_configure.sh`)

## Lokale Entwicklung / Schneller Start

1. Virtuelle Umgebung erstellen und aktivieren:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. App lokal starten (nur lokal, Entwicklung):

```bash
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

Die Oberfläche ist dann unter `http://localhost:8000` erreichbar.

## Automatisierte Installation (Debian LXC) — empfohlen

Die im Repo enthaltenen Skripte automatisieren Einrichtung und Betrieb:

- `scripts/install_keepup.sh` — Initialinstallation: legt System‑User `keepup` an, erstellt venv, installiert Abhängigkeiten, setzt Rechte und ruft die Konfig‑Prüfung auf.
- `scripts/check_and_configure.sh` — Prüft/erstellt venv, installiert Abhängigkeiten, legt die `systemd`‑Unit `/etc/systemd/system/keepup.service` an/aktualisiert sie, aktiviert & startet den Service.
- `scripts/update_keepup.sh` — Zieht ggf. Git‑Updates, installiert Abhängigkeiten und führt `check_and_configure.sh` aus.

Beispiel: Erstinstallation (im Projektverzeichnis):

```bash
sudo ./scripts/install_keepup.sh
```

Updates ausführen / prüfen:

```bash
./scripts/update_keepup.sh
```

## systemd Service

Die Skripte legen eine systemd‑Unit `/etc/systemd/system/keepup.service` an. Die Unit startet `uvicorn` aus der Projekt‑`venv` als Benutzer `keepup`.

Service managen:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now keepup.service
sudo systemctl status keepup.service
sudo journalctl -u keepup.service -f
```

Wenn du spezielle Umgebungsvariablen (z. B. `KEEPUP_UPDATE_TOKEN`) benötigst, kannst du sie in `scripts/check_and_configure.sh` in die Unit oder in eine `/etc/default/keepup`‑Datei einfügen.

## Hinweise zur Konfiguration

- Standardmäßig wird ein System‑User `keepup` angelegt und das Projektverzeichnis diesem User zugewiesen. Wenn du einen anderen Benutzer verwenden willst, passe `scripts/install_keepup.sh` / `scripts/check_and_configure.sh` entsprechend an.
- Die Skripte verwenden `sudo` für Operationen, die Root‑Rechte benötigen (User anlegen, systemd‑Unit schreiben, service starten).

## Lokaler Betrieb ohne systemd

Wenn du nur temporär testen willst, kannst du `tmux` oder `nohup` verwenden:

```bash
# Mit tmux (empfohlen für interaktives Arbeiten)
tmux new -s keepup
# dann im tmux: source .venv/bin/activate && uvicorn main:app --host 0.0.0.0 --port 8000

# Mit nohup (einfacher Hintergrundprozess, kein Restart)
nohup ./.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > keepup.log 2>&1 &
```

## Update-/Rollback‑Hinweise

- `./scripts/update_keepup.sh` führt nach Pull/Install eine Konfig‑Prüfung aus und versucht, den service/Setup zu korrigieren, falls nötig.
- Backup vor größeren Änderungen: nutze den JSON‑Export in der UI oder sichere `keepup.db`.

## Kurzer Troubleshooting‑Abschnitt

- Service startet nicht: `sudo journalctl -u keepup.service -n 200` zeigt die letzten Logs.
- Permission‑Fehler: stelle sicher, dass Dateien dem `keepup`‑User gehören (oder passe den User an).
- Virtualenv fehlt/abhängigkeiten: `sudo ./scripts/check_and_configure.sh` nochmals ausführen.

---

Wenn du willst, passe ich die README noch an (z. B. Beispiel für `KEEPUP_UPDATE_TOKEN` in der Unit oder Hinweise für Reverse‑Proxy/nginx). 
journalctl -u keepup -f

```
