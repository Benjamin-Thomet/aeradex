#!/usr/bin/env bash
# Ein aeradex-Buch auf einem eigenen Server (Debian/Ubuntu, systemd, Caddy): aeradex mit Login, optional Team-App,
# Telegram/Anruf und Zugriff für Claude Code per SSH. Vom Admin ausführen (nicht von einem Agenten), idempotent:
#
#   sudo cp deploy/server.conf.example /etc/aeradex-server.conf && sudo nano /etc/aeradex-server.conf
#   sudo bash deploy/setup.sh [/etc/aeradex-server.conf]
#
# Ein bestehendes Buch unter BASIS/buch bleibt, wie es ist (z.B. vorher mit `aeradex import-buch` hingelegt).
# Schlüssel (Agent, Telegram, ElevenLabs) trägt man danach in aeradex ein, nicht hier.
# Vor jeder Änderung an einer bestehenden Datei: Sicherung «<datei>.vor-aeradex».
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CONF="${1:-/etc/aeradex-server.conf}"
[ "$(id -u)" = 0 ] || { echo "bitte mit sudo"; exit 1; }
[ -r "$CONF" ] || { echo "$CONF fehlt (Vorlage: $HERE/server.conf.example)"; exit 1; }
# shellcheck source=/dev/null
. "$CONF"
: "${DOMAIN:?}" "${BENUTZER:?}" "${BASIS:?}" "${QUELLE:?}"
PORTAL_DOMAIN="${PORTAL_DOMAIN:-}"; PLUGINS="${PLUGINS:-}"; PORTAL_KENNUNG="${PORTAL_KENNUNG:-aeradex}"
BUCH_PORT="${BUCH_PORT:-4200}"; PORTAL_PORT="${PORTAL_PORT:-4100}"; GATEWAY_PORT="${GATEWAY_PORT:-8765}"
U=$BENUTZER; BUCH=$BASIS/buch; VENV=$BASIS/venv; CFG=$BASIS/config
TS=vor-aeradex
backup() { for f in "$@"; do [ -e "$f" ] && [ ! -e "$f.$TS" ] && cp -a "$f" "$f.$TS"; done; return 0; }
say() { printf '\n== %s\n' "$*"; }
has() { [[ " $PLUGINS " == *" $1 "* ]]; }
# a package from QUELLE: a git URL (…#subdirectory=…) or a local checkout
pkg() {
  local name="$1" extras="${2:-}" dir="${3:-}"
  if [[ "$QUELLE" == git+* || "$QUELLE" == http* ]]; then
    printf '%s%s @ %s%s' "$name" "$extras" "$QUELLE" "${dir:+#subdirectory=$dir}"
  else
    printf '%s%s' "$QUELLE${dir:+/$dir}" "$extras"
  fi
}
render() {   # template → stdout, {{VAR}} from the config
  sed -e "s|{{DOMAIN}}|$DOMAIN|g" -e "s|{{PORTAL_DOMAIN}}|$PORTAL_DOMAIN|g" -e "s|{{BENUTZER}}|$U|g" \
      -e "s|{{BASIS}}|$BASIS|g" -e "s|{{FIRMA}}|${FIRMA:-}|g" -e "s|{{BUCH_PORT}}|$BUCH_PORT|g" \
      -e "s|{{PORTAL_PORT}}|$PORTAL_PORT|g" -e "s|{{GATEWAY_PORT}}|$GATEWAY_PORT|g" "$1"
}
caddy_site() {   # add a site block once, validate, roll back if invalid
  local file="$1" marker="$2" CADDY=/etc/caddy/Caddyfile
  grep -q "^$marker {" "$CADDY" 2>/dev/null && return 0
  backup "$CADDY"
  { printf '\n'; render "$file" | grep -v '^#'; } >> "$CADDY"
  if caddy validate --config "$CADDY" --adapter caddyfile >/dev/null 2>&1; then
    chown caddy:caddy /var/log/caddy/aeradex.log 2>/dev/null || true   # validate (as root) creates it
    systemctl reload caddy && echo "caddy: $marker"
  else
    cp -a "$CADDY.$TS" "$CADDY"; echo "Caddyfile ungültig, zurückgesetzt"; exit 1
  fi
}

