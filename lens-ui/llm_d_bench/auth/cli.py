"""Operator CLI for account recovery (design section 5.4).

Examples:
    python -m llm_d_bench.auth.cli create-admin --username admin
    python -m llm_d_bench.auth.cli reset-password --username admin
    python -m llm_d_bench.auth.cli list-users

Run it in the same environment as the backend (same ``LLM_D_BENCH_*`` database
settings). Stop the backend first when it owns an embedded PostgreSQL instance.
"""

from __future__ import annotations

import argparse
import sys

from llm_d_bench.auth.bootstrap import generate_password, write_operator_credentials
from llm_d_bench.auth.records import UserRoleBindingRecord
from llm_d_bench.auth.service import AuthService
from llm_d_bench.auth.settings import AuthSettings


def _service() -> AuthService:
    return AuthService(settings=AuthSettings.from_environment())


def _create_admin(args: argparse.Namespace) -> int:
    service = _service()
    if service.user_dao.get_by_username(args.username) is not None:
        print(f"user already exists: {args.username}", file=sys.stderr)
        return 1
    password = args.password or generate_password()
    user = service.create_user(
        username=args.username,
        password=password,
        display_name=args.display_name or args.username,
        must_change_password=True,
    )
    admin_role = service.require_role("admin")
    service.user_binding_dao.create(UserRoleBindingRecord(user_id=user.id, role_id=admin_role.id, scope_type="global"))
    service.user_dao.bump_principal_version(user.id)
    print(f"created admin '{user.username}'")
    if args.password is None:
        path = write_operator_credentials(user.username, password)
        print(f"generated credentials saved to {path} (mode 600)")
    print("change the password at first sign-in")
    return 0


def _reset_password(args: argparse.Namespace) -> int:
    service = _service()
    user = service.user_dao.get_by_username(args.username)
    if user is None:
        print(f"user not found: {args.username}", file=sys.stderr)
        return 1
    password = args.password or generate_password()
    service.reset_password(user.id, password)
    print(f"password reset for '{user.username}' (must change at next sign-in)")
    if args.password is None:
        path = write_operator_credentials(user.username, password)
        print(f"generated credentials saved to {path} (mode 600)")
    return 0


def _list_users(_args: argparse.Namespace) -> int:
    service = _service()
    for user in service.user_dao.list():
        print(f"{user.username}\t{user.status}\t{user.auth_source}\t{user.id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm_d_bench.auth.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-admin", help="create an administrator")
    create.add_argument("--username", default="admin")
    create.add_argument("--password", default=None, help="omit to auto-generate")
    create.add_argument("--display-name", default=None)
    create.set_defaults(func=_create_admin)

    reset = sub.add_parser("reset-password", help="reset a user's password and unlock it")
    reset.add_argument("--username", required=True)
    reset.add_argument("--password", default=None, help="omit to auto-generate")
    reset.set_defaults(func=_reset_password)

    listing = sub.add_parser("list-users", help="list users")
    listing.set_defaults(func=_list_users)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
