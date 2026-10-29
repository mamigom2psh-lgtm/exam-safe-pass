#!/usr/bin/env python3
"""Build a clean, history-free export of the judge-only deployment.

Only an explicit whitelist of files is copied - never the working tree wholesale and never Git history. The result is
audited (no photos, databases, .env files, backups, Git metadata, development credentials or personal data patterns)
and can optionally be turned into a brand-new local repository with ONE commit. Nothing is pushed or deployed.

    python3 tools/export_judge_repo.py /path/outside/this/repo/exam-safe-pass-judge [--git-init]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import auth_config  # noqa: E402

FILES = [
    "server.py", "auth_config.py", "judge_mode.py", "idapp_link.py", "session_store.py", "demo_candidates.py", "demo_scenarios.py",
    "mobile_id_provider.py", "self_identity_provider.py", "verification_policy.py", "kiosk.html", "index.html",
    "judge_login.html", "face_similarity.js", "requirements.txt", "render.yaml", ".env.example", ".gitignore",
    "docs/RENDER_DEPLOY.md", "docs/JUDGE_CHECKLIST.md", "docs/PRIVACY.md", "docs/IDAPP_INTEGRATION.md", "docs/openapi-idapp.yaml",
]
README = """# 시험장 안심패스 플러스 (EXAM SAFE PASS+) — 심사위원 전용 DEMO

공모전 심사용 시제품입니다. 가상 응시자 5명과 합성 프로필만 사용하며 공식 신분 인증이 아니고 실제 입실에 사용할 수 없습니다.
배포 방법은 docs/RENDER_DEPLOY.md, 점검표는 docs/JUDGE_CHECKLIST.md 를 참고하세요.
"""
# Photos/DBs/secrets are rejected everywhere; the bundled vendor/ tree (library + model weights) is the only binary area.
FORBIDDEN_NAME = re.compile(
    r"(\.(jpe?g|gif|webp|bmp|sqlite3?|db|npy|pem|key)$)|(^|/)(images|demo_photos|\.git|tests)(/|$)|_backup\.", re.I
)
FORBIDDEN_ENV = re.compile(r"(^|/)\.env(\.[^/]*)?$")
FORBIDDEN_TEXT = [
    re.compile(re.escape(value)) for value in (*auth_config.KNOWN_WEAK_PASSWORDS[:4], *auth_config.KNOWN_WEAK_KEYS[:4])
]
PERSONAL = [re.compile(r"\b\d{6}-[1-4]\d{6}\b"), re.compile(r"(?i)data:image/(jpeg|png);base64,[A-Za-z0-9+/]{200,}")]
SCANNED_SUFFIXES = {".py", ".html", ".js", ".md", ".yaml", ".yml", ".txt", ".example", ".json", ""}


def audit(out: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(p for p in out.rglob("*") if p.is_file() and ".git" not in p.relative_to(out).parts):
        rel = path.relative_to(out).as_posix()
        if FORBIDDEN_NAME.search(rel) or (FORBIDDEN_ENV.search(rel) and rel != ".env.example"):
            problems.append(f"금지된 파일: {rel}")
            continue
        if rel.startswith("vendor/human/models/") or path.suffix not in SCANNED_SUFFIXES or path.stat().st_size > 3_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            problems.append(f"텍스트로 읽을 수 없는 파일: {rel}")
            continue
        if rel != "auth_config.py":  # its deny-list legitimately names the rejected placeholder values
            problems += [f"개발용 기본 비밀값 문자열 포함: {rel}" for pattern in FORBIDDEN_TEXT if pattern.search(text)]
        problems += [f"개인정보/이미지 패턴 포함: {rel}" for pattern in PERSONAL if pattern.search(text)]
    return problems


def export(out: Path) -> None:
    for name in FILES:
        source = ROOT / name
        if not source.is_file():
            raise SystemExit(f"필수 파일이 없습니다: {name}")
        (out / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, out / name)
    for source in sorted((ROOT / "vendor").rglob("*")):
        rel = source.relative_to(ROOT).as_posix()
        if source.is_file() and not FORBIDDEN_NAME.search(rel):
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, out / rel)
    (out / "README.md").write_text(README, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("output", type=Path)
    parser.add_argument("--git-init", action="store_true", help="create a new local repo with a single commit (never pushes)")
    args = parser.parse_args()
    out = args.output.resolve()
    if out == ROOT or ROOT in out.parents:
        print("출력 경로는 개발 저장소 밖이어야 합니다.", file=sys.stderr)
        return 2
    if out.exists() and any(out.iterdir()):
        print("출력 폴더가 비어 있지 않습니다. 새 폴더를 지정하세요(기존 내용은 삭제하지 않습니다).", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    export(out)
    problems = audit(out)
    if problems:
        print("내보내기 감사 실패:", *problems, sep="\n  - ", file=sys.stderr)
        return 1
    count = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"내보내기 완료: {out} ({count}개 파일, Git 이력 없음, 감사 통과)")
    if args.git_init:
        identity = {"GIT_AUTHOR_NAME": "judge-export", "GIT_AUTHOR_EMAIL": "judge-export@example.invalid",
                    "GIT_COMMITTER_NAME": "judge-export", "GIT_COMMITTER_EMAIL": "judge-export@example.invalid"}
        env = {**os.environ, **identity}
        for command in (["git", "init", "-q", "-b", "main"], ["git", "add", "--", "."],
                        ["git", "commit", "-q", "-m", "Judge-only DEMO export (no history, no real photos)"]):
            subprocess.run(command, cwd=out, env=env, check=True)
        print("새 로컬 저장소를 만들고 커밋 1개를 생성했습니다. 원격 등록·push 는 하지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
