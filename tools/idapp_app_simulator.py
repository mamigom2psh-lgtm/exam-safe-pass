#!/usr/bin/env python3
"""Reference client for the ID·DEMO app side of the 안심패스 link (what the Android app has to do).

Usage: python3 tools/idapp_app_simulator.py --base-url https://<host> --code ABCD2345

Exchanges the one-time pairing code shown on the kiosk for a 30 s DEMO ID token and prints it as QR text. It holds no
app key and sends no personal data. Plain HTTP is accepted only for localhost development.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.hostname:
        return value.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS:
        return value.rstrip("/")
    raise ValueError("HTTPS 주소만 허용됩니다(로컬 개발용 localhost 제외).")


def request_token(base_url: str, pairing_code: str, timeout: float = 10.0) -> dict:
    base = validate_base_url(base_url)
    request = urllib.request.Request(
        f"{base}/api/idapp/token", data=json.dumps({"pairing_code": pairing_code}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("error", "")
        except ValueError:
            message = ""
        raise RuntimeError(f"연동 실패({error.code}): {message} 인증되지 않았습니다.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("서버에 연결할 수 없습니다. 인증되지 않았습니다.") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--code", required=True, help="kiosk pairing code")
    args = parser.parse_args()
    try:
        result = request_token(args.base_url, args.code)
    except (ValueError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    print(result["qr_payload"])
    print(f"# DEMO 전용 · {result['expires_in']}초 유효 · 1회용 · 실제 신분 인증 아님", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
