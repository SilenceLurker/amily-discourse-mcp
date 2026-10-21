import argparse
import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import warnings

from .errors import AppError
from .service import Service
from .store import Store, valid_origin
from .vault import DPAPIVault


def key_prompt() -> str:
    if not sys.stdin.isatty():
        raise AppError("Enter credentials in an interactive local terminal; redirected input is disabled.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            key = getpass.getpass("Discourse API Key (hidden): ")
        except getpass.GetPassWarning:
            raise AppError("A terminal with hidden credential input is required.") from None
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,512}", key):
        raise AppError("API Key has an invalid length or character set.")
    return key


def parser():
    result = argparse.ArgumentParser(description="Local multi-account Discourse MCP (Windows).")
    result.add_argument("--state-dir", type=Path,
                        default=Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Amily" / "DiscourseMCP")
    commands = result.add_subparsers(dest="command", required=True)
    add = commands.add_parser("account-add", help="Enroll a new alias; key is requested through hidden input.")
    add.add_argument("alias")
    add.add_argument("--site", required=True)
    add.add_argument("--username", required=True)
    add.add_argument("--categories", required=True, help="Comma-separated category ids.")
    add.add_argument("--actions", default="read,create,reply,edit")
    add.add_argument("--allow-unattended", action="store_true", help="Explicitly allow this account's writes without local draft approval.")
    commands.add_parser("accounts")
    for name in ("account-rotate", "account-disable", "account-enable"):
        commands.add_parser(name).add_argument("account")
    show = commands.add_parser("draft-show")
    show.add_argument("account")
    show.add_argument("draft_id")
    approve = commands.add_parser("draft-approve", help="Approve a concrete revision/hash after reviewing draft-show.")
    approve.add_argument("account")
    approve.add_argument("draft_id")
    approve.add_argument("--revision", type=int, required=True)
    approve.add_argument("--hash", required=True)
    resolve = commands.add_parser("draft-resolve", help="Stop all clients and inspect the forum before using this command.")
    resolve.add_argument("account")
    resolve.add_argument("draft_id")
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--post-id", type=int)
    outcome.add_argument("--confirm-not-sent", action="store_true")
    resolve.add_argument("--clients-stopped", action="store_true", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--accounts", required=True, help="Explicit comma-separated aliases exposed to this client.")
    return result


def main():
    args = parser().parse_args()
    try:
        store = Store(args.state_dir.resolve())
        vault = DPAPIVault()
        service = Service(store, vault)
        if args.command == "account-add":
            site = valid_origin(args.site)
            try:
                categories = sorted(set(int(x) for x in args.categories.split(",")))
            except ValueError:
                raise AppError("Categories must be comma-separated positive integers.") from None
            secret = vault.encrypt(args.alias, key_prompt())
            store.add_account(args.alias, {"site": site, "username": args.username,
                "categories": categories, "actions": sorted(set(args.actions.split(","))),
                "require_approval": not args.allow_unattended}, secret)
            output = {"account": args.alias, "saved": True, "require_approval": not args.allow_unattended}
        elif args.command == "accounts":
            output = {"accounts": store.accounts()}
        elif args.command == "account-rotate":
            store.account(args.account, include_disabled=True)
            store.rotate(args.account, vault.encrypt(args.account, key_prompt()))
            output = {"account": args.account, "rotated_locally": True,
                      "note": "Old API keys must be revoked separately in Discourse; existing drafts need a new revision."}
        elif args.command in ("account-enable", "account-disable"):
            store.enable(args.account, args.command == "account-enable")
            output = {"account": args.account, "enabled": args.command == "account-enable"}
        elif args.command == "draft-show":
            output = service.draft(args.account, args.draft_id)
        elif args.command == "draft-approve":
            store.approve(args.account, args.draft_id, args.revision, args.hash)
            output = {"approved": True, "revision": args.revision}
        elif args.command == "draft-resolve":
            output = service.resolve(args.account, args.draft_id, args.post_id)
        else:
            aliases = args.accounts.split(",")
            if not aliases or any(not alias for alias in aliases):
                raise AppError("Provide at least one explicit account alias.")
            for alias in aliases:
                store.account(alias)
            from .server import build_server
            build_server(Service(store, vault, allowed_accounts=aliases)).run(transport="stdio")
            return
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except AppError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, sqlite3.Error):
        print("Local operation failed. Check state directory, terminal and configuration.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