python3 -c 'import sys; assert sys.version_info >= (3, 11)' || { echo "python3 >= 3.11 nötig"; exit 1; }
apt-get install -y -qq git python3-venv tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-ita >/dev/null

say "Benutzer und Ordner"
id "$U" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$BASIS" --shell /bin/bash "$U"
passwd -l "$U" >/dev/null                                 # no password login, keys only
install -d -o "$U" -g "$U" -m 750 "$BASIS" "$BASIS/.cache"
install -d -o "$U" -g "$U" -m 700 "$BASIS/.ssh" "$CFG"

say "Programme (venv)"
[ -d "$VENV" ] || sudo -u "$U" python3 -m venv "$VENV"
sudo -u "$U" "$VENV/bin/pip" install -q --upgrade pip
install -o root -g root -m 755 "$HERE/aeradex-update" /usr/local/sbin/aeradex-update
AERADEX_SERVER_CONF="$CONF" /usr/local/sbin/aeradex-update --ohne-neustart

say "Buch"
if [ ! -e "$BUCH/aeradex.yaml" ]; then
  sudo -u "$U" git config --global user.name "aeradex Server"
  sudo -u "$U" git config --global user.email "buch@$DOMAIN"
  sudo -u "$U" "$VENV/bin/aeradex" init "$BUCH" --firma "${FIRMA:?FIRMA fehlt}" --rechtsform "${RECHTSFORM:-AG}" \
    --jahr "${JAHR:-$(date +%Y)}" >/dev/null
  for p in $PLUGINS; do sudo -u "$U" "$VENV/bin/aeradex" --buch "$BUCH" plugins ein "$p" >/dev/null; done
  echo "Neues Buch: $BUCH (Plugins: ${PLUGINS:-keine})"
else
  echo "Buch besteht: $BUCH (unverändert)"
fi
sudo -u "$U" "$VENV/bin/aeradex" --buch "$BUCH" check | tail -1

if has anruf || has telegram; then
  say "Telegram/Anruf: Grundeinstellungen"
  if [ ! -e "$CFG/bot.env" ]; then
    printf 'PUBLIC_URL=https://%s\nAERADEX_ANRUF_BACKEND=api\nAERADEX_ANRUF_WERKZEUGE=entwuerfe\nLLM_SHARED_SECRET=%s\n' \
      "$DOMAIN" "$(openssl rand -hex 32)" > "$CFG/bot.env"
  fi
fi
chown -R "$U:$U" "$CFG"; chmod 700 "$CFG"; find "$CFG" -maxdepth 1 -type f -exec chmod 600 {} +

if [ -n "$PORTAL_DOMAIN" ]; then
  say "Team-App (Portal)"
  id aeradex-portal >/dev/null 2>&1 || useradd --system --home-dir /var/lib/aeradex-team-portal --shell /usr/sbin/nologin aeradex-portal
  install -d -o root -g root -m 755 /srv/aeradex-team-portal
  install -d -o aeradex-portal -g aeradex-portal -m 700 /var/lib/aeradex-team-portal
  install -d -o root -g aeradex-portal -m 750 /etc/aeradex-team-portal
  ENV=/etc/aeradex-team-portal/portal.env
  if [ ! -f "$ENV" ]; then
    TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    umask 027
    cat > "$ENV" <<EOF
AERADEX_PORTAL_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')
AERADEX_PORTAL_SYNC_TOKEN_SHA256=$(printf '%s' "$TOKEN" | sha256sum | cut -d' ' -f1)
AERADEX_PORTAL_KENNUNG=$PORTAL_KENNUNG
AERADEX_PORTAL_TITEL=Team · ${FIRMA:-aeradex}
${PORTAL_BRANDING:+AERADEX_PORTAL_BRANDING=$PORTAL_BRANDING}
EOF
    umask 022
    chown root:aeradex-portal "$ENV"; chmod 640 "$ENV"
    # the sync runs on this server: the book talks to the portal over localhost
    printf 'url: http://127.0.0.1:%s\ntoken: %s\n' "$PORTAL_PORT" "$TOKEN" > "$CFG/team.yaml"
    chown "$U:$U" "$CFG/team.yaml"; chmod 600 "$CFG/team.yaml"
  fi
  render "$HERE/systemd/aeradex-team-portal.service" > /etc/systemd/system/aeradex-team-portal.service
  render "$HERE/systemd/aeradex-team.service" > /etc/systemd/system/aeradex-team.service
