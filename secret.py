#!/usr/bin/env python3
"""
secret.py — CLI for the built-in encrypted secret store (Spec 026, Phase 3).

Usage:
    python secret.py init [--force]
    python secret.py get NAME
    python secret.py set NAME [VALUE]     # VALUE may be omitted; reads from stdin
    python secret.py list
    python secret.py rm NAME
    python secret.py import FILE

To put this on PATH (run once):
    ln -s /path/to/claude-ops-bot/secret.py ~/.local/bin/secret
    chmod +x ~/.local/bin/secret
Or add an alias in ~/.bashrc:
    alias secret='python /path/to/claude-ops-bot/secret.py'

Depends only on stdlib + cryptography (already installed system-wide).
Does NOT import bot.py or webapp.py.
"""

import argparse
import sys
from pathlib import Path

# Ensure the repo root is on the path regardless of where the script is invoked from
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

import secretstore


def cmd_init(args: argparse.Namespace) -> int:
    try:
        path = secretstore.init_key(force=args.force)
        print(f"Key written to: {path}")
        print("Store encryption is ready. Run  `secret set NAME value`  to add secrets.")
        return 0
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_get(args: argparse.Namespace) -> int:
    try:
        value = secretstore.get(args.name)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if value is None:
        print(f"error: secret '{args.name}' not found", file=sys.stderr)
        return 1
    # Print value to stdout (this is the one intentional reveal)
    print(value)
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    if args.value is not None:
        value = args.value
    else:
        # Read from stdin (piped or typed)
        if sys.stdin.isatty():
            print(f"Enter value for '{args.name}' (Ctrl-D to finish):")
        value = sys.stdin.read().rstrip("\n")

    try:
        secretstore.set(args.name, value)
        print(f"Set: {args.name}")
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    try:
        # Reserved names (^__.*__$) are hidden from the list — TOTP internals.
        # Pass include_reserved=False (default) to suppress them.
        metas = secretstore.list_meta()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not metas:
        print("(no secrets stored)")
        return 0
    max_name = max(len(m["name"]) for m in metas)
    print(f"{'NAME':<{max_name}}  CATEGORY  UPDATED_AT")
    print("-" * (max_name + 30))
    for m in metas:
        cat = m.get("category") or "-"
        upd = (m.get("updated_at") or "")[:19].replace("T", " ")
        print(f"{m['name']:<{max_name}}  {cat:<8}  {upd}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    try:
        removed = secretstore.delete(args.name)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not removed:
        print(f"error: secret '{args.name}' not found", file=sys.stderr)
        return 1
    print(f"Deleted: {args.name}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    try:
        count = secretstore.import_env(args.file)
        print(f"Imported: {count} secret(s) from {args.file}")
        return 0
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="secret",
        description="Built-in encrypted secret store for claude-ops-bot.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Generate master key (run once)")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing key")
    p_init.set_defaults(func=cmd_init)

    # get
    p_get = sub.add_parser("get", help="Retrieve a secret value")
    p_get.add_argument("name", help="Secret name")
    p_get.set_defaults(func=cmd_get)

    # set
    p_set = sub.add_parser("set", help="Create or update a secret")
    p_set.add_argument("name", help="Secret name")
    p_set.add_argument("value", nargs="?", default=None, help="Value (omit to read from stdin)")
    p_set.set_defaults(func=cmd_set)

    # list
    p_list = sub.add_parser("list", help="List secret names and categories (no values)")
    p_list.set_defaults(func=cmd_list)

    # rm
    p_rm = sub.add_parser("rm", help="Delete a secret")
    p_rm.add_argument("name", help="Secret name")
    p_rm.set_defaults(func=cmd_rm)

    # import
    p_import = sub.add_parser("import", help="Import secrets from a .env or JSON file")
    p_import.add_argument("file", help="Path to .env-style or JSON file")
    p_import.set_defaults(func=cmd_import)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
