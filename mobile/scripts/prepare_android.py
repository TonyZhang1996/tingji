"""Download only pinned public Android runtime libraries and models."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from urllib import request

ROOT = Path(__file__).resolve().parents[2]
MOBILE = ROOT / "mobile"
MANIFEST = MOBILE / "vendor-manifest.json"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def runtime_files():
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    allowed = {"android/app/libs/sherpa-onnx-2.0.1.aar",
               "android/app/src/main/assets/models/silero_vad.onnx",
               "android/app/src/main/assets/models/speaker_embedding.onnx"}
    rows = document["runtime"]
    if {row["path"] for row in rows} != allowed or len(rows) != len(allowed):
        raise ValueError("Unexpected Android runtime input.")
    for row in rows:
        if PurePosixPath(row["path"]).is_absolute() or ".." in PurePosixPath(row["path"]).parts:
            raise ValueError("Runtime path escaped its approved directory.")
        if not row["url"].startswith("https://github.com/k2-fsa/sherpa-onnx/releases/download/"):
            raise ValueError("Runtime must come from the pinned upstream release.")
    return rows


def verify_runtime():
    for row in runtime_files():
        path = MOBILE / row["path"]
        if not path.is_file() or path.stat().st_size != row["bytes"] or sha256(path) != row["sha256"]:
            raise ValueError("Missing or modified public runtime input: " + row["path"])


def license_files():
    rows = json.loads(MANIFEST.read_text(encoding="utf-8")).get("licenses", [])
    if not rows or len({row["name"] for row in rows}) != len(rows):
        raise ValueError("Pinned public license inventory is required.")
    for row in rows:
        name = PurePosixPath(row["name"])
        if len(name.parts) != 1 or name.suffix not in {".txt", ".md", ".json"} or "\\" in row["name"]:
            raise ValueError("Unexpected public license path.")
    return rows


def verify_licenses():
    root = MOBILE / "android/app/src/main/assets/licenses"
    rows = license_files()
    if {p.name for p in root.iterdir() if p.is_file()} != {row["name"] for row in rows}:
        raise ValueError("Public license folder differs from the reviewed inventory.")
    for row in rows:
        path = root / row["name"]
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row["bytes"] or sha256(path) != row["sha256"]:
            raise ValueError("Missing or modified public license input: " + row["name"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, default=ROOT / "work/android-build")
    parser.add_argument("--offline", action="store_true", help="Verify existing files without contacting the network")
    args = parser.parse_args()
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    downloads = work / "pinned-runtime"
    downloads.mkdir(exist_ok=True)
    for row in runtime_files():
        target = MOBILE / row["path"]
        if target.is_file() and target.stat().st_size == row["bytes"] and sha256(target) == row["sha256"]:
            continue
        if args.offline:
            raise SystemExit("Missing or modified runtime input: " + row["path"])
        cache = downloads / (row["name"] + ".download")
        if not cache.is_file() or cache.stat().st_size != row["bytes"] or sha256(cache) != row["sha256"]:
            part = cache.with_suffix(".part")
            with request.urlopen(request.Request(row["url"], headers={"User-Agent": "Tingji-Android-Build"}), timeout=120) as response, part.open("wb") as stream:
                shutil.copyfileobj(response, stream, length=1024 * 1024)
            if part.stat().st_size != row["bytes"] or sha256(part) != row["sha256"]:
                raise SystemExit("Public runtime download failed SHA256 validation.")
            part.replace(cache)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(MOBILE.resolve()):
            raise SystemExit("Unsafe runtime destination.")
        shutil.copyfile(cache, target)
    verify_runtime()
    verify_licenses()
    print("PASS: 3 public Android runtime inputs and pinned license notices verified; no personal configuration was read.")


if __name__ == "__main__":
    main()
