"""Loopback-only streaming model for disposable signed-reply qualification."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send(self, data, *, code=200, stream=False):
        if stream:
            body = b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in data) + b"data: [DONE]\n\n"
        else:
            body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/fixture":
            self.send(self.server.counts)
        elif self.path == "/v1/models":
            self.send({"object": "list", "data": [{"id": "fixture-model", "object": "model", "created": 0, "owned_by": "fixture"}]})
        else:
            self.send({"error": "unknown fixture route"}, code=404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self.send({"error": "unknown fixture route"}, code=404)
        try:
            size = int(self.headers.get("Content-Length", ""))
            if not 0 < size <= 8 * 1024 * 1024:
                raise ValueError()
            request = json.loads(self.rfile.read(size))
            role = request["messages"][-1]["role"]
            tool_names = {tool["function"]["name"] for tool in request.get("tools", [])}
        except (ValueError, TypeError, KeyError, IndexError):
            return self.send({"error": "invalid fixture request"}, code=400)
        self.server.counts["requests"] += 1
        token = (self.server.directory / "provider-token").read_text().strip()
        if self.headers.get("Authorization") != "Bearer " + token:
            self.server.counts["unauthorized"] += 1
            return self.send({"error": {"message": "API Error: 401 fixture authorization refused", "code": "invalid_api_key"}}, code=401)

        arm = self.server.directory / "arm"
        prompt = arm.read_text().strip() if role == "user" and arm.is_file() else ""
        target = re.fullmatch(r"([a-f0-9-]{36}) ([a-f0-9]{64})", prompt)
        message, finish = {"role": "assistant", "content": "Fixture turn complete."}, "stop"
        if target and "shell" in tool_names:
            channel, event = target.groups()
            command = f"buzz messages send --channel {channel} --reply-to {event} --content 'Signed fixture reply'"
            call = {"id": "call-fixture", "type": "function", "function": {"name": "shell", "arguments": json.dumps({"command": command})}}
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
            self.server.counts["shell_calls"] += 1

        base = {"id": "chatcmpl-fixture", "created": 1, "model": "fixture-model"}
        usage = {"prompt_tokens": 15, "completion_tokens": 10, "total_tokens": 25}
        if request.get("stream"):
            if finish == "tool_calls":
                call["index"] = 0
            metadata = {**base, "object": "chat.completion.chunk"}
            self.send([{**metadata, "choices": [{"index": 0, "delta": message, "finish_reason": None}]},
                       {**metadata, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage}], stream=True)
        else:
            self.send({**base, "object": "chat.completion", "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable", action="store_true", required=True)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if os.getuid() == 0:
        parser.error("Run the fixture as an unprivileged user.")
    with ThreadingHTTPServer(("127.0.0.1", 8765), Model) as server:
        server.counts = {"requests": 0, "unauthorized": 0, "shell_calls": 0}
        server.directory = args.directory
        server.serve_forever()


if __name__ == "__main__":
    main()
