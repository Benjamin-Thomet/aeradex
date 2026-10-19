# aeradex-zugriff

Für ein Buch auf einem Server (`aeradex serve`): Wer darf von seinem eigenen PC mit **Claude Code** oder Codex
(eigenes Abo) an diesem Buch arbeiten? Die Inhaberin trägt unter **Einstellungen › Buch › Zugriff (Claude Code)** den
öffentlichen SSH-Schlüssel einer Person ein; aeradex schreibt ihn beschränkt in `authorized_keys`:

```
restrict,command="/usr/local/sbin/aeradex-ssh anna" ssh-ed25519 AAAA… anna-buchhaltung
```

Der Schlüssel kann genau zwei Dinge: `mcp` (die aeradex-Werkzeuge, Commits als «Anna Muster (Claude Code)») und
`inbox NAME` (eine Datei bis 25 MB in den Belegeingang legen, nur PDF, Bilder, XML, CSV, XLSX). Keine Shell, keine
Weiterleitung, keine anderen Dateien. Zeilen, die aeradex nicht geschrieben hat, bleiben unberührt.

## Auf dem Server

[deploy/setup.sh](../../deploy) richtet alles ein. Von Hand:

```bash
aeradex zugriff skript | sudo install -o root -g root -m 755 /dev/stdin /usr/local/sbin/aeradex-ssh
printf 'BUCH=/srv/aeradex/buch\nVENV=/srv/aeradex/venv\nCONFIG=/srv/aeradex/config\nDOMAIN=buch.example.ch\n' \
  | sudo tee /etc/aeradex-ssh.conf
```

Im Dienst von `aeradex serve`:

| Umgebung | |
|---|---|
| `AERADEX_ZUGRIFF_KEYS` | die `authorized_keys` des Buch-Benutzers; ohne sie zeigt die Seite nur einen Hinweis |
| `AERADEX_ZUGRIFF_BEFEHL` | Pfad des Forced command (Standard `/usr/local/sbin/aeradex-ssh`) |
| `AERADEX_ZUGRIFF_SERVER`, `AERADEX_ZUGRIFF_BENUTZER`, `AERADEX_ZUGRIFF_ALIAS` | was die Anleitung auf der Seite nennt |

## Auf dem PC

- **Windows** (ohne WSL): [windows/install.ps1](windows/install.ps1) `-Server buch.example.ch -Benutzer aeradex
  -Url https://buch.example.ch` installiert Claude Code, erzeugt den Schlüssel, verbindet die Werkzeuge und legt
  «Senden an › Buchhaltung (Beleg)» an. Den ausgegebenen Schlüssel in aeradex eintragen.
- **Linux/Mac:** die Seite in aeradex zeigt die drei Befehle (`ssh-keygen`, `~/.ssh/config`, `claude mcp add`).
