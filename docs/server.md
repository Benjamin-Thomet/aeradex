# aeradex auf einem Server betreiben

`aeradex ui` ist für den eigenen Rechner gedacht (ein Benutzer, Zugangsschlüssel im Link).
`aeradex serve` ist der Mehrbenutzer-Betrieb mit Login: für einen eigenen Server, erreichbar über HTTPS.

Fertige Vorlagen für alles zusammen (systemd, Caddy, fail2ban, Team-App für Mitarbeitende, Telegram-Bot, Zugriff für
Claude Code per SSH) und ein Einrichtungsskript: [deploy/](../deploy). Diese Seite erklärt die Teile einzeln.

## Grundsätze

- **Ein Buch pro Instanz.** Für mehrere Mandanten mehrere Instanzen auf verschiedenen Ports bzw. Subdomains
  (z.B. `muster.buchhaltung.example.ch`), je mit eigenem Buch und eigener Benutzerliste.
- **HTTPS macht der Reverse Proxy** (Caddy oder nginx); aeradex selbst hört nur auf `127.0.0.1`.
- **Benutzer liegen nicht im Buch.** `users.yaml` und `secret` stehen im Konfigurationsverzeichnis
  (Standard `~/.config/aeradex`, sonst `--config DIR` bzw. `$AERADEX_CONFIG`), Dateirechte 600.
- **Jede Änderung trägt den Namen der angemeldeten Person** im git-Verlauf; Änderungen des Agenten sind als Agent markiert.

## Rollen

| Rolle | darf |
|---|---|
| `lesen` | alles ansehen, Berichte und PDFs öffnen |
| `buchhaltung` | buchen, Rechnungen, Kreditoren, Bank, Lohn, MWST, Agent |
| `admin` | zusätzlich Einstellungen ändern und Perioden sperren/entsperren |

## Einrichten

```bash
# als eigener Systembenutzer, z.B. "aeradex"
python3 -m venv ~/venv && ~/venv/bin/pip install "aeradex[ui,scan] @ git+https://github.com/…/aeradex"
git clone <repo-des-buchs> ~/buecher/muster       # oder: aeradex init ~/buecher/muster --firma …
~/venv/bin/aeradex user add anna --rolle admin --anzeige "Anna Muster"
~/venv/bin/aeradex user add treuhand --rolle lesen --anzeige "Treuhand Muster"
~/venv/bin/aeradex --buch ~/buecher/muster serve --port 8080 --https
```

Weitere Befehle: `aeradex user list`, `aeradex user passwort NAME` (beendet bestehende Anmeldungen),
`aeradex user rolle NAME ROLLE`, `aeradex user remove NAME`,
`aeradex user einladen --rolle lesen --notiz "Lea Muster" --url https://buch.example.ch` (druckt einen Einladungslink).
Nach 5 Fehlversuchen ist ein Benutzer bzw. eine Adresse 5 Minuten gesperrt.

Im Browser: **Benutzer** (im Firmenmenü oben rechts, oder Einstellungen › Benutzer) zeigt alle Logins. Jede Person
kann jemanden einladen, höchstens mit der eigenen Rolle; aeradex verschickt keine E-Mails, sondern zeigt einen Link
(7 Tage gültig, einmal verwendbar), den man per WhatsApp, SMS oder persönlich weitergibt. Wer ihn öffnet, wählt
Benutzername und Passwort. Admins ändern Rollen, entfernen Logins und erstellen Passwort-Links (statt «Passwort
vergessen»). **Mein Konto** ändert Anzeigename und Passwort. Was mit den Benutzern geschah, steht in
`benutzer-log.jsonl` im Konfigurationsverzeichnis und auf der Seite Benutzer; was jemand im Buch tat, im Verlauf
(git) und im Journal in der Spalte «Von».

### systemd

`/etc/systemd/system/aeradex-muster.service`:

```ini
[Unit]
Description=aeradex Buchhaltung Muster GmbH
After=network.target

[Service]
User=aeradex
WorkingDirectory=/home/aeradex/buecher/muster
Environment=AERADEX_CONFIG=/home/aeradex/.config/aeradex-muster
ExecStart=/home/aeradex/venv/bin/aeradex --buch /home/aeradex/buecher/muster serve --port 8080 --https
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/aeradex/buecher/muster /home/aeradex/.config/aeradex-muster

[Install]
WantedBy=multi-user.target
```

### Monatsbericht automatisch (Timer)

`/etc/systemd/system/aeradex-muster-monatsbericht.service` und `.timer` — am 5. jedes Monats der Bericht des
Vormonats, per E-Mail an `mail.an` aus `aeradex.yaml`:

