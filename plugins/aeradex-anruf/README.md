# aeradex-anruf

Mit dem aeradex-Agenten **sprechen** wie mit einem Mitarbeiter: Du rufst an, sagst, was zu tun ist, und er
erledigt es in der Buchhaltung.

- **Claude** ist der Agent. Er arbeitet mit denselben Werkzeugen wie das Agent-Panel in der Oberfläche,
  standardmässig über **Claude Code mit deinem eigenen Login** (dein Claude-Abo, kein API-Schlüssel).
- **ElevenLabs** übernimmt nur die Sprache (Spracherkennung, Gesprächsführung, Stimme) und holt jede Antwort über
  einen *Custom LLM* beim Gateway dieses Plugins.

```
Browser (Anrufseite) ──ElevenLabs-SDK──► ElevenLabs (Sprache) ──Custom LLM──► aeradex anruf serve ──► Claude ──► aeradex ──► git
        ▲                                                                            │
        └──────── Karten, PDFs, «was der Agent tut» (live) ◄────────────────────────┘
```

Neben dem Gespräch zeigt die Anrufseite live, was der Agent tut (welche Werkzeuge er aufruft), die Karten zum
Bestätigen und die PDFs, die er erstellt. Wer Telegram dazu will, nimmt das Plugin
[aeradex-telegram](../aeradex-telegram); es baut auf diesem auf.

## Einrichten

1. Installieren und im Buch einschalten:
   ```bash
   pip install -e plugins/aeradex-anruf
   aeradex --buch <buch> plugins ein anruf
   ```
2. `~/.config/aeradex/anruf.env` (chmod 600):
   ```ini
   ELEVENLABS_API_KEY=…
   LLM_SHARED_SECRET=…                     # openssl rand -hex 32
   PUBLIC_URL=https://<rechner>.<tailnet>.ts.net
   # optional:
   AERADEX_ANRUF_BACKEND=claude-code       # Standard: Claude Code mit deinem Login; «api» braucht ANTHROPIC_API_KEY
   # Auf einem Server ohne Claude-Login, z.B. mit Google Gemini (OpenAI-kompatibel, ohne Websuche):
   # AERADEX_ANRUF_BACKEND=openai
   # AERADEX_OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
   # AERADEX_OPENAI_API_KEY=…
   # AERADEX_OPENAI_MODEL=<Gemini-Modell>
   AERADEX_VOICE_MODEL=claude-sonnet-5-5   # Modell im Anruf
   ELEVENLABS_VOICE_ID=                    # Stimme; leer = «Daniel»
   ELEVENLABS_WEBHOOK_SECRET=…             # für die Zusammenfassung nach dem Anruf
   ```
3. Öffentliche Adresse für ElevenLabs: `tailscale funnel --bg 8765`. Die angezeigte Adresse ist `PUBLIC_URL`. Die
   Anrufseite selbst öffnest du lokal.
4. Den Sprach-Agenten bei ElevenLabs anlegen. Der Befehl schreibt `ELEVENLABS_AGENT_ID` in die env-Datei:
   ```bash
   aeradex --buch <buch> anruf elevenlabs-setup --begruessung "Grüezi Marco, was steht an?"
   ```
   Optional im ElevenLabs-Dashboard den Post-Call-Webhook auf `PUBLIC_URL/elevenlabs/webhook` setzen; dann
   erscheint nach dem Auflegen die Zusammenfassung auf der Seite.
5. Starten:
   ```bash
   aeradex --buch <buch> anruf serve
   ```
   Der Browser öffnet die Anrufseite. Der Link mit `?key=…` ist der Zugang, also nicht weitergeben. Dann
   **Anrufen** und sprechen.

`aeradex --buch <buch> anruf status` zeigt, was noch fehlt.

## Deine Sekretärin

- **Persona:** `AERADEX_ANRUF_NAME` (Standard Susi) ist die Sekretärin von `AERADEX_ANRUF_CHEF`
  (Standard: Vorname aus `git config user.name`) bei der Firma aus dem Buch. Sie duzt dich, ist herzlich, denkt mit
  und erfindet nichts über dich.
- **Gedächtnis:** `.aeradex/lokal/anruf/gedaechtnis.md` pro Buch (nicht in git). «Merk dir …» landet dort
  (`remember`). Beim Auflegen schreibt sie einen Satz über das Gespräch, und beim nächsten Anruf kennt sie ihn.
