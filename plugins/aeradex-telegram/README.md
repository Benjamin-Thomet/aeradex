# aeradex-telegram

Telegram für [aeradex-anruf](../aeradex-anruf): mit dem aeradex-Agenten chatten, Sprachnachrichten schicken und aus
Telegram anrufen (Mini App). Agent, Karten, ElevenLabs und Gateway kommen aus aeradex-anruf; dieses Plugin bringt nur
den Bot. Karten zum Bestätigen (Rechnung ausstellen, Vorschläge freigeben) erscheinen als Knöpfe im Chat; was du
bestätigst, steht im Verlauf unter deinem Namen mit «(Telegram)».

## Einrichten

### 1. Installieren

```bash
pip install -e plugins/aeradex-anruf -e plugins/aeradex-telegram
aeradex --buch <buch> plugins ein anruf
aeradex --buch <buch> plugins ein telegram
```

### 2. Telegram-Bot

1. Bei [@BotFather](https://t.me/BotFather) `/newbot` aufrufen und den Token kopieren.
2. Deine Telegram-ID herausfinden, z.B. mit [@userinfobot](https://t.me/userinfobot).

### 3. Konfiguration: dieselbe Datei wie aeradex-anruf, `~/.config/aeradex/anruf.env`

Zusätzlich zu den Einträgen von aeradex-anruf:

```ini
TELEGRAM_BOT_TOKEN=123456:ABC…
TELEGRAM_ALLOWED_USER_IDS=123456789          # nur diese Telegram-Konten, mehrere mit Komma
```

Mit `aeradex --buch <buch> telegram status` prüfst du, was noch fehlt.

### 4. Anrufen aus Telegram

Wie in aeradex-anruf: `PUBLIC_URL` (Tailscale Funnel) und `aeradex --buch <buch> anruf elevenlabs-setup`. Der Bot
setzt dann den Menü-Knopf «Anrufen», der die Mini App öffnet. Für Chat und Sprachnachrichten genügt der Bot ohne
öffentliche Adresse, weil er Telegram abfragt.

### 5. Starten

```bash
aeradex --buch <buch> telegram serve
```

Als Dienst: `~/.config/systemd/user/aeradex-telegram.service` (siehe `aeradex-telegram.service` in diesem
Ordner), danach `systemctl --user enable --now aeradex-telegram`.

## Bedienung

- **Text:** «Welche Rechnungen sind überfällig?», «Offerte für K0001: 8 Stunden Malerarbeiten à 95 Franken»,
  «Stell Kunde AG die Rechnung über die Offerte O-2026-003».
- **Sprachnachricht:** wird mit ElevenLabs Scribe transkribiert und wie Text behandelt.
- **Foto/PDF:** landet in `inbox/`, wird als Belegentwurf angelegt und kontiert. Gebucht wird erst nach deiner
  Freigabe in aeradex.
- **Anrufen:** Menü-Knopf im Chat oder `/anrufen`. Das funktioniert in Telegram auf dem Handy; Telegram Desktop
  unter Linux gibt das Mikrofon oft nicht frei. Nach dem Auflegen kommt eine Zusammenfassung in den Chat.
- `/offen` zeigt, was auf dich wartet, `/neu` beginnt ein neues Gespräch, `/stimme an` liest Antworten vor.

## Sicherheit

- Nur Telegram-IDs aus `TELEGRAM_ALLOWED_USER_IDS`, nur private Chats.
- Die Mini App weist sich mit Telegram-`initData` aus, geprüft per HMAC mit dem Bot-Token und höchstens 5 Minuten
  alt. Erst dann holt der Gateway eine signierte ElevenLabs-Adresse und ein Sitzungstoken (2 Stunden).
- Der ElevenLabs-Agent verlangt eine Anmeldung, also kann niemand sonst einen Anruf starten. Der Custom-LLM-Endpunkt
  verlangt `LLM_SHARED_SECRET` und ein gültiges Sitzungstoken. Der Webhook wird per HMAC geprüft.
- Schlüssel liegen nur in der env-Datei, nie im Buch. Offene Karten liegen in `.aeradex/lokal/telegram/`, das git
  ignoriert.
- Der Agent im Anruf ist ein Sprachmodell: Lies Beträge auf der Karte, bevor du **Ausstellen** tippst.
