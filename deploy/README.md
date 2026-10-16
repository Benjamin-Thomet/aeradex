# aeradex auf einem eigenen Server

Vorlagen für ein Buch auf einem Linux-Server (Debian/Ubuntu, systemd, Caddy): die Buchhaltung im Browser mit Login,
die Team-App für Mitarbeitende, ein Telegram-Bot, der rund um die Uhr arbeitet, und Claude Code auf den PCs, dessen
aeradex-Werkzeuge über einen eingeschränkten SSH-Zugang direkt auf dem Buch laufen.

```
Browser ───────── https://buch.example.ch ──► aeradex serve (Login)    ┐
Claude Code (PC) ─ ssh, nur «mcp»/«inbox» ──► aeradex mcp              ├─► /srv/aeradex/buch (ein Buch, git)
Telegram ───────── long polling ────────────► aeradex telegram serve   │
Team-Abgleich (Dienst) ◄── localhost ──► Team-App team.example.ch/app ◄── Handys
```

| Datei | Was |
|---|---|
| `server.conf.example` | Domain(s), Benutzer, Pfade, Plugins, Ports; als `/etc/aeradex-server.conf` |
| `setup.sh` | richtet alles ein (idempotent): Benutzer, venv, Buch, Dienste, Caddy, fail2ban, SSH-Zugang |
| `aeradex-update` | Programme neu von `QUELLE`, Dienste neu starten; das Buch bleibt unberührt |
| `systemd/` | `aeradex` (serve), `aeradex-team` (Abgleich), `aeradex-team-portal`, `aeradex-telegram` |
| `caddy/` | Site-Blöcke für das Buch (mit den Pfaden des Anruf-Gateways) und die Team-App |
| `fail2ban/` | Filter für Anmeldungen an aeradex und an der Team-App |

## Einrichten

```bash
git clone https://github.com/Benjamin-Thomet/aeradex && cd aeradex
sudo cp deploy/server.conf.example /etc/aeradex-server.conf && sudo chmod 600 /etc/aeradex-server.conf
sudo nano /etc/aeradex-server.conf          # DOMAIN, PORTAL_DOMAIN, FIRMA, PLUGINS …
sudo bash deploy/setup.sh
```

Das Skript gibt am Ende aus, was noch von Hand zu tun ist: DNS, Logins (`aeradex user add …`), Agent und Schlüssel
in aeradex eintragen. Schlüssel (Claude API, OpenAI-kompatibles Modell, Telegram, ElevenLabs) gehören nie in die
Konfiguration oder ins Buch; aeradex schreibt sie nach `BASIS/config`, das nur der Dienst lesen kann.

Ein bestehendes Buch vorher als `BASIS/buch` hinlegen (z.B. `aeradex import-buch firma.aeradex /srv/aeradex/buch`,
Besitzer `BENUTZER`); `setup.sh` legt nur ein neues an, wenn dort keins ist.

Updates: `sudo aeradex-update`.

## Was wo läuft

- **Buch**: `aeradex serve --https` auf `127.0.0.1:BUCH_PORT` hinter Caddy, Logins in `BASIS/config`
  ([docs/server.md](../docs/server.md)). Jede Änderung ist ein geprüfter git-Commit mit dem Namen der Person.
- **Team-App**: eigenes Paket und eigener Benutzer (`aeradex-portal`), Daten in `/var/lib/aeradex-team-portal`.
  Der Abgleich (`aeradex team sync --wiederholen 300 --pruefen 15`) läuft beim Buch und spricht das Portal über
  localhost an. Auf einer bestehenden Website statt einer eigenen Subdomain: den `handle /app*`-Block aus
  `caddy/portal.caddy` in deren Site-Block setzen. Eigenes Logo und Farben: `PORTAL_BRANDING`
  ([aeradex-team-portal](../plugins/aeradex-team-portal)).
- **Telegram/Anruf** ([aeradex-anruf](../plugins/aeradex-anruf), [aeradex-telegram](../plugins/aeradex-telegram)):
  Grundeinstellungen in `BASIS/config/bot.env`, Zugänge auf ihrer Seite unter Plugins (Name der Sekretärin,
  Standard «Susi») › Zugänge. Auf dem Server ohne Claude-Login: `AERADEX_ANRUF_BACKEND=api` (Claude API) oder
  `openai` (z.B. Gemini).
- **Claude Code auf den PCs** ([aeradex-zugriff](../plugins/aeradex-zugriff)): `/usr/local/sbin/aeradex-ssh` als
  Forced command, Pfade aus `/etc/aeradex-ssh.conf`. Schlüssel trägt die Inhaberin in aeradex ein; dafür braucht
  niemand Admin-Zugang zum Server.

## Sicherung

Das Buch ist ein git-Repository: `git bundle create - --all` (als `BENUTZER`) ergibt eine konsistente Kopie, dazu
die Dateien ausserhalb von git (`inbox/`, `.aeradex/lokal/`) und `BASIS/config`. Die Team-App: `sqlite3
/var/lib/aeradex-team-portal/portal.db ".backup …"` und `/etc/aeradex-team-portal`.
