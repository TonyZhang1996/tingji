"""Current-Windows-user encrypted storage for one DeepSeek API key.

Only DPAPI ciphertext is persisted. This module has no network behavior and does
not offer a browser-facing method that returns a credential.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import threading

from .runtime_paths import source_root

MAX_KEY_LENGTH = 512
_MAX_CIPHERTEXT = 64 * 1024
_PAYLOAD_MAGIC = b"Tingji/DeepSeek/API-key/v1\0"
_ENTROPY = b"Tingji.current-user.deepseek-key.v1"


class CredentialStoreError(ValueError):
    """A controlled, user-actionable error; never includes a credential."""


def validate_api_key(value: object) -> str:
    if not isinstance(value, str):
        raise CredentialStoreError("请填写有效的 DeepSeek API Key。")
    key = value.strip()
    if not key or len(key) > MAX_KEY_LENGTH or any(not 33 <= ord(char) <= 126 for char in key):
        raise CredentialStoreError("请填写有效的 DeepSeek API Key，勿包含换行或空格。")
    return key


def default_credential_path(app_dir: Path) -> Path:
    """Real app data uses LocalAppData; temporary/injected apps remain isolated."""
    app_dir = Path(app_dir).resolve()
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    user_root = (base / "Tingji").resolve()
    if app_dir in (source_root().resolve(), user_root):
        return user_root / "credentials" / "deepseek-api-key.dpapi"
    return app_dir / "credentials" / "deepseek-api-key.dpapi"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _WindowsDPAPI:
    supported = os.name == "nt" and hasattr(ctypes, "WinDLL")

    def _transform(self, value: bytes, *, decrypt: bool) -> bytes:
        if not self.supported:
            raise CredentialStoreError("此系统暂不支持加密保存 API Key，请在本次操作中填写 Key。")
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        protect = crypt32.CryptProtectData
        protect.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_wchar_p, ctypes.POINTER(_DataBlob),
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        protect.restype = wintypes.BOOL
        unprotect = crypt32.CryptUnprotectData
        unprotect.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.POINTER(_DataBlob),
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_DataBlob)]
        unprotect.restype = wintypes.BOOL
        input_buffer = ctypes.create_string_buffer(value, len(value))
        entropy_buffer = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
        incoming = _DataBlob(len(value), ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        entropy = _DataBlob(len(_ENTROPY), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        outgoing = _DataBlob()
        try:
            # 0x1 = CRYPTPROTECT_UI_FORBIDDEN. Intentionally do NOT use
            # CRYPTPROTECT_LOCAL_MACHINE: protection is bound to this user.
            if decrypt:
                ok = unprotect(ctypes.byref(incoming), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(outgoing))
            else:
                ok = protect(ctypes.byref(incoming), "Tingji DeepSeek API Key", ctypes.byref(entropy), None, None, 0x1, ctypes.byref(outgoing))
            if not ok:
                raise CredentialStoreError("已保存的 API Key 无法由当前 Windows 用户解密，请在设置中重新保存。" if decrypt
                                           else "Windows 无法加密保存 API Key，请确认当前用户配置可用后重试。")
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            ctypes.memset(ctypes.addressof(input_buffer), 0, len(value))
            if outgoing.pbData:
                if decrypt:
                    ctypes.memset(outgoing.pbData, 0, outgoing.cbData)
                kernel32.LocalFree(ctypes.cast(outgoing.pbData, ctypes.c_void_p))

    def encrypt(self, value: bytes) -> bytes:
        return self._transform(value, decrypt=False)

    def decrypt(self, value: bytes) -> bytes:
        return self._transform(value, decrypt=True)


class CredentialStore:
    def __init__(self, path: Path, *, protector=None):
        self.path = Path(path).resolve()
        self._protector = protector if protector is not None else _WindowsDPAPI()
        self._lock = threading.RLock()
        self._status_signature = None
        self._status_valid = False

    @property
    def supported(self) -> bool:
        return bool(self._protector.supported)

    def _signature(self):
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def is_saved(self) -> bool:
        """Return only usable-storage status, never the key or a masked key."""
        if not self.supported:
            return False
        with self._lock:
            signature = self._signature()
            if signature is None:
                return False
            if signature != self._status_signature:
                try:
                    self._status_valid = bool(self._load())
                except CredentialStoreError:
                    self._status_valid = False
                self._status_signature = signature
            return self._status_valid

    def save(self, api_key: str) -> None:
        key = validate_api_key(api_key)
        if not self.supported:
            raise CredentialStoreError("此系统暂不支持加密保存 API Key，请在本次操作中填写 Key。")
        encoded = key.encode("utf-8")
        payload = _PAYLOAD_MAGIC + len(encoded).to_bytes(4, "big") + encoded + hashlib.sha256(encoded).digest()
        try:
            encrypted = self._protector.encrypt(payload)
            if not isinstance(encrypted, bytes) or not 16 <= len(encrypted) <= _MAX_CIPHERTEXT:
                raise CredentialStoreError("Windows 返回了无效的加密结果，API Key 未保存，请重试。")
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("Windows 无法加密保存 API Key，请重试。") from None
        with self._lock:
            temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with temporary.open("xb") as stream:
                    stream.write(encrypted)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            except OSError:
                raise CredentialStoreError("无法保存 API Key，请检查当前用户数据目录的写入权限后重试。") from None
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # Any leftover temporary file contains ciphertext only.
            self._status_signature = self._signature()
            self._status_valid = True

    def _load(self) -> str:
        if not self.supported:
            raise CredentialStoreError("此系统暂不支持读取加密的 API Key，请在本次操作中填写 Key。")
        try:
            with self.path.open("rb") as stream:
                encrypted = stream.read(_MAX_CIPHERTEXT + 1)
        except FileNotFoundError:
            return ""
        except OSError:
            raise CredentialStoreError("无法读取已保存的 API Key，请检查文件权限或在设置中重新保存。") from None
        try:
            if not 16 <= len(encrypted) <= _MAX_CIPHERTEXT:
                raise ValueError("invalid encrypted file length")
            payload = self._protector.decrypt(encrypted)
            prefix_size = len(_PAYLOAD_MAGIC)
            if not payload.startswith(_PAYLOAD_MAGIC) or len(payload) < prefix_size + 4 + 32:
                raise ValueError("invalid payload")
            length = int.from_bytes(payload[prefix_size:prefix_size + 4], "big")
            encoded = payload[prefix_size + 4:-32]
            if length != len(encoded) or not hmac.compare_digest(hashlib.sha256(encoded).digest(), payload[-32:]):
                raise ValueError("invalid payload integrity")
            return validate_api_key(encoded.decode("utf-8"))
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("已保存的 API Key 已损坏或不可解密，请在设置中重新保存。") from None

    def load(self) -> str:
        with self._lock:
            return self._load()

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                raise CredentialStoreError("无法清除已保存的 API Key，请检查当前用户数据目录的权限后重试。") from None
            self._status_signature = None
            self._status_valid = False
