import unittest
from unittest.mock import patch

from main import _changelog_cache, _format_commit_change, _format_german_date, _humanize_commit_subject, get_changelog_items


class ChangelogTests(unittest.TestCase):
    def test_known_commit_subject_is_humanized(self):
        summary = _humanize_commit_subject("Add automated CI checks")

        self.assertEqual(summary, "Automatische Tests auf GitHub wurden ergänzt.")

    def test_known_keyword_commit_subject_stays_readable(self):
        summary = _humanize_commit_subject("improve dashboard loading")

        self.assertEqual(summary, "Das Dashboard wurde verbessert.")

    def test_unknown_technical_commit_is_hidden(self):
        self.assertEqual(_humanize_commit_subject("Refactor internal helper plumbing"), "")

    def test_german_commit_subject_is_used_as_summary(self):
        self.assertEqual(
            _humanize_commit_subject("Aktive Filter im Dashboard deutlicher anzeigen"),
            "Aktive Filter im Dashboard deutlicher anzeigen.",
        )

    def test_recent_monitor_card_commit_is_humanized(self):
        summary = _humanize_commit_subject("Tighten monitor card height")

        self.assertEqual(summary, "Monitor-Karten wurden kompakter gemacht und bleiben gleichmäßiger hoch.")

    def test_changelog_cache_keeps_enough_items_for_detail_page(self):
        output = "\n".join(
            f"abcde{i}\t21.07.2026 13:4{i}\timprove dashboard change {i}"
            for i in range(8)
        )
        _changelog_cache["items"] = None
        _changelog_cache["expires_at"] = 0

        with patch("main._run_git_command", return_value=output):
            footer_items = get_changelog_items(limit=3)
            detail_items = get_changelog_items(limit=8)

        self.assertEqual(len(footer_items), 3)
        self.assertEqual(len(detail_items), 8)

    def test_commit_change_contains_user_summary(self):
        change = _format_commit_change("5b3d0a8abcdef", "Add automated CI checks", "2026-07-21T12:34:56Z")

        self.assertEqual(change["sha"], "5b3d0a8")
        self.assertEqual(change["summary"], "Automatische Tests auf GitHub wurden ergänzt.")
        self.assertEqual(change["committed_at"], "21.07.2026")

    def test_recent_changelog_subjects_are_humanized(self):
        self.assertEqual(
            _humanize_commit_subject("Move floating filter button to screen edge"),
            "Der schwebende Filterbutton sitzt jetzt platzsparend mittig am rechten Bildschirmrand.",
        )
        self.assertEqual(
            _humanize_commit_subject("Extract system router and dashboard sorting"),
            "Health- und Bereitschaftsprüfungen liegen jetzt in einem eigenen FastAPI-Router; die Karten-Sortierung wurde aus dem HTML-Template in ein geprüftes JavaScript-Modul verschoben.",
        )
        self.assertEqual(
            _humanize_commit_subject("Show active filters as floating button"),
            "Aktive Dashboard-Filter erscheinen jetzt platzsparend als schwebender Button und lassen sich mit einem Klick vollständig löschen.",
        )
        self.assertEqual(
            _humanize_commit_subject("Add architecture boundaries and performance budgets"),
            "Monitor-Zugriffe laufen jetzt über eine klare Repository-Grenze, Datenbankmigrationen werden schrittweise angewendet und automatische Performance-Budgets schützen schnelle Cache- und Frontend-Pfade.",
        )
        self.assertEqual(
            _humanize_commit_subject("Refactor core architecture"),
            "Die Kernlogik ist jetzt klar in Module für Caches, Formatierung, Systemmetriken, API-Modelle und Performance-Messung aufgeteilt. Datenbankänderungen werden versioniert und HTMX wird lokal ausgeliefert.",
        )
        self.assertEqual(
            _humanize_commit_subject("Fix changelog page theme"),
            "Die Änderungsseite nutzt jetzt wieder das dunkle KeepUp-Design.",
        )
        self.assertEqual(
            _humanize_commit_subject("Show changelog during updates"),
            "Während eines Updates werden die enthaltenen Änderungen direkt angezeigt.",
        )
        self.assertEqual(
            _humanize_commit_subject("Add monitor groups and category filters"),
            "Monitore können jetzt in Gruppen/Kategorien organisiert und gefiltert werden.",
        )
        self.assertEqual(
            _humanize_commit_subject("Support multiple monitor groups"),
            "Monitore können jetzt mehreren Gruppen gleichzeitig zugeordnet und über jede dieser Gruppen gefiltert werden.",
        )
        self.assertEqual(
            _humanize_commit_subject("Make detail preloading non-blocking"),
            "Kartendetails werden jetzt gedrosselt im Hintergrund vorgeladen, ohne das Dashboard oder den Raspberry Pi durch einen großen Sammelabruf auszubremsen.",
        )
        self.assertEqual(
            _humanize_commit_subject("Fix card detail loading"),
            "Kartendetails starten beim Öffnen zuverlässig neu, zeigen währenddessen einen Ladebalken und beschränken die Auswertung auf sieben Tage.",
        )
        self.assertEqual(
            _humanize_commit_subject("Speed up initial dashboard loading"),
            "Das Dashboard liefert zuerst eine kompakte Oberfläche aus, lädt Karten nur einmal aus dem Snapshot und verwendet ein deutlich kleineres Logo.",
        )
        self.assertEqual(
            _humanize_commit_subject("Remove first request database stalls"),
            "Der erste Seitenaufruf blockiert nicht mehr an wiederholter SQLite-WAL-Konfiguration oder langsamen Git-Abfragen.",
        )
        self.assertEqual(
            _humanize_commit_subject("Make monitor edits update inline"),
            "Bearbeitete Monitore werden direkt im Dashboard mit Ladeanzeige aktualisiert.",
        )
        self.assertEqual(
            _humanize_commit_subject("Use real category dropdowns"),
            "Kategorie-Felder nutzen echte Dropdowns mit Option für neue Gruppen.",
        )
        self.assertEqual(
            _humanize_commit_subject("Show monitor group badges"),
            "Monitor-Karten zeigen die zugehörige Gruppe deutlicher als Badge an.",
        )
        self.assertEqual(
            _humanize_commit_subject("Fix category validation while monitors wait"),
            "Kategorie-Auswahl und Speichern bleiben auch bei wartenden Monitoren bedienbar.",
        )
        self.assertEqual(
            _humanize_commit_subject("Stabilize editing while monitors refresh"),
            "Das Bearbeiten von Monitoren bleibt stabil, auch wenn andere Karten gerade aktualisieren.",
        )
        self.assertEqual(
            _humanize_commit_subject("Keep monitor edit button clickable during refreshes"),
            "Der Speichern-Button im Monitor-Dialog bleibt auch bei mehreren laufenden Änderungen zuverlässig bedienbar.",
        )
        self.assertEqual(
            _humanize_commit_subject("Align monitor modal field rows"),
            "Felder im Monitor-Dialog sind am Desktop sauberer auf gleicher Höhe ausgerichtet.",
        )
        self.assertEqual(
            _humanize_commit_subject("Preserve dashboard filters after monitor saves"),
            "Dashboard-Filter und Sortierung bleiben nach dem Anlegen oder Speichern von Monitoren erhalten.",
        )
        self.assertEqual(
            _humanize_commit_subject("Avoid blocking live card refreshes after edits"),
            "Live-Karten liefern vorhandene Daten sofort aus, während Aktualisierungen nach Monitoränderungen im Hintergrund laufen.",
        )
        self.assertEqual(
            _humanize_commit_subject("Keep pending monitor category visible during refreshes"),
            "Geänderte Monitor-Kategorien bleiben sofort sichtbar, auch wenn kurz ein älterer Kartenstand zurückkommt.",
        )
        self.assertEqual(
            _humanize_commit_subject("Preserve sort selection during live refreshes"),
            "Die gewählte Sortierung bleibt auch nach Live-Aktualisierungen sichtbar und aktiv.",
        )
        self.assertEqual(
            _humanize_commit_subject("Render incidents feed directly on page load"),
            "Die Incident-Liste wird direkt mit der Seite ausgeliefert und bleibt nicht mehr unnötig im Wartebildschirm hängen.",
        )
        self.assertEqual(
            _humanize_commit_subject("Make live refresh tolerate network changes"),
            "Live-Aktualisierungen reagieren ruhiger auf kurze Netzwerkwechsel.",
        )
        self.assertEqual(
            _humanize_commit_subject("Show monitor forms in compact modals"),
            "Monitor anlegen und bearbeiten öffnet jetzt als kompaktes Overlay ohne Scrollsprung.",
        )
        self.assertEqual(
            _humanize_commit_subject("Compact monitor form layout on desktop"),
            "Monitor-Formulare sind am Desktop deutlich kompakter und passen besser auf eine Bildschirmhöhe.",
        )
        self.assertEqual(
            _humanize_commit_subject("Constrain monitor modal width"),
            "Monitor-Dialoge sind am Desktop schmaler, damit die Felder nicht über die ganze Browserbreite laufen.",
        )
        self.assertEqual(
            _humanize_commit_subject("Tighten first monitor modal fields"),
            "Die ersten Felder im Monitor-Dialog sind am Desktop kürzer und übersichtlicher angeordnet.",
        )
        self.assertEqual(
            _humanize_commit_subject("Improve dashboard sorting and edit position"),
            "Dashboard-Sortierung wurde erweitert und bearbeitete Karten behalten ihre Position ruhiger bei.",
        )
        self.assertEqual(
            _humanize_commit_subject("Enhance Telegram notifications with links and check history"),
            "Telegram-Meldungen enthalten Links und eine kompakte Check-Historie.",
        )

    def test_iso_date_is_formatted_for_german_ui(self):
        self.assertEqual(_format_german_date("2026-07-21"), "21.07.2026")
        self.assertEqual(_format_german_date("2026-07-21T12:34:56Z"), "21.07.2026")


if __name__ == "__main__":
    unittest.main()
