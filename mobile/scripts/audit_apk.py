"""Audit an APK without printing credentials or personal file contents."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
from zipfile import ZipFile, ZIP_STORED

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "mobile/vendor-manifest.json"
MEDIA = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".amr", ".mp4", ".mkv", ".webm"}
PRIVATE = {"settings.json", "job.json", "analysis.json", "checkpoint.json", "speaker_names.json", "review_annotations.json",
           "summary.json", "summary.md", "summary.txt", "transcript.json", "transcript.txt", "segments.json",
           "转写结果", "人物试听片段", ".ai_summary_cache", "shared_exports", "speaker-embeddings.bin", "speech-regions.json"}
CREDENTIAL = re.compile(rb"(?<![\w-])(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})(?![\w-])")
ART_PROFILES = {"assets/dexopt/baseline.prof": b"pro\x00", "assets/dexopt/baseline.profm": b"prm\x00"}


def elf_alignment(data: bytes):
    if len(data) < 64 or not data.startswith(b"\x7fELF") or data[4] != 2 or data[5] != 1:
        raise ValueError("Expected a 64-bit little-endian native library.")
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    if size < 56 or count < 1 or offset + count * size > len(data):
        raise ValueError("Invalid native library program headers.")
    loads = []
    for index in range(count):
        row = struct.unpack_from("<IIQQQQQQ", data, offset + index * size)
        if row[0] == 1:
            if row[7] < 16384 or row[2] % 16384 != row[3] % 16384:
                raise ValueError("Native library is not compatible with 16 KiB memory pages.")
            loads.append(row[7])
    if not loads: raise ValueError("Native library has no loadable segments.")
    return loads


def manifest_info(apk: Path, sdk: Path):
    executable = sdk / "build-tools/35.0.0" / ("aapt2.exe" if os.name == "nt" else "aapt2")
    if not executable.is_file(): raise ValueError("Android SDK build-tools 35.0.0 is required for manifest verification.")
    result = subprocess.run([str(executable), "dump", "xmltree", str(apk), "--file", "AndroidManifest.xml"], check=True, capture_output=True, text=True, encoding="utf-8")
    text = result.stdout
    # aapt2 renders boolean values as either true/false or 0xffffffff/0x0.
    def enabled(name):
        rows = [line for line in text.splitlines() if re.search(r"(?:android:)?" + re.escape(name) + r"\(", line)]
        return any(re.search(r"(?:true|0xffffffff)\b", line) for line in rows)
    permissions = set()
    current_element = ""
    for line in text.splitlines():
        element = re.search(r"E: ([A-Za-z0-9_-]+)", line)
        if element:
            current_element = element.group(1)
        if current_element in {"uses-permission", "uses-permission-sdk-23"}:
            permissions.update(re.findall(r'android\.permission\.[A-Z_]+', line))
    return {"debuggable": enabled("debuggable"), "backup_enabled": enabled("allowBackup") or "allowBackup(" not in text,
            "cleartext_enabled": enabled("usesCleartextTraffic"), "permissions": sorted(permissions),
            "network_security_config": "networkSecurityConfig(" in text}


def audit(apk: Path, sdk: Path, require_release: bool):
    pinned = json.loads(MANIFEST.read_text(encoding="utf-8"))
    models = {row["apk_path"]: row for row in pinned["runtime"] if row.get("apk_path")}
    native = {row["path"]: row for row in pinned["native_libraries"]}
    licenses = {"assets/licenses/" + row["name"]: row for row in pinned.get("licenses", [])}
    secret_values = [value for key, value in os.environ.items() if re.search(r"api.?key|token|secret|password|credential", key, re.I) and len(value) >= 12]
    needles = [value.encode(encoding) for value in secret_values for encoding in ("utf-8", "utf-16le", "utf-16be")]
    rows = []; found = set(); abis = set(); library_count = 0
    with ZipFile(apk) as archive, apk.open("rb") as raw:
        for info in archive.infolist():
            if info.is_dir(): continue
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename or info.filename in found:
                raise ValueError("Unsafe or duplicate APK archive entry.")
            found.add(info.filename)
            if path.suffix.lower() in MEDIA or any(part in PRIVATE for part in path.parts) or path.name.startswith((".env", "AI总结.", "完整转写.")) or path.suffix.lower() in {".key", ".pem", ".jks", ".keystore", ".p12", ".pfx", ".db", ".sqlite"}:
                raise ValueError("Private data or signing material in APK: " + info.filename)
            data = archive.read(info)
            if CREDENTIAL.search(data) or any(value in data for value in needles):
                raise ValueError("Credential data in APK; matching values were not printed: " + info.filename)
            if require_release and path.suffix == ".dex" and b"run_self_check" in data:
                raise ValueError("Debug self-check entry remains in release code.")
            sha = hashlib.sha256(data).hexdigest()
            expected = models.get(info.filename) or native.get(info.filename) or licenses.get(info.filename)
            if expected and (sha != expected["sha256"] or len(data) != expected["bytes"]):
                raise ValueError("A packaged public component differs from its pinned hash: " + info.filename)
            if info.filename.startswith("assets/models/") and info.filename not in models:
                raise ValueError("Unreviewed model in APK.")
            if info.filename.startswith("assets/licenses/") and info.filename not in licenses:
                raise ValueError("Unreviewed notice in APK.")
            # AGP generates these two ART optimization profiles from reviewed
            # bytecode. They are not imported files or application data.
            if info.filename in ART_PROFILES and (not data.startswith(ART_PROFILES[info.filename]) or len(data) > 2 * 1024 * 1024):
                raise ValueError("Invalid generated Android optimization profile.")
            if info.filename.startswith("assets/") and info.filename not in models and info.filename not in licenses and info.filename not in ART_PROFILES and not re.fullmatch(r"assets/web/(?:index\.html|assets/index-[A-Za-z0-9_-]+\.(?:js|css))", info.filename):
                raise ValueError("Unreviewed application asset in APK: " + info.filename)
            if info.filename.startswith("lib/"):
                if info.filename not in native: raise ValueError("Unreviewed native library in APK.")
                abis.add(path.parts[1]); elf_alignment(data); library_count += 1
                if info.compress_type != ZIP_STORED: raise ValueError("Native libraries must remain uncompressed for direct Android loading.")
                raw.seek(info.header_offset + 26)
                name_length, extra_length = struct.unpack("<HH", raw.read(4))
                data_offset = info.header_offset + 30 + name_length + extra_length
                if data_offset % 16384: raise ValueError("Native APK entry is not 16 KiB ZIP aligned.")
            rows.append({"path": info.filename, "bytes": len(data), "sha256": sha})
    if not set(models) <= found or not set(licenses) <= found: raise ValueError("A required public model or notice is missing.")
    if "arm64-v8a" not in abis: raise ValueError("The APK does not include the vivo X300 arm64 target.")
    for abi in abis:
        if not {name for name in native if name.startswith("lib/" + abi + "/")} <= found:
            raise ValueError("A required native library is missing.")
    if not {"assets/web/index.html", "classes.dex", "AndroidManifest.xml"} <= found:
        raise ValueError("Required application content is missing.")
    manifest = manifest_info(apk, sdk)
    if require_release and (manifest["debuggable"] or manifest["cleartext_enabled"] or manifest["network_security_config"]):
        raise ValueError("Release manifest contains debugging or cleartext networking settings.")
    if manifest["backup_enabled"]: raise ValueError("Private application backups must be disabled.")
    prohibited = {"android.permission.RECORD_AUDIO", "android.permission.MANAGE_EXTERNAL_STORAGE", "android.permission.READ_CONTACTS", "android.permission.ACCESS_FINE_LOCATION", "android.permission.ACCESS_BACKGROUND_LOCATION"}
    if prohibited.intersection(manifest["permissions"]): raise ValueError("Unexpected private-data permission in APK.")
    if require_release:
        signer = sdk / "build-tools/35.0.0" / ("apksigner.bat" if os.name == "nt" else "apksigner")
        signed = subprocess.run([str(signer), "verify", "--print-certs", str(apk)], capture_output=True, text=True, check=True)
        if "CN=Android Debug" in signed.stdout or "CN=AndroidDebug" in signed.stdout:
            raise ValueError("A release must not use the public Android debug signing key.")
    with apk.open("rb") as stream: apk_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"ok": True, "apk": apk.name, "bytes": apk.stat().st_size, "sha256": apk_hash, "release_required": require_release,
            "abis": sorted(abis), "native_libraries": library_count, "entries": len(rows), "actual_credentials_checked": len(secret_values), "manifest": manifest, "files": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("apk", type=Path)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--require-release", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.apk.resolve(strict=True), args.sdk.resolve(strict=True), args.require_release)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print("FAIL APK audit:", str(error) if isinstance(error, ValueError) else type(error).__name__)
        raise SystemExit(1)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS APK audit:", result["apk"], result["entries"], "entries; private-data, credentials, runtime hashes and 16 KiB native alignment checked.")


if __name__ == "__main__": main()