```ini
# .service
[Service]
Type=oneshot
User=aeradex
Environment=AERADEX_SMTP_PASSWORD=…
ExecStart=/home/aeradex/venv/bin/aeradex --buch /home/aeradex/buecher/muster bericht monat --mail

# .timer
[Timer]
OnCalendar=*-*-05 06:00
Persistent=true
[Install]
WantedBy=timers.target
```

Ohne systemd genügt cron: `0 6 5 * * aeradex --buch … bericht monat --mail`. Das Passwort steht nie im Buch.

### Caddy (HTTPS automatisch)

```
muster.buchhaltung.example.ch {
    reverse_proxy 127.0.0.1:8080
}
```

nginx: `proxy_pass http://127.0.0.1:8080;` mit `proxy_set_header Host $host; proxy_set_header X-Forwarded-Proto $scheme;
proxy_set_header X-Forwarded-For $remote_addr;` und für die Live-Aktualisierung `proxy_buffering off;`.

## Datensicherung

Das Buch ist ein git-Repository. Eine zweite Kopie ausserhalb des Servers entsteht mit einem Remote:

```bash
cd ~/buecher/muster && git remote add backup <ssh-url> && git push backup HEAD
# täglich per cron/systemd-timer: git -C ~/buecher/muster push -q backup HEAD
```

GeBüV verlangt die Aufbewahrung über 10 Jahre; der git-Verlauf belegt zusätzlich, wer wann was geändert hat.

## Der Agent auf dem Server

Der Agent im Seitenpanel nutzt auf dem Server entweder `ANTHROPIC_API_KEY` (in der systemd-Unit als
`Environment=` bzw. `EnvironmentFile=`) oder ein dort angemeldetes Claude Code. Benutzer mit Leserecht
können ihn nicht verwenden. Im Modus `vorschlag` legt er nur Vorschläge an, die eine Person freigibt.

## Handy-Ansicht (`/m`)

Wer sich mit dem Handy anmeldet, landet in einer schlanken Ansicht mit Tab-Leiste unten: **Heute** (Nachfassen,
überfällige Rechnungen, Belege zum Prüfen, Bankstand), **Kunden** (das ganze Dossier wie am Desktop; Anrufen,
E-Mail, Navigieren — Apple Karten auf dem iPhone, sonst Google Maps), **Scannen**, **Rechnungen** (Offen, Überfällig,
Gemahnt, Entwürfe, Bezahlt, Alle) und **Agent**. Erkannt wird das Handy am Browser; im Menü «Desktop-Ansicht» bzw. im
Firmenmenü am Desktop «Handy-Ansicht» wechselt man, die Wahl gilt auf diesem Gerät (Cookie `ansicht`).

- **Beleg scannen:** Foto aufnehmen, aeradex sucht die Ränder des Blatts, die Ecken lassen sich mit Lupe verschieben,
  das Bild wird entzerrt («Dokument»: gleichmässig hell, gut lesbar) — alles im Browser, hochgeladen werden nur die
  fertigen Seiten. Mehrere Seiten werden ein PDF im Belegeingang, daraus ein Entwurf wie beim Hochladen am Desktop.
  Unter «Belege prüfen» sieht man, was gelesen wurde, wählt das Konto und bucht, wenn der Entwurf bereit ist; neue
  Lieferanten und IBAN-Abweichungen bleiben dem Desktop.
- **Agent mit Sprache:** Mikrofon tippen, sprechen, nochmals tippen — der Text erscheint im Eingabefeld und wird erst
  nach dem Lesen gesendet. Sprache-zu-Text liefert ein Plugin über `aeradex_sprache_zu_text`, z.B. die Funktion
  «Telegram-Agent» (`aeradex-anruf`) mit `ELEVENLABS_API_KEY` oder dem Gemini-Zugang des Agenten in der Datei, auf die
  `AERADEX_ANRUF_ENV` zeigt (die Unit des Web-Dienstes braucht diese Variable). Ohne Sprachdienst fehlt das Mikrofon.
  Mikrofon und Kamera verlangen HTTPS.
- **Als App:** «Zum Home-Bildschirm» (iPhone: Teilen-Menü; Android: Menü › App installieren) legt ein Icon an, das
  ohne Browserleiste startet. `/manifest.webmanifest` und `/sw.js` sind ohne Anmeldung erreichbar; der Service Worker
  speichert nichts (keine Buchdaten auf dem Handy).
- Rollen gelten wie am Desktop: Leserecht sieht alles, Scannen und Agent fehlen.

