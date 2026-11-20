import argparse
import sqlite3
from pathlib import Path

from .store import Store


def main():
    parser = argparse.ArgumentParser(prog="python -m service")
    parser.add_argument(
        "--data", type=Path, required=True, help="Private service database, uploads and results"
    )
    commands = parser.add_subparsers(dest="action", required=True)
    user = commands.add_parser("create-user", help="Issue a personal token; it is displayed once")
    user.add_argument("name")
    revoke = commands.add_parser("disable-user")
    revoke.add_argument("name")
    serve = commands.add_parser("serve")
    serve.add_argument("--upstream", required=True, type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--cert", type=Path)
    serve.add_argument("--key", type=Path)
    args = parser.parse_args()
    if args.action == "create-user":
        if not 1 <= len(args.name.strip()) <= 120:
            parser.error("Provide a name between 1 and 120 characters.")
        try:
            print(Store(args.data).add_user(args.name.strip()))
        except sqlite3.IntegrityError:
            parser.error("This account already exists.")
    elif args.action == "disable-user":
        with Store(args.data).db(True) as db:
            if not db.execute("UPDATE users SET enabled=0 WHERE name=?", (args.name,)).rowcount:
                parser.error("Unknown account.")
    else:
        if bool(args.key) != bool(args.cert):
            parser.error("Supply both --cert and --key.")
        if not args.cert and args.host not in ("127.0.0.1", "::1", "localhost"):
            parser.error("A LAN listener needs --cert and --key.")
        import uvicorn

        from .api import create_app

        uvicorn.run(
            create_app(args.upstream, args.data),
            host=args.host,
            port=args.port,
            proxy_headers=False,
            access_log=False,
            ssl_certfile=str(args.cert) if args.cert else None,
            ssl_keyfile=str(args.key) if args.key else None,
        )


if __name__ == "__main__":
    main()
