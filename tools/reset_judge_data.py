#!/usr/bin/env python3
"""Reset the judge sandbox to its initial virtual data (all virtual-candidate progress, tokens and sessions).

Deletes ONLY the judge database and judge photo folder. It refuses to run unless EXAM_SAFE_JUDGE_MODE=1, requires
--yes, and refuses the development database and the development photo folders. The server recreates and reseeds the
five virtual candidates (with generated avatars) on its next start.

    EXAM_SAFE_JUDGE_MODE=1 EXAM_SAFE_DB=/tmp/judge/exam_safe_pass_judge.sqlite3 \\
    EXAM_SAFE_PHOTO_DIR=/tmp/judge/photos python3 tools/reset_judge_data.py --yes
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="confirm the deletion")
    args = parser.parse_args()
    if os.environ.get("EXAM_SAFE_JUDGE_MODE", "") != "1":
        print("EXAM_SAFE_JUDGE_MODE=1 이 아니므로 실행하지 않습니다(개발 데이터 보호).", file=sys.stderr)
        return 2
    database_value, photo_value = os.environ.get("EXAM_SAFE_DB"), os.environ.get("EXAM_SAFE_PHOTO_DIR")
    if not database_value or not photo_value:
        print("EXAM_SAFE_DB 와 EXAM_SAFE_PHOTO_DIR 를 심사용 경로로 지정해야 합니다.", file=sys.stderr)
        return 2
    database, photos = Path(database_value).resolve(), Path(photo_value).resolve()
    protected = [ROOT / "exam_safe_pass.sqlite3", ROOT / "images", ROOT / "demo_photos"]
    if database in [p.resolve() for p in protected] or photos in [p.resolve() for p in protected] or ROOT in photos.parents:
        print("개발용 DB/사진 경로는 삭제할 수 없습니다.", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"삭제 대상: {database}* 및 {photos}\n--yes 를 붙여 다시 실행하면 삭제합니다.")
        return 1
    removed = 0
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(database) + suffix)
        if target.is_file():
            target.unlink()
            removed += 1
    if photos.is_dir():
        shutil.rmtree(photos)
        removed += 1
    print(f"심사용 데이터를 초기화했습니다({removed}개 항목). 서버를 재시작하면 가상 응시자 5명이 다시 만들어집니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
