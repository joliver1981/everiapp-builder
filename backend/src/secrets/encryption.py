import base64
import hashlib
import logging
import subprocess
import sys

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

logger = logging.getLogger(__name__)

# Salt for PBKDF2 derivation — changing this invalidates all machine-derived keys.
_DERIVATION_SALT = b"aihub-platform-v1"


def _get_machine_id() -> str | None:
    """Get a stable, unique identifier for this machine.

    Windows:  HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid
    Linux:    /etc/machine-id
    macOS:    IOPlatformSerialNumber via ioreg
    """
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                return value
        elif sys.platform == "darwin":
            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if "IOPlatformSerialNumber" in line:
                    return line.split('"')[-2]
        else:
            for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    with open(path) as f:
                        mid = f.read().strip()
                        if mid:
                            return mid
                except FileNotFoundError:
                    continue
    except Exception as exc:
        logger.warning("Could not read machine ID: %s", exc)
    return None


def _derive_fernet_key(machine_id: str) -> str:
    """Derive a Fernet-compatible key from a machine ID using PBKDF2."""
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        machine_id.encode(),
        _DERIVATION_SALT,
        iterations=100_000,
        dklen=32,
    )
    return base64.urlsafe_b64encode(dk).decode()


class EncryptionService:
    def __init__(self):
        self.key_source: str = "unknown"

        key = settings.master_encryption_key.strip()

        if key:
            self.key_source = "custom"
            logger.info("Encryption: using custom MASTER_ENCRYPTION_KEY")
        else:
            machine_id = _get_machine_id()
            if machine_id:
                key = _derive_fernet_key(machine_id)
                self.key_source = "machine"
                logger.info(
                    "Encryption: using key derived from machine ID (stable across restarts)"
                )
            else:
                key = Fernet.generate_key().decode()
                self.key_source = "random"
                logger.warning(
                    "Encryption: using RANDOM key — secrets will be lost on restart! "
                    "Set MASTER_ENCRYPTION_KEY in .env or ensure machine ID is readable."
                )

        self._fernet = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken:
            raise ValueError(
                "Failed to decrypt: invalid key or corrupted data. "
                "If the encryption key changed, re-enter the secret."
            )


encryption_service = EncryptionService()
