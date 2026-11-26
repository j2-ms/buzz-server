import re
import secrets
import subprocess

from .errors import Error

# SEC1 private key, named secp256k1 parameters. OpenSSL performs public-key derivation.
PREFIX = bytes.fromhex("302e0201010420")
SUFFIX = bytes.fromhex("a00706052b8104000a")
ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def operator_public_key(value):
    """Normalize the two public-key formats accepted by guided creation."""
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    alphabet = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
    if (len(value) != 63 or not value.isascii() or value != value.lower() and value != value.upper()
            or not value.lower().startswith("npub1")):
        return None
    try:
        words = [alphabet.index(char) for char in value.lower()[5:]]
    except ValueError:
        return None
    # Bech32 checksum over the expanded HRP and all data/check symbols.
    remainder = 1
    hrp = [ord(char) for char in "npub"]
    for word in [char >> 5 for char in hrp] + [0] + [char & 31 for char in hrp] + words:
        top = remainder >> 25
        remainder = ((remainder & 0x1FFFFFF) << 5) ^ word
        for bit, generator in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            if top & (1 << bit):
                remainder ^= generator
    if remainder != 1 or words[-7] & 0x0F:
        return None
    payload = 0
    for word in words[:-6]:
        payload = (payload << 5) | word
    return (payload >> 4).to_bytes(32, "big").hex()


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
