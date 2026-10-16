# aeradex-team-portal

Die Seite der Mitarbeitenden für [aeradex-team](../aeradex-team): eine Handy-App (PWA, offline-fähig) und die
Sync-API für das Buch. Starlette und SQLite, ohne aeradex-Abhängigkeit: das Portal kennt keine Buchhaltung, es ist
Briefkasten (Einträge der Handys) und Abbild (was das Buch jeder Person zeigt). Alles liegt unter `/app`.

```bash
pip install aeradex-team-portal            # oder: pip install -e plugins/aeradex-team-portal
aeradex-team-portal secret                 # → AERADEX_PORTAL_SECRET=…
aeradex-team-portal sync-token             # Token fürs Buch + Zeile AERADEX_PORTAL_SYNC_TOKEN_SHA256=…
aeradex-team-portal serve --port 4100      # hinter Caddy/nginx mit https
```

| Umgebung | |
|---|---|
| `AERADEX_PORTAL_SECRET` | Sitzungen signieren (mind. 32 Zeichen), Pflicht |
| `AERADEX_PORTAL_SYNC_TOKEN_SHA256` | SHA-256 des Sync-Tokens; das Token selbst kennt nur das Buch |
| `AERADEX_PORTAL_DATA` | Datenordner (Standard `/var/lib/aeradex-team-portal`) |
| `AERADEX_PORTAL_KENNUNG` | ein Wort für diese Installation (Standard `aeradex`): Cookie, Header, Speicher auf dem Handy, Salz der Schlüssel. **Nach dem Start nie ändern**: sonst sind alle abgemeldet und gespeicherte Dokumente nicht mehr lesbar |
| `AERADEX_PORTAL_TITEL` | Name der App, z.B. «Team · Muster AG» |
| `AERADEX_PORTAL_FARBE` | Farbe der Kopfzeile und Statusleiste |
| `AERADEX_PORTAL_BRANDING` | Ordner mit eigenem `logo.png`, `icon-192.png`, `icon-512.png`, `branding.css` (überschreibt die Farb-Token aus `app.css`, kann Schriften laden; weitere `.woff2`/`.png`/`.svg` liegen unter `/app/branding/…`) |
| `AERADEX_PORTAL_INSECURE_COOKIE=1` | nur lokal ohne https |

Sicherheit: Passwörter verlassen das Handy nie (Anmeldung mit einem daraus abgeleiteten Schlüssel, scrypt auf dem
Server), Schreibzugriffe verlangen einen eigenen Header (CSRF), Anmeldeversuche sind gedrosselt und für fail2ban
protokolliert, strenge CSP, `noindex`. Lohndokumente sind mit dem Schlüssel der Person verschlüsselt; das Portal
kann sie nicht lesen. Details: [PROTOCOL.md](../aeradex-team/PROTOCOL.md).

Tests: `pip install -e "plugins/aeradex-team-portal[dev]" && pytest plugins/aeradex-team-portal`
