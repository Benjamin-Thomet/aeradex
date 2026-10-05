# allkvitt auf einem Server betreiben

`allkvitt ui` ist für den eigenen Rechner gedacht (ein Benutzer, Zugangsschlüssel im Link).
`allkvitt serve` ist der Mehrbenutzer-Betrieb mit Login: für einen eigenen Server, erreichbar über HTTPS.

## Grundsätze

- **Ein Buch pro Instanz.** Für mehrere Mandanten mehrere Instanzen auf verschiedenen Ports bzw. Subdomains
  (z.B. `muster.buchhaltung.example.ch`), je mit eigenem Buch und eigener Benutzerliste.
- **HTTPS macht der Reverse Proxy** (Caddy oder nginx); allkvitt selbst hört nur auf `127.0.0.1`.
- **Benutzer liegen nicht im Buch.** `users.yaml` und `secret` stehen im Konfigurationsverzeichnis
  (Standard `~/.config/allkvitt`, sonst `--config DIR` bzw. `$ALLKVITT_CONFIG`), Dateirechte 600.
- **Jede Änderung trägt den Namen der angemeldeten Person** im git-Verlauf; Änderungen des Agenten sind als Agent markiert.

## Rollen

| Rolle | darf |
|---|---|
| `lesen` | alles ansehen, Berichte und PDFs öffnen |
| `buchhaltung` | buchen, Rechnungen, Kreditoren, Bank, Lohn, MWST, Agent |
| `admin` | zusätzlich Einstellungen ändern und Perioden sperren/entsperren |

## Einrichten

```bash
# als eigener Systembenutzer, z.B. "allkvitt"
python3 -m venv ~/venv && ~/venv/bin/pip install "allkvitt[ui,scan] @ git+https://github.com/…/allkvitt"
git clone <repo-des-buchs> ~/buecher/muster       # oder: allkvitt init ~/buecher/muster --firma …
~/venv/bin/allkvitt user add anna --rolle admin --anzeige "Anna Muster"
~/venv/bin/allkvitt user add treuhand --rolle lesen --anzeige "Treuhand Muster"
~/venv/bin/allkvitt --buch ~/buecher/muster serve --port 8080 --https
```

Weitere Befehle: `allkvitt user list`, `allkvitt user passwort NAME` (beendet bestehende Anmeldungen),
`allkvitt user remove NAME`. Nach 5 Fehlversuchen ist ein Benutzer bzw. eine Adresse 5 Minuten gesperrt.

### systemd

`/etc/systemd/system/allkvitt-muster.service`:

```ini
[Unit]
Description=allkvitt Buchhaltung Muster GmbH
After=network.target

[Service]
User=allkvitt
WorkingDirectory=/home/allkvitt/buecher/muster
Environment=ALLKVITT_CONFIG=/home/allkvitt/.config/allkvitt-muster
ExecStart=/home/allkvitt/venv/bin/allkvitt --buch /home/allkvitt/buecher/muster serve --port 8080 --https
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/allkvitt/buecher/muster /home/allkvitt/.config/allkvitt-muster

[Install]
WantedBy=multi-user.target
```

### Monatsbericht automatisch (Timer)

`/etc/systemd/system/allkvitt-muster-monatsbericht.service` und `.timer` — am 5. jedes Monats der Bericht des
Vormonats, per E-Mail an `mail.an` aus `allkvitt.yaml`:

```ini
# .service
[Service]
Type=oneshot
User=allkvitt
Environment=ALLKVITT_SMTP_PASSWORD=…
ExecStart=/home/allkvitt/venv/bin/allkvitt --buch /home/allkvitt/buecher/muster bericht monat --mail

# .timer
[Timer]
OnCalendar=*-*-05 06:00
Persistent=true
[Install]
WantedBy=timers.target
```

Ohne systemd genügt cron: `0 6 5 * * allkvitt --buch … bericht monat --mail`. Das Passwort steht nie im Buch.

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
