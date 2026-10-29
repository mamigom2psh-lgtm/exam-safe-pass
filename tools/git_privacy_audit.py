#!/usr/bin/env python3
"""Read-only audit: do any Git refs contain face photos, DBs or embeddings?

Never modifies the repository. Prints only paths and ref names (no file contents).
"""
import re, subprocess, sys

SENSITIVE = re.compile(r"(\.(jpe?g|png|webp|gif|bmp|sqlite3?|db|npy|embedding)$)|(^|/)images/", re.I)
ALLOWED = re.compile(r"^(vendor/|docs/|tests/fixtures/)")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def sensitive_paths(ref):
    paths = git("ls-tree", "-r", "--name-only", ref).splitlines()
    return [p for p in paths if SENSITIVE.search(p) and not ALLOWED.match(p)]


def audit():
    report = {"tracked": [p for p in git("ls-files").splitlines()
                          if SENSITIVE.search(p) and not ALLOWED.match(p)],
              "refs": {}}
    refs = [r.split()[0] for r in git("for-each-ref", "--format=%(refname)").splitlines()]
    for ref in refs:
        found = sensitive_paths(ref)
        if found:
            report["refs"][ref] = found
    return report


if __name__ == "__main__":
    result = audit()
    print("tracked sensitive files:", len(result["tracked"]))
    origin = {k: v for k, v in result["refs"].items() if k.startswith("refs/remotes/") or k.startswith("refs/heads/")}
    print("branch/remote refs with sensitive files:", len(origin))
    local_only = {k: v for k, v in result["refs"].items() if k not in origin}
    print("other (e.g. host checkpoint) refs with sensitive files:", len(local_only))
    for ref in sorted(result["refs"]):
        print(" ", ref, len(result["refs"][ref]), "file(s)")
    sys.exit(1 if result["tracked"] or origin else 0)
