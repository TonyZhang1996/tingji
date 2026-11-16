"""Pin an explicitly curated redistributable directory; never scan app data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

SPEAKER_FILES = {
    "speakerlab/__init__.py", "speakerlab/models/__init__.py",
    "speakerlab/models/campplus/__init__.py", "speakerlab/models/campplus/DTDNN.py",
    "speakerlab/models/campplus/layers.py",
    "pretrained_models/campplus/campplus_cn_en_common.pt",
}
WEBVIEW = "prerequisites/MicrosoftEdgeWebView2RuntimeInstallerX64.exe"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-dir", required=True, type=Path)
    args = parser.parse_args()
    root = args.vendor_dir.resolve(strict=True)
    chosen = [("3d-speaker/" + name, "speaker") for name in sorted(SPEAKER_FILES)]
    ffmpeg = root / "ffmpeg" / "bin"
    chosen += [("ffmpeg/bin/ffmpeg.exe", "ffmpeg"), ("ffmpeg/bin/ffprobe.exe", "ffmpeg")]
    chosen += [(p.relative_to(root).as_posix(), "ffmpeg") for p in sorted(ffmpeg.glob("*.dll"))]
    licenses = sorted(p for p in (root / "licenses").iterdir()
                      if p.is_file() and p.suffix.lower() in {".txt", ".md"})
    if not licenses:
        raise SystemExit("Missing reviewed third-party license files.")
    chosen += [(p.relative_to(root).as_posix(), "license") for p in licenses]
    chosen.append((WEBVIEW, "prerequisite"))
    entries = []
    for name, role in chosen:
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or path.is_symlink() or not path.is_file():
            raise SystemExit("Vendor input escaped the curated directory.")
        entries.append({"path": name, "role": role, "bytes": path.stat().st_size, "sha256": digest(path)})
    target = root / "manifest.json"
    target.write_text(json.dumps({"schema_version": 1, "files": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Pinned {len(entries)} curated vendor files; review manifest.json before building.")


if __name__ == "__main__":
    main()