fi

if has zugriff; then
  say "SSH-Zugang für die Werkzeuge (Claude Code)"
  sudo -u "$U" "$VENV/bin/aeradex" zugriff skript > /tmp/aeradex-ssh.$$
  install -o root -g root -m 755 /tmp/aeradex-ssh.$$ /usr/local/sbin/aeradex-ssh; rm -f /tmp/aeradex-ssh.$$
  printf 'BUCH=%s\nVENV=%s\nCONFIG=%s\nDOMAIN=%s\n' "$BUCH" "$VENV" "$CFG" "$DOMAIN" > /etc/aeradex-ssh.conf
  chmod 644 /etc/aeradex-ssh.conf
  touch "$BASIS/.ssh/authorized_keys"
  chown "$U:$U" "$BASIS/.ssh/authorized_keys"; chmod 600 "$BASIS/.ssh/authorized_keys"
fi

say "Dienste"
render "$HERE/systemd/aeradex.service" > /etc/systemd/system/aeradex.service
UNITS=(aeradex)
[ -n "$PORTAL_DOMAIN" ] && UNITS+=(aeradex-team-portal aeradex-team)
if has telegram; then
  render "$HERE/systemd/aeradex-telegram.service" > /etc/systemd/system/aeradex-telegram.service
  UNITS+=(aeradex-telegram)
fi
systemctl daemon-reload
for unit in "${UNITS[@]}"; do systemctl enable --quiet "$unit.service"; systemctl restart "$unit.service"; done

say "Caddy"
caddy_site "$HERE/caddy/buch.caddy" "$DOMAIN"
[ -n "$PORTAL_DOMAIN" ] && caddy_site "$HERE/caddy/portal.caddy" "$PORTAL_DOMAIN"

say "fail2ban"
if [ -d /etc/fail2ban/filter.d ]; then
  install -m 644 "$HERE/fail2ban/aeradex.conf" "$HERE/fail2ban/aeradex-team-portal.conf" /etc/fail2ban/filter.d/
  grep -q '^\[aeradex\]' /etc/fail2ban/jail.local 2>/dev/null || cat "$HERE/fail2ban/jail.local.snippet" >> /etc/fail2ban/jail.local
  systemctl reload fail2ban || true
fi

cat <<EOF

=== Noch zu tun ===
1. DNS: A-Eintrag $DOMAIN${PORTAL_DOMAIN:+ und $PORTAL_DOMAIN} → $(hostname -I | awk '{print $1}')
2. Logins anlegen (fragt nach dem Passwort):
     sudo -u $U AERADEX_CONFIG=$CFG $VENV/bin/aeradex --buch $BUCH user add NAME --rolle admin --anzeige "Vorname Name"
3. In aeradex (https://$DOMAIN, als admin): Einstellungen › Agent; Claude API alternativ in $CFG/anthropic.env
   (ANTHROPIC_API_KEY=…, Besitzer $U, 600), dann: sudo systemctl restart ${UNITS[*]}
EOF
has telegram && echo "   Telegram: Plugins › (Sekretärin, Standard «Susi») › Zugänge: Bot-Token, erlaubte IDs; der Bot startet dann von selbst."
has zugriff && echo "4. Claude Code: öffentliche Schlüssel unter Plugins › Zugriff (Claude Code) eintragen."
[ -n "$PORTAL_DOMAIN" ] && echo "5. Team-App: https://$PORTAL_DOMAIN/app — Mitarbeitende unter Plugins › Team-App einladen."
echo "Prüfen: systemctl status ${UNITS[*]}"
