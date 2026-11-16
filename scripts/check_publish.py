"""Audit Git blobs using a strict, content-aware publication allowlist.

Ignored recordings, settings and caches are never read. Findings report file
names and categories, without printing matched credentials or private content.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path, PurePosixPath
import re
import struct
import subprocess
import zlib

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    '.gitignore', '.gitattributes', 'README.md', 'README.en.md', 'app.py',
    'desktop.py', 'requirements_desktop.txt', 'requirements_windows.txt', '启动工具.cmd', '启动工具.vbs',
    '启动旧版界面.cmd', '启动旧版界面.vbs', '准备桌面环境.cmd',
}
WEB_FILES = {
    'web/package.json', 'web/package-lock.json', 'web/index.html',
    'web/vite.config.ts', 'web/tsconfig.json', 'web/components.json',
    'web/COMPONENT-SOURCES.txt', 'web/SHADCN-LICENSE.txt', 'web/dist/index.html',
}
BRAND_FILES = {'assets/tingji.svg', 'assets/tingji.png', 'assets/tingji.ico'}
SCREENSHOT_FILES = {
    'docs/screenshots/transcript.png', 'docs/screenshots/summary.png',
    'docs/screenshots/review.png',
}
BUILD_FILES = {
    'scripts/windows/README.md', 'scripts/windows/build_windows.py',
    'scripts/windows/prepare_vendor_manifest.py', 'scripts/windows/tingji.spec',
    'scripts/windows/tingji.iss',
}
ANDROID_WRAPPER = 'mobile/android/gradle/wrapper/gradle-wrapper.jar'
ANDROID_WRAPPER_BYTES = 43583
ANDROID_WRAPPER_SHA256 = '2db75c40782f5e8ba1fc278a5574bab070adccb2d21ca5a6e5ed840888448046'
ANDROID_LICENSE_FILES = {
    'mobile/android/app/src/main/assets/licenses/' + name for name in {
        '3D-Speaker-LICENSE.txt', 'AndroidX-LICENSE.txt', 'CAMplusplus-model-card.txt',
        'Eigen-3.4.0-MPL2.txt', 'Eigen-3.4.0-README.txt',
        'frontend-components.json', 'frontend-licenses.txt',
        'hclust-cpp-2026-02-25-LICENSE.txt', 'json-3.12.0-LICENSE.txt',
        'kaldi-decoder-0.3.0-LICENSE.txt', 'kaldi-native-fbank-1.22.4-LICENSE.txt',
        'kaldifst-1.8.0-LICENSE.txt', 'KissFFT-BSD-3-Clause.txt', 'KissFFT-COPYING.txt',
        'KissFFT-Unlicense.txt', 'Kotlin-2.1.21-COPYRIGHT.txt', 'Kotlin-2.1.21-LICENSE.txt',
        'license-sources.json', 'LLVM-runtime-LICENSE.txt',
        'onnxruntime-1.28.2-LICENSE.txt', 'onnxruntime-1.28.2-ThirdPartyNotices.txt',
        'OpenFst-1.8.5-LICENSE.txt', 'sherpa-onnx-2.0.1-LICENSE.txt',
        'Silero-VAD-LICENSE.txt', 'simple-sentencepiece-0.7-LICENSE.txt',
        'utf8proc-2.12.0-LICENSE.txt',
    }
}
ANDROID_FILES = {
    'mobile/README.md',
    'mobile/vendor-manifest.json',
    'mobile/scripts/prepare_android.py', 'mobile/scripts/build_android.py', 'mobile/scripts/audit_apk.py',
    'mobile/android/settings.gradle.kts', 'mobile/android/build.gradle.kts',
    'mobile/android/gradle.properties', 'mobile/android/gradlew', 'mobile/android/gradlew.bat',
    'mobile/android/gradle/wrapper/gradle-wrapper.properties', ANDROID_WRAPPER,
    'mobile/android/app/build.gradle.kts',
    'mobile/android/app/src/main/AndroidManifest.xml',
    'mobile/android/app/src/debug/AndroidManifest.xml',
    'mobile/android/app/src/debug/res/xml/debug_network_security.xml',
    'mobile/android/app/src/main/res/xml/file_paths.xml',
    'mobile/android/app/src/main/res/xml/data_extraction_rules.xml',
    'mobile/android/app/src/main/res/values/styles.xml',
    'mobile/android/app/src/main/res/drawable/ic_notification.xml',
    'mobile/android/app/src/main/res/mipmap/ic_launcher.xml',
    'mobile/android/app/src/main/java/com/tingji/app/MainActivity.kt',
    'mobile/android/app/src/main/java/com/tingji/app/MediaRegistry.kt',
    'mobile/android/app/src/main/java/com/tingji/app/SherpaSpeakerAnalyzer.kt',
    'mobile/android/app/src/main/java/com/tingji/app/TingjiApplication.kt',
    'mobile/android/app/src/main/java/com/tingji/app/TingjiTaskService.kt',
    'mobile/android/app/src/main/java/com/tingji/app/engine/AudioTools.kt',
    'mobile/android/app/src/main/java/com/tingji/app/engine/DeepSeekClient.kt',
    'mobile/android/app/src/main/java/com/tingji/app/engine/EngineFiles.kt',
    'mobile/android/app/src/main/java/com/tingji/app/engine/MeetingEngine.kt',
    'mobile/android/app/src/main/java/com/tingji/app/engine/MeetingSummary.kt',
    'mobile/android/app/src/main/java/com/tingji/app/engine/SpeakerAnalyzer.kt',
    'mobile/android/app/src/test/java/com/tingji/app/ByteRangeTest.kt',
    'mobile/android/app/src/test/java/com/tingji/app/SphericalClustererTest.kt',
    'mobile/android/app/src/test/java/com/tingji/app/engine/EnginePureTests.kt',
    'mobile/android/app/src/androidTest/java/com/tingji/app/engine/MeetingEngineInstrumentedTest.kt',
    'mobile/android/app/src/androidTest/java/com/tingji/app/engine/DeepSeekProxyInstrumentedTest.kt',
} | ANDROID_LICENSE_FILES
FORBIDDEN_PARTS = {
    '.runtime', '.venv', 'venv', 'node_modules', '__pycache__', 'cache', 'work',
    '.ai_summary_cache', '转写结果', '人物试听片段', '.gradle', 'build',
}
FORBIDDEN_SUFFIXES = {
    '.wav', '.mp3', '.m4a', '.aac', '.ogg', '.opus', '.flac', '.wma', '.amr',
    '.mp4', '.mkv', '.mov', '.avi', '.webm', '.pem', '.key', '.p12', '.pfx',
    '.zip', '.7z', '.rar', '.db', '.sqlite', '.pt', '.pth', '.onnx', '.safetensors',
    '.pyc', '.log', '.tmp', '.tsbuildinfo', '.aar', '.apk', '.aab', '.jks',
    '.keystore', '.dex', '.so', '.class', '.hprof', '.jar',
}
PATTERNS = {
    'API credential pattern': re.compile(r'(?<![\w-])(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,})(?![\w-])'),
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----'),
    'personal Windows user path': re.compile(r'(?i)[a-z]:[\\/]+Users[\\/]+(?!Public(?:[\\/]|$))[^\\/\s"\'<>]+'),
    'phone number pattern': re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'),
    'credential embedded in URL': re.compile(r'https?://[^\s/<>"\']+:[^\s/@<>"\']+@'),
}


def git(*args, **kwargs):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, stdout=subprocess.PIPE, **kwargs).stdout


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if any(part.lower() in FORBIDDEN_PARTS for part in path.parts):
        return False
    if name == ANDROID_WRAPPER:
        return True  # The audit below requires the exact reviewed upstream bytes.
    if path.suffix.lower() in FORBIDDEN_SUFFIXES or path.name.lower() in {'settings.json', 'local.properties'} or path.name.startswith('.env'):
        return False
    if name in ROOT_FILES or name in WEB_FILES or name in BRAND_FILES or name in SCREENSHOT_FILES or name in BUILD_FILES or name in ANDROID_FILES or name == 'scripts/check_publish.py':
        return True
    if len(path.parts) == 2 and path.parts[0] == 'meeting_tool' and path.suffix == '.py':
        return True
    if len(path.parts) == 2 and path.parts[0] == 'tests' and path.name.startswith('test_') and path.suffix == '.py':
        return True
    if name.startswith('web/src/') and path.suffix in {'.ts', '.tsx', '.css'}:
        return True
    return bool(re.fullmatch(r'web/dist/assets/index-[A-Za-z0-9_-]+\.(?:js|css)', name))


def valid_android_wrapper(content: bytes) -> bool:
    return len(content) == ANDROID_WRAPPER_BYTES and hashlib.sha256(content).hexdigest() == ANDROID_WRAPPER_SHA256


def valid_screenshot(content: bytes) -> bool:
    """Accept only bounded, metadata-free browser PNG captures for reviewed names.

    Pixel contents still require human privacy review before publishing.
    """
    if not content.startswith(b'\x89PNG\r\n\x1a\n'):
        return False
    offset, kinds = 8, []
    while offset < len(content):
        if offset + 12 > len(content):
            return False
        length = struct.unpack('>I', content[offset:offset + 4])[0]
        kind = content[offset + 4:offset + 8]
        end = offset + 8 + length
        if end + 4 > len(content) or kind not in {b'IHDR', b'IDAT', b'IEND'}:
            return False
        payload = content[offset + 8:end]
        expected = struct.unpack('>I', content[end:end + 4])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != expected:
            return False
        if kind == b'IHDR':
            if kinds or length != 13:
                return False
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
            if not (960 <= width <= 2560 and 600 <= height <= 1600 and depth == 8
                    and color in {2, 6} and compression == filtering == interlace == 0):
                return False
        elif kind == b'IEND':
            if length != 0 or end + 4 != len(content):
                return False
        kinds.append(kind)
        offset = end + 4
    return (len(kinds) >= 3 and kinds[0] == b'IHDR' and kinds[-1] == b'IEND'
            and all(kind == b'IDAT' for kind in kinds[1:-1]))


def entries(ref: str | None):
    rows = git('ls-tree', '-r', '-z', '--full-tree', ref) if ref else git('ls-files', '--stage', '-z')
    result = []
    for row in rows.split(b'\0'):
        if not row:
            continue
        meta, name = row.split(b'\t', 1)
        mode, middle, last = meta.decode('ascii').split()
        if ref:
            sha = last
        else:
            sha = middle
            if last != '0':
                raise ValueError('Unmerged index entries cannot be published.')
        result.append((name.decode('utf-8'), mode, sha))
    return result


def audit(ref: str | None) -> int:
    files = entries(ref)
    if not files:
        print('No reviewed files in the Git index/commit; upload blocked.')
        return 1
    raw = git('cat-file', '--batch', input=('\n'.join(row[2] for row in files) + '\n').encode('ascii'))
    offset, total_bytes, findings = 0, 0, []
    for name, mode, _sha in files:
        end = raw.index(b'\n', offset)
        header = raw[offset:end].split()
        if len(header) != 3 or header[1] != b'blob':
            raise ValueError('A staged object is not a regular Git blob.')
        size = int(header[2])
        content = raw[end + 1:end + 1 + size]
        offset = end + 1 + size + 1
        total_bytes += size
        if mode not in {'100644', '100755'} or not allowed(name):
            findings.append((name, 'file is outside the reviewed source allowlist'))
            continue
        if size > 3 * 1024 * 1024:
            findings.append((name, 'unexpectedly large file'))
            continue
        if name == ANDROID_WRAPPER:
            if not valid_android_wrapper(content):
                findings.append((name, 'Gradle wrapper does not match the pinned upstream bytes'))
            continue
        if name in SCREENSHOT_FILES:
            if not valid_screenshot(content):
                findings.append((name, 'screenshot format, dimensions or metadata not approved'))
            continue
        if name in {'assets/tingji.png', 'assets/tingji.ico'}:
            expected = b'\x89PNG\r\n\x1a\n' if name.endswith('.png') else b'\x00\x00\x01\x00'
            if not content.startswith(expected):
                findings.append((name, 'unexpected brand image format'))
            continue
        try:
            text = content.decode('utf-16') if content.startswith((b'\xff\xfe', b'\xfe\xff')) else content.decode('utf-8-sig')
        except UnicodeError:
            findings.append((name, 'unexpected binary or encoding'))
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append((name, label))
    if findings:
        for name, label in findings:
            print(f'BLOCKED: {name}: {label}')
        print('Upload blocked. No matching secret or private content was printed.')
        return 1
    print(f'PASS: {len(files)} reviewed files, {total_bytes} bytes; no forbidden paths, media, or credential patterns.')
    return 0


def main():
    parser = argparse.ArgumentParser(description='Check Git content before sharing this project.')
    parser.add_argument('--ref', help='Audit a commit/ref instead of the current Git index.')
    args = parser.parse_args()
    try:
        return audit(args.ref)
    except (ValueError, OSError, subprocess.CalledProcessError):
        print('Could not complete the Git privacy check; upload blocked.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
