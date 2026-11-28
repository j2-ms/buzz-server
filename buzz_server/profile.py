"""Publish self-signed agent metadata through the Buzz relay HTTP bridge."""
from base64 import b64encode
from contextlib import closing
from hashlib import sha256
from http.client import HTTPConnection, HTTPSConnection
import json
from pathlib import Path
from secrets import token_hex
import ssl
import time
from urllib.parse import quote, urlsplit

from .config import validate_relay_transport
from .identity import schnorr_sign


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def signed_event(key, pubkey, kind, content, tags, created_at):
    digest = sha256(json_bytes([0, pubkey, created_at, kind, tags, content])).digest()
    return {"id": digest.hex(), "pubkey": pubkey, "created_at": created_at, "kind": kind,
            "tags": tags, "content": content, "sig": schnorr_sign(key, digest).hex()}


def relay_tls_context():
    # Use OpenSSL's system roots without consulting SSL_CERT_* or SSLKEYLOGFILE.
    roots = ssl.get_default_verify_paths()
    cafile = roots.openssl_cafile if Path(roots.openssl_cafile).is_file() else None
    capath = roots.openssl_capath if Path(roots.openssl_capath).is_dir() else None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=cafile, capath=capath)
    return context


def post(relay, endpoint, value, key, pubkey):
    secure = relay.scheme == "wss"
    scheme = "https" if secure else "http"
    default_port = 443 if secure else 80
    # An explicit port prevents http.client from splitting a bare IPv6 host.
    port = relay.port if relay.port is not None else default_port
    host = relay.hostname.encode("idna").decode("ascii")
    authority = f"[{host}]" if ":" in host else host
    if port != default_port:
        authority += f":{port}"
    path = quote(relay.path.rstrip("/") + endpoint, safe="/%:@!$&'()*+,;=")
    body = json_bytes(value)
    tags = [["u", f"{scheme}://{authority}{path}"], ["method", "POST"],
            ["nonce", token_hex(16)], ["payload", sha256(body).hexdigest()]]
    proof = signed_event(key, pubkey, 27235, "", tags, int(time.time()))
    headers = {"Authorization": "Nostr " + b64encode(json_bytes(proof)).decode("ascii"),
               "Content-Type": "application/json"}
    connection = (HTTPSConnection(host, port, timeout=10, context=relay_tls_context())
                  if secure else HTTPConnection(host, port, timeout=10))
    # Direct requests never follow redirects or consult proxy settings.
    with closing(connection):
        connection.request("POST", path, body, headers)
        with connection.getresponse() as response:
            if not 200 <= response.status < 300:
                raise ValueError("Profile request failed")
            limit = 128 * 1024
            payload = response.read(limit + 1)
    if len(payload) > limit:
        raise ValueError("Profile response exceeds limit")
    return json.loads(payload)


def publish_profile(relay_url, profile, key, pubkey):
    validate_relay_transport(relay_url)
    relay = urlsplit(relay_url)
    events = post(relay, "/query", [{"kinds": [0], "authors": [pubkey], "limit": 1}], key, pubkey)
    if not isinstance(events, list) or len(events) > 1:
        raise ValueError("Invalid profile query response")
    content = {"display_name": profile["name"], "about": profile["about"],
               "picture": profile["avatar"], "is_agent": True}
    created_at = int(time.time())
    if events:
        current = events[0]
        if (not isinstance(current, dict) or current.get("kind") != 0 or current.get("pubkey") != pubkey
                or type(current.get("created_at")) is not int or current["created_at"] > created_at):
            raise ValueError("Invalid current profile")
        try:
            current_content = json.loads(current["content"])
        except (KeyError, TypeError, ValueError):
            current_content = None
        # Preserve the optional field the previous Buzz CLI profile writer kept.
        if isinstance(current_content, dict) and isinstance(current_content.get("nip05"), str):
            content["nip05"] = current_content["nip05"]
        if current["created_at"] == created_at:
            # Kind 0 is replaceable. An edit must outrank the previous event.
            time.sleep(max(0, created_at + 1 - time.time()))
            created_at = int(time.time())
            if created_at <= current["created_at"]:
                raise ValueError("Profile clock did not advance")
    event = signed_event(key, pubkey, 0, json_bytes(content).decode("utf-8"), [], created_at)
    result = post(relay, "/events", event, key, pubkey)
    if isinstance(result, dict) and "event_id" in result and result["event_id"] != event["id"]:
        raise ValueError("Profile response refers to a different event")
    return result
