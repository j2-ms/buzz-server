import secrets
import subprocess

from .errors import Error

# SEC1 private key, named secp256k1 parameters. OpenSSL performs public-key derivation.
PREFIX = bytes.fromhex("302e0201010420")
SUFFIX = bytes.fromhex("a00706052b8104000a")
ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def public(key):
    try:
        n = int(key, 16)
        if len(key) != 64 or not 0 < n < ORDER:
            raise ValueError()
        p = subprocess.run(["/usr/bin/openssl", "ec", "-inform", "DER", "-pubout", "-conv_form", "compressed", "-outform", "DER"],
                           input=PREFIX + bytes.fromhex(key) + SUFFIX, capture_output=True, timeout=5)
        if p.returncode or len(p.stdout) != 56 or p.stdout[-33] not in (2, 3):
            raise ValueError()
        return p.stdout[-32:].hex()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise Error("identity_unavailable", "Cannot verify the agent identity. Install OpenSSL with secp256k1 support or restore a valid identity file.")


def generate():
    key = f"{secrets.randbelow(ORDER - 1) + 1:064x}"
    return key, public(key)
