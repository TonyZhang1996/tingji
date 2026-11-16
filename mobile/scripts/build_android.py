"""Build reviewed Android sources in a fresh, isolated staging directory.

Example (Windows, an ASCII drive mapped to a dedicated work directory):
  python mobile/scripts/build_android.py --java-home JAVA17 --sdk SDK36 --stage-dir T:/stages --variant release
Signing is deliberately separate; never put signing passwords or API keys here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from prepare_android import ROOT, MOBILE, MANIFEST, verify_runtime, verify_licenses, license_files

CREDENTIAL = re.compile(rb"(?<![\w-])(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})(?![\w-])")
BUILD_ROOT_FILES = {"settings.gradle.kts", "build.gradle.kts", "gradle.properties", "gradlew", "gradlew.bat",
                    "gradle/wrapper/gradle-wrapper.jar", "gradle/wrapper/gradle-wrapper.properties", "app/build.gradle.kts"}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def child_environment(work: Path, java: Path, sdk: Path, node: Path):
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    profile = work / "build-profile"
    local, roaming, temp = profile / "AppData/Local", profile / "AppData/Roaming", work / "temp"
    for folder in (profile, local, roaming, temp): folder.mkdir(parents=True, exist_ok=True)
    windows = Path(env.get("SYSTEMROOT", r"C:\Windows"))
    env.update(USERPROFILE=str(profile), LOCALAPPDATA=str(local), APPDATA=str(roaming), TEMP=str(temp), TMP=str(temp),
               JAVA_HOME=str(java), ANDROID_HOME=str(sdk), ANDROID_SDK_ROOT=str(sdk),
               ANDROID_USER_HOME=str(work / "android-home"), ANDROID_AVD_HOME=str(work / "android-avd"),
               GRADLE_USER_HOME=str(work / "gradle-home"), PYTHONNOUSERSITE="1", PYTHONUTF8="1")
    env["PATH"] = os.pathsep.join([str(java / "bin"), str(node.parent), str(sdk / "platform-tools"), str(windows / "System32"), str(windows)])
    return env


def source_allowed(relative: str) -> bool:
    return (relative in BUILD_ROOT_FILES
            or bool(re.fullmatch(r"app/src/(main|debug)/AndroidManifest\.xml", relative))
            or bool(re.fullmatch(r"app/src/(main|debug)/res/[a-z0-9_-]+/[A-Za-z0-9_.-]+\.xml", relative))
            or bool(re.fullmatch(r"app/src/(main|debug|test|androidTest)/java/com/tingji/app/(?:[A-Za-z0-9_]+/)*[A-Za-z0-9_]+\.kt", relative)))


def scan_input(path: Path, secrets: tuple[bytes, ...]):
    data = path.read_bytes()
    if CREDENTIAL.search(data) or any(value in data for value in secrets):
        raise ValueError("Credential data in reviewed build input; matching values were not printed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--java-home", type=Path, required=True)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--gradle", type=Path, help="Optional installed Gradle 8.11.1 executable")
    parser.add_argument("--node", type=Path, default=Path(shutil.which("node") or "node"))
    parser.add_argument("--work-dir", type=Path, default=ROOT / "work/android-build")
    parser.add_argument("--stage-dir", type=Path, required=True, help="Parent of a fresh source staging folder; use an ASCII path on Windows")
    parser.add_argument("--variant", choices=("debug", "release"), default="debug")
    parser.add_argument("--abis", default="arm64-v8a,x86_64")
    parser.add_argument("--skip-web", action="store_true", help="Use existing reviewed web/dist instead of rebuilding it")
    args = parser.parse_args()
    # Preserve a Windows SUBST drive: resolve() expands it back to a Chinese
    # physical path, defeating the ASCII staging path required by Android tools.
    java, sdk, node = args.java_home.absolute(), args.sdk.absolute(), args.node.absolute()
    java_exe = java / "bin" / ("java.exe" if os.name == "nt" else "java")
    if not java_exe.is_file() or not (sdk / "platforms/android-36/android.jar").is_file():
        raise SystemExit("Provide Java 17 and Android SDK platform 36.")
    if not (sdk / "build-tools/35.0.0").is_dir(): raise SystemExit("Install SDK build-tools 35.0.0.")
    version_env = {key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}}
    version = subprocess.run([str(java_exe), "-version"], env=version_env, capture_output=True, text=True, check=True)
    if not re.search(r'version "17[.\"]', version.stderr + version.stdout): raise SystemExit("This build is pinned to Java 17.")
    if not args.abis or not set(args.abis.split(',')) <= {"arm64-v8a", "x86_64"}: raise SystemExit("Unsupported Android ABI.")
    work, stage_parent = args.work_dir.absolute(), args.stage_dir.absolute()
    if os.name == "nt" and not str(stage_parent).isascii():
        raise SystemExit("Use an ASCII staging path. On Windows, map a dedicated work directory with subst and pass that drive's /stages folder.")
    work.mkdir(parents=True, exist_ok=True)
    stage = stage_parent / ("project-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    stage.mkdir(parents=True, exist_ok=False)
    verify_runtime()
    verify_licenses()
    frontend_licenses = MOBILE / "android/app/src/main/assets/licenses/frontend-components.json"
    if json.loads(frontend_licenses.read_text(encoding="utf-8"))["package_lock_sha256"] != digest(ROOT / "web/package-lock.json"):
        raise SystemExit("Frontend dependencies changed; regenerate and review their public notices before building.")
    env = child_environment(work, java, sdk, node)
    if not args.skip_web:
        if any(path.is_file() for path in (ROOT / "web").glob(".env*")):
            raise SystemExit("Build from a frontend checkout without personal .env files; Vite loads those files automatically.")
        npm_cli = node.parent / "node_modules/npm/bin/npm-cli.js"
        if not npm_cli.is_file(): raise SystemExit("Provide a Node installation including npm.")
        subprocess.run([str(node), str(npm_cli), "run", "build"], cwd=ROOT / "web", env=env, check=True)
    # API values remain in this parent solely for exact-byte auditing. Build
    # hooks and JavaScript compilers receive only the clean child environment.
    secret_values = tuple(value for key, value in os.environ.items() if re.search(r"api.?key|token|secret|password|credential", key, re.I) and len(value) >= 12)
    needles = tuple(value.encode(encoding) for value in secret_values for encoding in ("utf-8", "utf-16le", "utf-16be"))
    receipt = []
    def copy(source: Path, relative: str):
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT.resolve()): raise ValueError("Unapproved linked source input.")
        scan_input(source, needles)
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        receipt.append({"path": relative, "sha256": digest(source)})
    android = MOBILE / "android"
    for source in sorted(android.rglob("*")):
        if source.is_file():
            relative = source.relative_to(android).as_posix()
            if source_allowed(relative): copy(source, relative)
    for row in json.loads(MANIFEST.read_text(encoding="utf-8"))["runtime"]:
        copy(MOBILE / row["path"], row["path"].removeprefix("android/"))
    license_root = android / "app/src/main/assets/licenses"
    for row in license_files():
        source = license_root / row["name"]
        copy(source, source.relative_to(android).as_posix())
    web_files = sorted((ROOT / "web/dist").rglob("*"))
    selected = []
    for source in web_files:
        relative = source.relative_to(ROOT / "web/dist").as_posix()
        if source.is_file() and (relative == "index.html" or re.fullmatch(r"assets/index-[A-Za-z0-9_-]+\.(js|css)", relative)):
            copy(source, "app/src/main/assets/web/" + relative); selected.append(relative)
    if "index.html" not in selected: raise SystemExit("The built mobile frontend is missing.")
    (stage / "local.properties").write_text("sdk.dir=" + str(sdk).replace("\\", "/").replace(":", "\\:") + "\n", encoding="utf-8")
    (stage / "build-inputs.json").write_text(json.dumps({"schema_version": 1, "variant": args.variant, "abis": args.abis.split(','), "files": receipt}, indent=2), encoding="utf-8")
    if args.gradle:
        command = [str(args.gradle.resolve())]
        installed = subprocess.run(command + ["--version"], env=env, check=True, capture_output=True, text=True)
        if not re.search(r"(?m)^Gradle 8\.11\.1\s*$", installed.stdout):
            raise SystemExit("The installed Gradle override must be version 8.11.1.")
    else:
        command = [str(java_exe), "-classpath", str(stage / "gradle/wrapper/gradle-wrapper.jar"), "org.gradle.wrapper.GradleWrapperMain"]
    title = args.variant.capitalize()
    subprocess.run(command + ["--no-daemon", "-Duser.home=" + env["USERPROFILE"], "-PtingjiAbis=" + args.abis, "test" + title + "UnitTest", "assemble" + title], cwd=stage, env=env, check=True)
    apk = stage / f"app/build/outputs/apk/{args.variant}/app-{args.variant}{'-unsigned' if args.variant == 'release' else ''}.apk"
    if not apk.is_file(): raise SystemExit("Gradle did not produce the expected APK.")
    out = work / "dist"; out.mkdir(exist_ok=True)
    output = out / f"Tingji-2.3.0-android.3-{args.variant}{'-unsigned' if args.variant == 'release' else ''}.apk"
    shutil.copyfile(apk, output)
    shutil.copyfile(stage / "build-inputs.json", out / (output.stem + "-build-inputs.json"))
    (out / (output.name + ".sha256")).write_text(digest(output) + "  " + output.name + "\n", encoding="ascii")
    print("Built:", output)
    print("Release APKs must be signed separately, then checked with audit_apk.py --require-release.")


if __name__ == "__main__": main()
