"""Android source publishing boundaries; no user data or device access."""
from pathlib import Path
from contextlib import redirect_stdout
import importlib.util
import hashlib
import io
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('android_publication', Path(__file__).resolve().parents[1] / 'scripts/check_publish.py')
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


class AndroidPublicationTests(unittest.TestCase):
    def test_only_reviewed_android_source_is_allowed(self):
        for name in publication.ANDROID_FILES:
            self.assertTrue(publication.allowed(name), name)
        for name in ('mobile/android/app/src/main/java/com/tingji/app/PrivateNotes.kt',
                     'mobile/android/app/src/main/AndroidManifest-copy.xml', 'mobile/notes.md',
                     'mobile/scripts/private_export.py', 'mobile/vendor-manifest-private.json',
                     'mobile/android/app/src/main/assets/licenses/private-notes.txt'):
            self.assertFalse(publication.allowed(name), name)

    def test_private_build_inputs_and_outputs_are_rejected(self):
        for name in ('mobile/android/local.properties', 'mobile/android/.gradle/state.bin',
                     'mobile/android/app/build/outputs/apk/release/app.apk', 'mobile/android/app/libs/sherpa.aar',
                     'mobile/android/app/src/main/assets/models/speaker.onnx', 'mobile/release.aab',
                     'mobile/signing-release.jks', 'mobile/upload.keystore', 'mobile/.env',
                     'mobile/android/settings.json', 'mobile/转写结果/AI总结.md',
                     'mobile/android/gradle/wrapper/other.jar', 'work/android/result.json'):
            self.assertFalse(publication.allowed(name), name)

    def test_wrapper_is_the_only_hash_checked_binary_exception(self):
        self.assertTrue(publication.allowed(publication.ANDROID_WRAPPER))
        self.assertFalse(publication.valid_android_wrapper(b'PK fake wrapper'))
        fixture = b'public upstream wrapper fixture'
        with patch.object(publication, 'ANDROID_WRAPPER_BYTES', len(fixture)), \
                patch.object(publication, 'ANDROID_WRAPPER_SHA256', hashlib.sha256(fixture).hexdigest()):
            self.assertTrue(publication.valid_android_wrapper(fixture))
            self.assertFalse(publication.valid_android_wrapper(fixture + b'changed'))

    def test_blob_audit_checks_wrapper_bytes_without_text_decoding(self):
        fixture = b'PK\x03\x04\xff\xfe\x00upstream-wrapper-fixture'
        blob = lambda value: b'f' * 40 + b' blob ' + str(len(value)).encode() + b'\n' + value + b'\n'
        with patch.object(publication, 'entries', return_value=[(publication.ANDROID_WRAPPER, '100644', 'f' * 40)]), \
                patch.object(publication, 'ANDROID_WRAPPER_BYTES', len(fixture)), \
                patch.object(publication, 'ANDROID_WRAPPER_SHA256', hashlib.sha256(fixture).hexdigest()), \
                redirect_stdout(io.StringIO()):
            with patch.object(publication, 'git', return_value=blob(fixture)):
                self.assertEqual(publication.audit(None), 0)
            with patch.object(publication, 'git', return_value=blob(fixture[:-1] + b'!')):
                self.assertEqual(publication.audit(None), 1)


if __name__ == '__main__':
    unittest.main()