- **Begrüssung:** je nach Tageszeit mit deinem Vornamen (ElevenLabs `first_message = {{begruessung}}`).
- **Tempo:** Haiku antwortet (`AERADEX_VOICE_MODEL`, etwa 1 bis 3 s). Mehrstufige Aufgaben gibt sie an einen
  Opus-«Buchhalter» weiter (`AERADEX_EXPERT_MODEL`). Claude Code läuft den ganzen Anruf durch und wird schon beim
  Tippen auf «Anrufen» vorgewärmt. Bei einer Pause sagt sie «Moment.» (nach 3,5 s, danach alle 6 s).
- **Werkzeuge:** `AERADEX_ANRUF_WERKZEUGE=alle` (Standard) gibt alle aeradex-Werkzeuge wie im MCP-Chat; vor
  Unumkehrbarem fragt sie nach einem klaren Ja. Mit `entwuerfe` darf sie nur Entwürfe anlegen, Ausstellen nur per
  Karte.
- **Internet:** Websuche, z.B. Handelsregister für neue Kunden. Webinhalte gelten nur als Daten, nie als Anweisung.
- **ElevenLabs:** `cascade_timeout_seconds` 15, `turn_eagerness` patient, Modell `eleven_v4_turbo`.

## Die Seite in aeradex

Unter **Plugins → Susi** (bzw. dem Namen aus `AERADEX_ANRUF_NAME`):
- ob der Dienst läuft, mit dem Knopf **Anrufen**; der laufende Dienst meldet sich in `.aeradex/lokal/anruf/dienst.json`
- **Wartet auf dich**: Karten (Rechnung ausstellen, Vorschläge freigeben) direkt in aeradex bestätigen
- **Was sie sich merkt**: das Gedächtnis bearbeiten, eine Notiz pro Zeile
- **Letzte Gespräche**: ihre Notiz zu jedem Anruf
- **Einstellungen** auf einen Blick: Name, Stimme, Modelle, Werkzeuge, Telegram

## Claude Code oder Claude API

| | `claude-code` (Standard) | `api` |
|---|---|---|
| Kosten | dein Claude-Abo (zählt gegen dessen Limits) | pro Token (`ANTHROPIC_API_KEY`) |
| Antwortzeit im Anruf | erste Antwort etwa 4 s, danach 1 bis 3 s (Haiku, vorgewärmt), mit Werkzeugen mehr | etwa 1 bis 3 s |

Während der Agent arbeitet, sagt er «Moment, ich schaue nach.». Mit Claude Code startet jede Antwort `claude -p`
neu. Dabei gilt:

- Es werden keine Benutzereinstellungen und keine Hooks geladen.
- Eingebaut ist nur das Werkzeug *Read*, und zwar nur für die Ordner `inbox/` und `belege/`.
- Der aeradex-MCP-Server bietet nur die erlaubten Werkzeuge an (`AERADEX_MCP_TOOLS`).

## Was der Agent darf

| darf | darf nicht (nur per Knopf durch dich) |
|---|---|
| alles lesen: Salden, Rechnungen, offene Posten, Berichte, MWST | Rechnungen ausstellen |
| Offerten als Entwurf (`draft_quote`, Plugin leistungen) | Vorschläge freigeben, buchen, stornieren |
| Belege in den Eingang legen und kontieren | zahlen, Zahlungslauf, Lohn abschliessen |
| Buchungen *vorschlagen*, Kunden anlegen, Zeit erfassen | |

Will der Agent eine Rechnung stellen, ruft er `request_invoice` auf. Auf der Anrufseite erscheint eine Karte mit
den Beträgen, die aeradex gerechnet hat, und du klickst **Ausstellen** oder **Verwerfen**. Ausgestellt wird
deterministisch, ohne LLM, unter deinem git-Benutzer. Eine Karte gilt genau einmal und läuft nach 24 Stunden ab.
Für Buchungsvorschläge funktioniert es gleich (`request_approval`).

## Sicherheit

- Die Anrufseite erreicht nur, wer den Link mit dem Zugangsschlüssel hat. Der Schlüssel wird bei jedem Start neu
  erzeugt.
- Der ElevenLabs-Agent verlangt eine Anmeldung. Nur der Gateway kann eine signierte Adresse holen, also kann niemand
  sonst einen Anruf starten.
- Der Custom-LLM-Endpunkt verlangt `LLM_SHARED_SECRET` und ein gültiges Sitzungstoken (2 Stunden). Der
  Post-Call-Webhook wird per HMAC geprüft.
- Schlüssel liegen nur in der env-Datei, nie im Buch. Offene Karten liegen in `.aeradex/lokal/anruf/`, das git
  ignoriert.
- Der Agent im Anruf ist ein Sprachmodell: Lies die Beträge auf der Karte, bevor du **Ausstellen** klickst.
