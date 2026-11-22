#!/usr/bin/python3
"""Disposable loopback-only HTTP/Nostr transport. Not a real Buzz relay.

Accepts profile publication; exercises AUTH and acknowledges EVENT and REQ.
No inference, message routing, auth or signature validation is claimed.
"""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import struct
import sys
import threading
from typing import cast


def frame(value, opcode=1):
    payload = json.dumps(value).encode() if opcode == 1 else value
    n = len(payload)
    header = bytes([0x80 | opcode, n]) if n < 126 else bytes([0x80 | opcode, 126]) + struct.pack("!H", n)
    return header + payload


def receive(stream):
    header = stream.read(2)
    if len(header) != 2:
        return None
    opcode, length = header[0] & 15, header[1] & 127
    if length == 126:
        length = struct.unpack("!H", stream.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", stream.read(8))[0]
    if length > 1048576:
        raise ValueError("fixture frame limit")
    masked = bool(header[1] & 128)
    mask = stream.read(4) if masked else b""
    payload = stream.read(length)
    if masked:
        payload = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
    return opcode, payload


class Relay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root):
        super().__init__(("127.0.0.1", 0), Handler)
        self.root = root
        self.guard = threading.Lock()
        self.counts = {"profile_posts": 0, "websockets": 0, "subscriptions": 0}

    def count(self, key):
        with self.guard:
            self.counts[key] += 1
            temp = self.root / "counts.tmp"
            temp.write_text(json.dumps(self.counts))
            temp.replace(self.root / "counts.json")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def unavailable(self):
        return (cast(Relay, self.server).root / "unavailable").exists()

    def do_POST(self):
        if self.unavailable():
            self.send_error(503)
            return
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 1048576:
            self.send_error(400)
            return
        body = json.loads(self.rfile.read(length))
        if self.path == "/query" and isinstance(body, list):
            self.reply([])
        elif self.path == "/events" and isinstance(body, dict) and body.get("kind") == 0 and isinstance(body.get("id"), str):
            self.server.count("profile_posts")
            self.reply({"accepted": True, "message": "", "id": body["id"]})
        else:
            self.send_error(400)

    def do_GET(self):
        if self.unavailable():
            self.send_error(503)
            return
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self.reply([])
            return
        key = self.headers["Sec-WebSocket-Key"]
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.server.count("websockets")
        self.wfile.write(frame(["AUTH", "synthetic-lifecycle-challenge"]))
        self.wfile.flush()
        while True:
            item = receive(self.rfile)
            if item is None or item[0] == 8:
                break
            opcode, data = item
            if opcode == 9:
                self.wfile.write(frame(data, 10))
            elif opcode == 1:
                value = json.loads(data)
                if value[0] == "REQ":
                    self.server.count("subscriptions")
                    self.wfile.write(frame(["EOSE", value[1]]))
                elif value[0] in {"EVENT", "AUTH"}:
                    self.wfile.write(frame(["OK", value[1]["id"], True, ""]))
            self.wfile.flush()


if __name__ == "__main__":
    root = Path(sys.argv[1])
    server = Relay(root)
    (root / "relay-port").write_text(str(server.server_port))
    server.serve_forever()
