"""Build a Windows installer from reviewed source and pinned redistributables.

No personal environment, settings, recordings or result directory is copied.
Run from the repository after staging/committing the reviewed build sources.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys

from prepare_vendor_manifest import SPEAKER_FILES, WEBVIEW, digest

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = {"desktop.py", "assets/tingji.ico", "assets/tingji.png", "assets/tingji.svg", "web/dist/index.html"}
FORBIDDEN = {"settings.json", "analysis.json", "checkpoint.json", "speaker_names.json", "review_annotations.json",
             ".git", ".runtime", ".venv", "venv", "work", "转写结果", "人物试听片段", ".ai_summary_cache"}
MEDIA = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".amr", ".mp4", ".mkv", ".mov", ".avi", ".webm"}
CREDENTIAL = re.compile(rb"(?<![\w-])(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})(?![\w-])")
SENSITIVE_ENV = re.compile(r"api.?key|token|secret|password|credential", re.I)


def run(command, *, env=None):
    subprocess.run([str(part) for part in command], check=True, cwd=ROOT, env=env)


def git(*args):
    return subprocess.check_output(["git", "-c", "core.quotepath=false", *args], cwd=ROOT)


def source_allowed(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return (name in SOURCE_ROOTS or len(parts) == 2 and parts[0] == "meeting_tool" and name.endswith(".py")
            or bool(re.fullmatch(r"web/dist/assets/index-[A-Za-z0-9_-]+\.(js|css)", name)))


def reject_link(path: Path, root: Path):
    if not path.resolve(strict=True).is_relative_to(root):
        raise ValueError("Input escaped its approved root.")
    current = path
    while current != root:
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("Build inputs must not be symlinks or directory junctions.")
        current = current.parent


def read_vendor(root: Path):
    document = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or not isinstance(document.get("files"), list):
        raise ValueError("Invalid vendor manifest.")
    seen = set()
    for row in document["files"]:
        name = row.get("path", "")
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or name in seen:
            raise ValueError("Invalid or duplicate vendor path.")
        valid = (row.get("role") == "speaker" and name.startswith("3d-speaker/") and name[11:] in SPEAKER_FILES
                 or row.get("role") == "prerequisite" and name == WEBVIEW
                 or row.get("role") == "license" and len(path.parts) == 2 and path.parts[0] == "licenses" and path.suffix.lower() in {".txt", ".md"}
                 or row.get("role") == "ffmpeg" and len(path.parts) == 3 and path.parts[:2] == ("ffmpeg", "bin")
                 and (path.name in {"ffmpeg.exe", "ffprobe.exe"} or path.suffix.lower() == ".dll"))
        if not valid:
            raise ValueError("Vendor manifest contains a non-allowlisted input.")
        file = root / path
        reject_link(file, root)
        if not file.is_file() or file.stat().st_size != row["bytes"] or digest(file) != row["sha256"]:
            raise ValueError("A vendor file no longer matches its reviewed hash.")
        seen.add(name)
    required = {"3d-speaker/" + name for name in SPEAKER_FILES} | {WEBVIEW, "ffmpeg/bin/ffmpeg.exe", "ffmpeg/bin/ffprobe.exe"}
    if not required <= seen or not any(row["role"] == "license" for row in document["files"]):
        raise ValueError("Required redistributables or notices are missing.")
    return document


def clean_environment(work: Path, python: Path):
    # Child build hooks must not inherit credentials, user's site packages or
    # application-specific paths. The environment itself is never serialized.
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}}
    profile = work / "build-profile"
    for folder in (profile, profile / "AppData/Local", profile / "AppData/Roaming", work / "temp"):
        folder.mkdir(parents=True, exist_ok=True)
    env.update(USERPROFILE=str(profile), APPDATA=str(profile / "AppData/Roaming"),
               LOCALAPPDATA=str(profile / "AppData/Local"), TEMP=str(work / "temp"), TMP=str(work / "temp"),
               PYTHONNOUSERSITE="1", PYTHONUTF8="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    system = Path(env.get("SYSTEMROOT", r"C:\Windows"))
    env["PATH"] = os.pathsep.join((str(python.parent), str(system / "System32"), str(system)))
    return env


def audit_tree(root: Path, *, secret_values=()):
    rows = []
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        reject_link(file, root)
        relative = file.relative_to(root).as_posix()
        if (any(part in FORBIDDEN for part in file.relative_to(root).parts) or file.name.startswith(".env")
                or file.suffix.lower() in MEDIA or file.name.startswith(("AI总结.", "完整转写."))):
            raise ValueError(f"Private or media path in build: {relative}")
        needles = [value.encode("utf-8") for value in secret_values if len(value) >= 12]
        needles += [value.encode("utf-16le") for value in secret_values if len(value) >= 12]
        tail = b""
        with file.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                data = tail + chunk
                if CREDENTIAL.search(data) or any(needle in data for needle in needles):
                    raise ValueError(f"Credential-shaped data in build: {relative}")
                tail = data[-4096:]
        rows.append({"path": relative, "bytes": file.stat().st_size, "sha256": digest(file)})
    return rows


def write_version(path: Path, version: str):
    numbers = tuple(int(x) for x in version.split(".")) + (0,)
    text = f'''VSVersionInfo(ffi=FixedFileInfo(filevers={numbers!r}, prodvers={numbers!r}, mask=0x3f, flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0,0)), kids=[StringFileInfo([StringTable('040904B0', [StringStruct('CompanyName','Tingji'), StringStruct('FileDescription','听记 · 会议转写工作台'), StringStruct('FileVersion','{version}'), StringStruct('ProductName','听记'), StringStruct('ProductVersion','{version}'), StringStruct('OriginalFilename','Tingji.exe')])]), VarFileInfo([VarStruct('Translation',[1033,1200])])])'''
    path.write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--work-dir", type=Path, default=ROOT / "work/windows-installer")
    parser.add_argument("--python", type=Path)
    parser.add_argument("--iscc", required=True, type=Path)
    parser.add_argument("--stage-only", action="store_true")
    parser.add_argument("--skip-freeze", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        raise SystemExit("Version must have three numeric components.")
    work = args.work_dir.resolve()
    intended = (ROOT / "work").resolve()
    if work == intended or not work.is_relative_to(intended):
        raise SystemExit("Build work directory must be a dedicated subdirectory of this repository's work directory.")
    work.mkdir(parents=True, exist_ok=True)
    python = (args.python or work / "build-venv/Scripts/python.exe").resolve(strict=True)
    if not python.is_relative_to(work / "build-venv"):
        raise SystemExit("Use the dedicated clean build-venv interpreter, never an existing user environment.")
    cfg = work / "build-venv/pyvenv.cfg"
    if not cfg.is_file() or re.search(r"include-system-site-packages\s*=\s*true", cfg.read_text(encoding="utf-8"), re.I):
        raise SystemExit("The build venv must not include system site packages.")
    vendor = (work / "vendor").resolve(strict=True)
    manifest = read_vendor(vendor)
    stage = work / "stage"
    # This is the only recursive removal, and its exact resolved scope has been
    # checked against the designated workspace immediately before use.
    if stage.exists():
        if stage.resolve() != work / "stage" or not stage.resolve().is_relative_to(intended):
            raise SystemExit("Unsafe staging directory.")
        shutil.rmtree(stage)
    stage.mkdir()
    tracked = set(git("ls-files", "-z").decode("utf-8").split("\0"))
    source = sorted(name for name in tracked if source_allowed(name))
    if not SOURCE_ROOTS <= set(source) or "meeting_tool/runtime_paths.py" not in source:
        raise SystemExit("Stage the reviewed runtime and assets in Git before building.")
    input_rows = []
    for name in source:
        file = ROOT / name
        reject_link(file, ROOT)
        target = stage / "app" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)
        input_rows.append({"path": name, "sha256": digest(file)})
    for row in manifest["files"]:
        target = stage / "vendor" / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(vendor / row["path"], target)
    write_version(stage / "version_info.txt", args.version)
    (stage / "build-inputs.json").write_text(json.dumps({"version": args.version,
        "source_commit": git("rev-parse", "HEAD").decode().strip(), "source_files": input_rows,
        "vendor_files": manifest["files"], "spec_sha256": digest(ROOT / "scripts/windows/tingji.spec")},
        ensure_ascii=False, indent=2), encoding="utf-8")
    secret_values = tuple(value for key, value in os.environ.items() if SENSITIVE_ENV.search(key))
    audit_tree(stage / "app", secret_values=secret_values)
    if args.stage_only:
        print(f"Prepared reviewed staging input: {len(source)} source files.")
        return
    env = clean_environment(work, python)
    env["TINGJI_BUILD_STAGE"] = str(stage)
    bundle = work / "dist/Tingji"
    receipt = work / "dist/Tingji-build-inputs.json"
    if not args.skip_freeze:
        run([python, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", work / "dist",
             "--workpath", work / "build", ROOT / "scripts/windows/tingji.spec"], env=env)
        shutil.copyfile(stage / "build-inputs.json", receipt)
    elif not receipt.is_file() or receipt.read_bytes() != (stage / "build-inputs.json").read_bytes():
        raise SystemExit("The existing frozen bundle does not match the reviewed inputs; run a full freeze.")
    if not (bundle / "Tingji.exe").is_file() or not (bundle / "TingjiSpeaker.exe").is_file():
        raise SystemExit("The frozen UI or speaker worker is missing.")
    inventory = audit_tree(bundle, secret_values=secret_values)
    release = work / "release"
    release.mkdir(exist_ok=True)
    (release / "Tingji-file-manifest.json").write_text(json.dumps({"version": args.version, "files": inventory},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    notices = sorted((stage / "vendor/licenses").glob("*.txt")) + sorted((stage / "vendor/licenses").glob("*.md"))
    combined = "\n\n".join("=" * 72 + "\n" + p.name + "\n" + "=" * 72 + "\n" + p.read_text(encoding="utf-8-sig") for p in notices)
    (release / "THIRD-PARTY-NOTICES.txt").write_text(combined, encoding="utf-8")
    run([args.iscc.resolve(strict=True), "/DAppVersion=" + args.version, "/DBundleDir=" + str(bundle),
         "/DVendorDir=" + str(stage / "vendor"), "/DReleaseDir=" + str(release), ROOT / "scripts/windows/tingji.iss"], env=env)
    setup = release / f"Tingji-Setup-{args.version}-windows-x64.exe"
    checksums = [f"{digest(p)}  {p.name}" for p in (setup, release / "THIRD-PARTY-NOTICES.txt", release / "Tingji-file-manifest.json")]
    (release / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="ascii")
    print(f"Installer built and audited: {setup.name}; {len(inventory)} bundled files.")


if __name__ == "__main__":
    main()
