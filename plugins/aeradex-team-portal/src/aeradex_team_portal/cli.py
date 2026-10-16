"""`aeradex-team-portal serve` and helpers for setting up the server."""
from __future__ import annotations

import argparse
import logging
import secrets

from . import auth


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aeradex-team-portal", description="Team-App für Mitarbeitende (aeradex)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="Portal starten (hinter Caddy)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=4100)
    sub.add_parser("sync-token", help="Neues Sync-Token für das Buch erzeugen")
    sub.add_parser("secret", help="Neues Session-Secret erzeugen")
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn
        from .app import create_app
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=False, access_log=False)
    elif args.cmd == "sync-token":
        token = secrets.token_urlsafe(32)
        print("Token für das Buch (nur jetzt sichtbar; dort: aeradex team verbinden --url … --token …):")
        print(f"  {token}")
        print("Zeile für /etc/aeradex-team-portal/portal.env:")
        print(f"  AERADEX_PORTAL_SYNC_TOKEN_SHA256={auth.sha256(token)}")
    elif args.cmd == "secret":
        print(f"AERADEX_PORTAL_SECRET={auth.new_secret()}")


if __name__ == "__main__":
    main()
