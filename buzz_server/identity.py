from hashlib import sha256
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


def compressed_public(scalar):
    try:
        if not 0 < scalar < ORDER:
            raise ValueError()
        p = subprocess.run(["/usr/bin/openssl", "ec", "-inform", "DER", "-pubout", "-conv_form", "compressed", "-outform", "DER"],
                           input=PREFIX + scalar.to_bytes(32, "big") + SUFFIX, capture_output=True,
                           timeout=5, env={"PATH": "/usr/bin:/bin"})
        if p.returncode or len(p.stdout) != 56 or p.stdout[-33] not in (2, 3):
            raise ValueError()
        return p.stdout[-33:]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise Error("identity_unavailable", "Cannot verify the agent identity. Install OpenSSL with secp256k1 support or restore a valid identity file.")


def public(key):
    try:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ValueError()
        return compressed_public(int(key, 16))[1:].hex()
    except ValueError:
        raise Error("identity_unavailable", "Cannot verify the agent identity. Install OpenSSL with secp256k1 support or restore a valid identity file.") from None


def tagged_hash(tag, data):
    prefix = sha256(tag).digest()
    return sha256(prefix + prefix + data).digest()


def schnorr_sign(key, message):
    """BIP-340 signing. OpenSSL supplies the secp256k1 point multiplication."""
    secret = int(key, 16)
    point = compressed_public(secret)
    public_x = point[1:]
    if point[0] == 3:
        secret = ORDER - secret
    mask = tagged_hash(b"BIP0340/aux", secrets.token_bytes(32))
    masked_secret = bytes(a ^ b for a, b in zip(secret.to_bytes(32, "big"), mask, strict=True))
    nonce = int.from_bytes(tagged_hash(b"BIP0340/nonce", masked_secret + public_x + message), "big") % ORDER
    nonce_point = compressed_public(nonce)
    nonce_x = nonce_point[1:]
    if nonce_point[0] == 3:
        nonce = ORDER - nonce
    challenge = int.from_bytes(tagged_hash(b"BIP0340/challenge", nonce_x + public_x + message), "big") % ORDER
    return nonce_x + ((nonce + challenge * secret) % ORDER).to_bytes(32, "big")


def generate():
    key = f"{secrets.randbelow(ORDER - 1) + 1:064x}"
    return key, public(key)
