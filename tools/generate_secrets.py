#!/usr/bin/env python3
"""Generate administrator secrets for the DEMO server.

Run this yourself in a private terminal. The values are printed once to YOUR terminal only; copy them into
Codespaces Secrets (GitHub > Settings > Codespaces > Secrets) or an untracked .env file. Never commit them.

Usage:
    python3 tools/generate_secrets.py            # random password + key for the admin role
    python3 tools/generate_secrets.py --role proctor
    python3 tools/generate_secrets.py --prompt   # type your own password (hidden input)
"""
from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auth_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--role", choices=auth_config.ROLES, default="admin")
    parser.add_argument("--prompt", action="store_true", help="enter the password interactively instead of generating one")
    args = parser.parse_args()

    if args.prompt:
        password = getpass.getpass("새 비밀번호(12자 이상): ")
        if len(password) < 12 or password != getpass.getpass("한 번 더 입력: "):
            print("비밀번호가 짧거나 일치하지 않습니다.", file=sys.stderr)
            return 1
        shown_password = None
    else:
        password = secrets.token_urlsafe(18)
        shown_password = password
    prefix = f"EXAM_SAFE_{args.role.upper()}"
    print("# 아래 값을 Codespaces Secrets(또는 추적되지 않는 .env)에 입력하세요. 이 출력은 저장하거나 공유하지 마세요.")
    if shown_password:
        print(f"# 로그인 비밀번호(해시의 원본, 한 번만 표시): {shown_password}")
    print(f"{prefix}_PASSWORD_HASH={auth_config.hash_password(password)}")
    print(f"{prefix}_KEY={secrets.token_urlsafe(24)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
