"""CLI: ``python -m studio_server serve|create-admin|reset-password`` (§3.1).

Adapted from QCnext ``cli.py`` (https://github.com/MicRaving/qcnext), reduced to
the commands the foundation needs.
"""

from __future__ import annotations

import argparse
import os
import sys

from .config import load_config
from .db import (
    audit,
    connect,
    get_user_by_id,
    get_user_by_username,
    init_db,
    insert_user,
    revoke_user_tokens,
    transaction,
    update_user,
)
from .security import hash_password

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8770


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    cfg = load_config(args.data)
    app = create_app(cfg)
    print(
        f"studio-server listening on http://{args.host}:{args.port} "
        f"(data: {cfg.data_dir})",
        flush=True,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", workers=1)
    return 0


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def _cmd_create_admin(args: argparse.Namespace) -> int:
    # The deploy installer runs this without flags and exports ADMIN_USER /
    # ADMIN_PASSWORD, so accept the environment too (flags always win).
    username = (args.username or _env("STUDIO_ADMIN_USER") or _env("ADMIN_USER")).strip()
    password = args.password or _env("STUDIO_ADMIN_PASSWORD") or _env("ADMIN_PASSWORD")
    if not username or not password:
        print(
            "create-admin needs --username/--password "
            "(or ADMIN_USER/ADMIN_PASSWORD in the environment)",
            file=sys.stderr,
        )
        return 2
    cfg = load_config(args.data)
    init_db(cfg.data_dir)
    conn = connect(cfg.data_dir)
    try:
        existing = get_user_by_username(conn, username)
        with transaction(conn, immediate=True):
            if existing is None:
                row = insert_user(
                    conn,
                    username,
                    hash_password(password),
                    is_admin=True,
                )
                action = "user.create"
            else:
                row = update_user(
                    conn,
                    int(existing["id"]),
                    pw_hash=hash_password(password),
                    is_admin=True,
                    disabled=False,
                )
                assert row is not None
                revoke_user_tokens(conn, int(row["id"]))
                action = "user.promote"
            audit(conn, None, int(row["id"]), action, {"username": row["username"]})
        print(f"admin ready: {row['username']} (id {row['id']})")
        return 0
    finally:
        conn.close()


def _cmd_reset_password(args: argparse.Namespace) -> int:
    if not args.password:
        print("a non-empty --password is required", file=sys.stderr)
        return 2
    cfg = load_config(args.data)
    init_db(cfg.data_dir)
    conn = connect(cfg.data_dir)
    try:
        user = get_user_by_username(conn, args.username)
        if user is None:
            print(f"no such user: {args.username}", file=sys.stderr)
            return 1
        with transaction(conn, immediate=True):
            update_user(conn, int(user["id"]), pw_hash=hash_password(args.password))
            revoke_user_tokens(conn, int(user["id"]))
            audit(
                conn,
                None,
                int(user["id"]),
                "user.password.reset",
                {"username": user["username"]},
            )
        fresh = get_user_by_id(conn, int(user["id"]))
        assert fresh is not None
        print(f"password reset for {fresh['username']} (id {fresh['id']})")
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m studio_server",
        description="CodingStudio online-project sync server",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the HTTP server (one worker)")
    serve.add_argument("--host", default=DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--data", default=None, help="data dir (default $STUDIO_DATA_DIR or ./data)")
    serve.set_defaults(func=_cmd_serve)

    create = sub.add_parser("create-admin", help="create or promote a server admin")
    create.add_argument("--username", default=None)
    create.add_argument("--password", default=None)
    create.add_argument("--data", default=None)
    create.set_defaults(func=_cmd_create_admin)

    reset = sub.add_parser("reset-password", help="set a user's password")
    reset.add_argument("--username", required=True)
    reset.add_argument("--password", required=True)
    reset.add_argument("--data", default=None)
    reset.set_defaults(func=_cmd_reset_password)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
