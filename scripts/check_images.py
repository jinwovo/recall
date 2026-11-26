#!/usr/bin/env python3
"""Fail if any container image this repo pulls can no longer be resolved from its registry,
or if the same image is pinned to different versions in different places.

Why: minio/minio was withdrawn from Docker Hub and quay.io. Runners with a warm local cache
kept passing, while the nightly tune workflow failed at `docker compose up` for two and a half
months before anyone looked. This script asks each registry directly (no pull, no local cache).
It runs on every change to an image reference and weekly, so a withdrawn upstream image shows
up as one clear failure instead of a stack that will not start.

Sources:
  * docker compose services that use a registry image (services with `build:` are skipped)
  * FROM lines of every Dockerfile (build-stage aliases are skipped)
  * string literals in backend/src/test/java/com/portfolio/recall/TestImages.java

Standard library only. Needs `docker` with compose and buildx, both present on GitHub runners.

    python3 scripts/check_images.py            # check everything
    python3 scripts/check_images.py --list     # only print what would be checked
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILES = ["docker-compose.yml", "docker-compose.ci.yml"]
TEST_IMAGES = ROOT / "backend/src/test/java/com/portfolio/recall/TestImages.java"
SKIP_DIRS = {"node_modules", ".git", "build", ".next", ".gradle"}

# registry/repo[:tag][@sha256:digest]. Needs a tag, a digest or a slash, so plain
# identifiers in the Java file do not count as images.
IMAGE_LITERAL = re.compile(
    r'"([a-z0-9][a-z0-9._/-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?)"'
)


def compose_images() -> dict[str, str]:
    args = ["docker", "compose"]
    for f in COMPOSE_FILES:
        if (ROOT / f).exists():
            args += ["-f", f]
    args += ["--profile", "*", "config", "--format", "json"]
    out = subprocess.run(args, cwd=ROOT, check=True, capture_output=True, text=True).stdout
    services = json.loads(out)["services"]
    return {
        svc["image"]: f"compose service '{name}'"
        for name, svc in services.items()
        if svc.get("image") and "build" not in svc
    }


def dockerfile_images() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in ROOT.rglob("Dockerfile*"):
        if SKIP_DIRS.intersection(path.relative_to(ROOT).parts):
            continue
        stages: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line, re.I)
            if not m:
                continue
            ref, alias = m.group(1), m.group(2)
            if alias:
                stages.add(alias.lower())
            if ref.lower() in stages or ref == "scratch":
                continue
            if "$" in ref:
                print(f"warning: {path.relative_to(ROOT)}: FROM {ref} uses a build arg, not checked")
                continue
            found[ref] = str(path.relative_to(ROOT).as_posix())
    return found


def test_images() -> dict[str, str]:
    if not TEST_IMAGES.exists():
        return {}
    text = TEST_IMAGES.read_text(encoding="utf-8")
    # asCompatibleSubstituteFor("minio/minio") names the image a substitute stands in for.
    # Testcontainers never pulls it, so it is not checked.
    text = re.sub(r'asCompatibleSubstituteFor\(\s*"[^"]*"\s*\)', "", text)
    refs = [r for r in IMAGE_LITERAL.findall(text) if "/" in r or ":" in r or "@" in r]
    return {r: TEST_IMAGES.relative_to(ROOT).as_posix() for r in refs}


def repo_of(ref: str) -> str:
    name = ref.split("@", 1)[0]
    head, _, last = name.rpartition("/")
    return (head + "/" if head else "") + last.split(":", 1)[0]


def drift(sources: list[dict[str, str]]) -> list[str]:
    """Repos referenced at different versions in different places, e.g. compose vs tests."""
    by_repo: dict[str, set[str]] = {}
    for source in sources:
        for ref in source:
            by_repo.setdefault(repo_of(ref), set()).add(ref)
    return [f"{repo}: {' vs '.join(sorted(refs))}" for repo, refs in sorted(by_repo.items()) if len(refs) > 1]


def resolves(ref: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", ref],
        capture_output=True, text=True, timeout=120,
    )
    detail = (proc.stderr or proc.stdout).strip().splitlines()
    return proc.returncode == 0, (detail[-1] if detail else "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="print the images and exit")
    args = ap.parse_args()

    sources = [compose_images(), dockerfile_images(), test_images()]
    images: dict[str, str] = {}
    for source in sources:
        for ref, where in source.items():
            images.setdefault(ref, where)

    if not images:
        print("error: found no images. The parsing is broken, not the registries.")
        return 2

    width = max(map(len, images))
    if args.list:
        for ref, where in sorted(images.items()):
            print(f"{ref:<{width}}  {where}")
        return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(zip(images, pool.map(resolves, images)))

    missing = 0
    for ref, where in sorted(images.items()):
        ok, detail = results[ref]
        mutable = "@sha256:" not in ref and (ref.endswith(":latest") or ":" not in ref.rsplit("/", 1)[-1])
        tag = "ok     " if ok else "MISSING"
        note = "  (mutable tag)" if ok and mutable else ""
        print(f"{tag} {ref:<{width}}  {where}{note}")
        if not ok:
            missing += 1
            print(f"        {detail}")

    drifted = drift(sources)
    for line in drifted:
        print(f"DRIFT   {line}")

    if missing:
        print(f"\n{missing} image(s) can no longer be pulled. Replace or re-pin them before the stack breaks.")
    if drifted:
        print(f"\n{len(drifted)} image(s) are pinned differently across compose, Dockerfiles and "
              "TestImages.java. Tests and the stack would run different builds. Align them.")
    if missing or drifted:
        return 1
    print(f"\nall {len(images)} images resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
