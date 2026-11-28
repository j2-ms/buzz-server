"""Bounded advisory observations shared by the worker and the operator CLI."""
import re
import threading
import time


WORKER = ("local_setup", "profile_unconfirmed", "local_runtime", "ready", "failed", "exited", "stopped")
RELAY = ("admitted", "refused", "connected", "disconnected", "connection_failed")
HARNESS = ("failed", "authorization_failed")
STALE_AFTER = 900
TRACE = re.compile(rb"^\d{4}-\d\d-\d\dT[0-9:.]+Z\s+(?:INFO|WARN|ERROR)\s+([a-z_:]+): (.*)\n$")
ANSI = re.compile(rb"\x1b\[[0-9;]*m")


class Observations:
    def __init__(self, notify):
        self.notify = notify
        self.lock = threading.Lock()
        self.values = [None, None, None]

    def update(self, index, outcome, *, ready=False):
        with self.lock:
            value = (outcome, int(time.time()))
            if self.values[index] != value or ready:
                self.values[index] = value
                fields = [f"{v[0]}:{v[1]}" if v else "-" for v in self.values]
                self.notify("observations-v1|" + "|".join(fields), ready=ready)

    def watch(self, stream):
        # Tracing is advisory and version-sensitive. Keep only fixed outcomes
        # from the pinned adapter. Never save, forward or print raw lines.
        discarding = False
        with stream:
            while line := stream.readline(4097):
                complete = line.endswith(b"\n")
                if not discarding and complete and len(line) <= 4096:
                    self.trace(line)
                discarding = not complete

    def trace(self, line):
        record = TRACE.fullmatch(ANSI.sub(b"", line))
        if record is None:
            return
        target, message = record.groups()
        if target == b"buzz_acp" and message.startswith(b"connected to relay at "):
            self.update(1, "connected")
        elif target == b"buzz_acp::relay":
            if message.startswith(b"relay connection lost "):
                self.update(1, "disconnected")
            elif message.startswith((b"initial relay connect ", b"relay reconnect failed:")):
                self.update(1, "connection_failed")
            elif message.startswith(b"resubscribing to ") and message.endswith(b" channel(s) after reconnect"):
                self.update(1, "connected")
        elif target == b"buzz_acp" and message.startswith(b"dead-lettering batch immediately ") and b"non-retryable auth error" in message:
            self.update(2, "authorization_failed")
        elif ((target == b"pool::prompt" and message.startswith(b"session_prompt error: "))
              or (target == b"buzz_acp" and message.startswith(b"agent failed to spawn: "))):
            self.update(2, "failed")


def observed(outcome=None, when=None, *, current=False):
    return {"outcome": outcome or "unknown", "observed_at": when,
            "stale": when is not None and (not current or time.time() - when > STALE_AFTER)}


def parse(raw, *, current):
    fields = raw.split(b"|") if len(raw) <= 256 else []
    if len(fields) != 4 or fields[0] != b"buzz-server:observations-v1":
        fields = [None] * 4
    result = {}
    for key, field, allowed in zip(("worker", "relay", "harness"), fields[1:], (WORKER, RELAY, HARNESS), strict=True):
        match = re.fullmatch(rb"([a-z_]+):([1-9][0-9]{0,9})", field or b"")
        if match is None or match[1].decode() not in allowed or int(match[2]) > time.time() + 1:
            result[key] = observed()
        else:
            result[key] = observed(match[1].decode(), int(match[2]), current=current)
    return result
