import ctypes
import sys
from ctypes import wintypes


class CredentialError(RuntimeError):
    pass


def credential_target(owner_username: str, model_config_id: str) -> str:
    return f"FuturesRealtimeAI:{owner_username}:{model_config_id}"


class WindowsCredentialStore:
    """Windows Credential Manager adapter. It never falls back to files."""

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    def __init__(self):
        if sys.platform != "win32":
            raise CredentialError("Windows 凭据管理器仅在 Windows 可用")
        self.api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self.api.CredWriteW.argtypes = [ctypes.POINTER(self.CREDENTIALW), wintypes.DWORD]
        self.api.CredWriteW.restype = wintypes.BOOL
        self.api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(ctypes.POINTER(self.CREDENTIALW))]
        self.api.CredReadW.restype = wintypes.BOOL
        self.api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.api.CredDeleteW.restype = wintypes.BOOL
        self.api.CredFree.argtypes = [ctypes.c_void_p]

    def set(self, target: str, secret: str) -> None:
        if not secret:
            raise CredentialError("API Key 不能为空")
        blob = secret.encode("utf-16-le")
        buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        credential = self.CREDENTIALW()
        credential.Type = self.CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.CredentialBlobSize = len(blob)
        credential.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "FuturesRealtimeAI"
        if not self.api.CredWriteW(ctypes.byref(credential), 0):
            raise CredentialError(f"写入 Windows 凭据失败，错误码 {ctypes.get_last_error()}")

    def get(self, target: str) -> str | None:
        pointer = ctypes.POINTER(self.CREDENTIALW)()
        if not self.api.CredReadW(target, self.CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise CredentialError(f"读取 Windows 凭据失败，错误码 {error}")
        try:
            credential = pointer.contents
            raw = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
            return raw.decode("utf-16-le")
        finally:
            self.api.CredFree(pointer)

    def delete(self, target: str) -> None:
        if self.api.CredDeleteW(target, self.CRED_TYPE_GENERIC, 0):
            return
        error = ctypes.get_last_error()
        if error != self.ERROR_NOT_FOUND:
            raise CredentialError(f"删除 Windows 凭据失败，错误码 {error}")


class UnavailableCredentialStore:
    def __init__(self, reason: str):
        self.reason = reason

    def set(self, target: str, secret: str) -> None:
        raise CredentialError(self.reason)

    def get(self, target: str) -> str | None:
        raise CredentialError(self.reason)

    def delete(self, target: str) -> None:
        raise CredentialError(self.reason)


try:
    credential_store = WindowsCredentialStore()
except CredentialError as exc:
    credential_store = UnavailableCredentialStore(str(exc))
