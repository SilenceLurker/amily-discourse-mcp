"""Windows DPAPI, current-user scope only. No plaintext fallback."""

import ctypes
from ctypes import wintypes
import sys

from .errors import AppError


class Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class DPAPIVault:
    def __init__(self):
        if sys.platform != "win32":
            raise AppError("Credential storage requires Windows DPAPI; no plaintext fallback exists.")
        self.crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.crypt.CryptProtectData.argtypes = [
            ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
        ]
        self.crypt.CryptUnprotectData.argtypes = [
            ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob),
        ]
        self.crypt.CryptProtectData.restype = wintypes.BOOL
        self.crypt.CryptUnprotectData.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p

    def _convert(self, data: bytes, alias: str, *, decrypt: bool) -> bytes:
        entropy = b"amily-discourse-mcp/v1/" + alias.encode("utf-8")
        data_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        entropy_buffer = (ctypes.c_ubyte * len(entropy)).from_buffer_copy(entropy)
        source = Blob(len(data), data_buffer)
        extra = Blob(len(entropy), entropy_buffer)
        output = Blob()
        function = self.crypt.CryptUnprotectData if decrypt else self.crypt.CryptProtectData
        # CRYPTPROTECT_UI_FORBIDDEN, deliberately NOT CRYPTPROTECT_LOCAL_MACHINE.
        ok = function(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(output))
        try:
            if not ok:
                raise AppError("Windows credential protection failed; use the original Windows user and machine.")
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            ctypes.memset(data_buffer, 0, len(data))
            if output.pbData:
                ctypes.memset(output.pbData, 0, output.cbData)
                self.kernel.LocalFree(output.pbData)

    def encrypt(self, alias: str, secret: str) -> bytes:
        return self._convert(secret.encode("ascii"), alias, decrypt=False)

    def decrypt(self, alias: str, encrypted: bytes) -> str:
        return self._convert(encrypted, alias, decrypt=True).decode("ascii")
