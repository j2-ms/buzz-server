"""Bounded subprocess transport, with locks held through child cleanup."""
import os
import selectors
import signal
import subprocess
import tempfile
import time


def run(argv, *, env, cwd, umask, input=None, stdin=None, capture_output=True, timeout: float | None = 30):
    child = None
    previous_term = signal.getsignal(signal.SIGTERM)
    source = None

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        if input is not None:
            if stdin is not None:
                raise ValueError("conflicting stdin")
            source = tempfile.TemporaryFile()
            source.write(input)
            source.seek(0)
            stdin = source
        child = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE if capture_output else None,
                                 stderr=subprocess.DEVNULL if capture_output else None,
                                 env=env, cwd=cwd, umask=umask)
        output = bytearray()
        if capture_output:
            if timeout is None:
                raise ValueError("captured commands require a deadline")
            deadline = time.monotonic() + timeout
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in selector.select(min(remaining, 0.1)):
                        block = os.read(key.fileobj.fileno(), 65536)
                        if not block:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(block)
                            if len(output) > 65536:
                                raise OSError("bounded command output exceeded")
            code = child.wait(timeout=max(0.001, deadline - time.monotonic()))
        else:
            code = child.wait(timeout=timeout)
        return subprocess.CompletedProcess(argv, code, bytes(output), b"")
    finally:
        previous_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if child is not None:
                if child.poll() is None:
                    # sudo forwards this signal to its monitored target child.
                    child.terminate()
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                if child.stdout is not None:
                    child.stdout.close()
            if source is not None:
                source.close()
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
