#!/usr/bin/python3
"""Synthetic ACP adapter; no inference, login, tools or external connection.

Uses the same `goose acp` invocation boundary as the supported harness.
"""
import json
import os
from pathlib import Path
import sys


def serve(source, sink):
    for line in source:
        request = json.loads(line)
        method = request.get("method")
        if "id" not in request:
            continue
        if method == "initialize":
            result = {"protocolVersion": 1, "agentCapabilities": {"loadSession": False},
                      "agentInfo": {"name": "buzz-synthetic-acp", "version": "1"}, "authMethods": []}
            # Marker contains no config or environment values.
            if os.environ.get("HOME"):
                Path(os.environ["HOME"], "fixture-acp-initialized").touch(mode=0o600)
                assert not any(key in os.environ for key in ("GOOSE_PROVIDER", "GOOSE_MODEL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"))
                assert os.environ.get("GOOSE_DISABLE_KEYRING") == "1"
                Path(os.environ["HOME"], "fixture-managed-acp-initialized").touch(mode=0o600)
        elif method == "session/new":
            result = {"sessionId": "synthetic-session", "models": {"currentModelId": "fixture-model", "availableModels": [{"modelId": "fixture-model", "name": "Synthetic"}]}}
        elif method == "session/prompt":
            result = {"stopReason": "end_turn"}
        else:
            result = {}
        sink.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n")
        sink.flush()


if __name__ == "__main__":
    if sys.argv[1:] in (["acp", "--help"], ["configure", "--help"]):
        print(f"Usage: goose {sys.argv[1]} (synthetic fixture)")
        sys.exit(0)
    if sys.argv[1:] != ["acp"]:
        sys.exit(1)
    serve(sys.stdin, sys.stdout)
