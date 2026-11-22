from ipaddress import ip_address
import json
from pathlib import Path
import re
from typing import NoReturn
from urllib.parse import urlsplit

from .errors import Error


def invalid() -> NoReturn:
    raise Error("invalid_config", "Invalid configuration. Use the documented v1 schema, absolute file references and credential-free URLs; credentials belong in Goose private state.")


def text(value, maxlen=4096, empty=False):
    return isinstance(value, str) and (empty or bool(value)) and len(value) <= maxlen and not any(ord(c) < 32 and c not in "\n\t" for c in value)


def absolute(value):
    return text(value, 4096) and Path(value).is_absolute() and "\n" not in value


def url(value, schemes):
    if not text(value, 2048):
        return False
    try:
        u = urlsplit(value)
        return u.scheme in schemes and bool(u.hostname) and not u.username and not u.password and not u.query and not u.fragment and u.port != 0
    except ValueError:
        return False


def validate_relay_transport(value):
    relay = urlsplit(value)
    if any(c.isspace() for c in value) or "\\" in value or relay.username is not None or relay.password is not None:
        invalid()
    if value.startswith("wss://"):
        return
    host = relay.hostname
    try:
        if value.startswith("ws://") and host and "%" not in host and ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise Error("invalid_config", "Use wss:// for the relay. ws:// is allowed only for a literal loopback IP address in local tests.")


def validate(c):
    if not isinstance(c, dict) or set(c) != {"relay_url", "owner", "profile", "prompt_file", "provider", "harness"}:
        invalid()
    if not url(c["relay_url"], ("ws", "wss")) or not isinstance(c["owner"], str) or not re.fullmatch("[0-9a-f]{64}", c["owner"]):
        invalid()
    p = c["profile"]
    if not isinstance(p, dict) or set(p) != {"name", "about", "avatar"}:
        invalid()
    if not text(p["name"], 100) or not text(p["about"], 4096, True) or not (p["avatar"] == "" or url(p["avatar"], ("https",))):
        invalid()
    h = c["harness"]
    if not isinstance(h, dict) or set(h) != {"kind", "command"} or h["kind"] != "goose" or not absolute(h["command"]):
        invalid()
    if c["provider"] != {"kind": "goose-managed"} or not absolute(c["prompt_file"]):
        invalid()
    return c


def validate_stored(c):
    """Recognize pre-release definitions only so they can be taken out of service."""
    p = c.get("provider") if isinstance(c, dict) else None
    if not isinstance(p, dict) or p.get("kind") not in ("goose-openai", "openai", "anthropic"):
        return validate(c)
    replacement = {**c, "provider": {"kind": "goose-managed"}}
    fields = {"kind", "model", "env_file"}
    if p["kind"] == "goose-openai":
        fields.add("command")
        if "harness" in c:
            invalid()
        replacement["harness"] = {"kind": "goose", "command": p.get("command")}
    if (set(p) != fields or not absolute(p["env_file"]) or not isinstance(p["model"], str) or
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", p["model"])):
        invalid()
    validate(replacement)
    return c


def current(c):
    return c["provider"] == {"kind": "goose-managed"}


def require_current(c):
    if not current(c):
        raise Error("obsolete_config", "This pre-release agent cannot run. Stop it, replace its definition with configure --config using the current schema, then run inference configure.")


def load(path):
    # Configuration contains references, never inline credentials.
    from .storage import read_private
    try:
        raw = read_private(path)
    except Error:
        raise Error("invalid_config", "Cannot read --config. Use a regular file owned by the service user with mode 0600, and check the supplied path.") from None
    try:
        config = validate(json.loads(raw))
    except (ValueError, UnicodeError):
        invalid()
    validate_relay_transport(config["relay_url"])
    return config
