import logging
import sys
import threading
import unittest

from smarts_client import (
    LanguageServerClient,
    LanguageServerStatus,
    notification,
    request,
)

# A minimal language server: floods stderr, then answers 'initialize'.
FAKE_SERVER = r"""
import json, sys

sys.stderr.write("x" * 1_000_000)
sys.stderr.flush()

headers = {}
while (line := sys.stdin.buffer.readline().decode().strip()):
    k, v = line.split(": ", 1)
    headers[k] = v

request = json.loads(sys.stdin.buffer.read(int(headers["Content-Length"])))

content = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"capabilities": {}}})
sys.stdout.buffer.write(f"Content-Length: {len(content)}\r\n\r\n{content}".encode())
sys.stdout.buffer.flush()

sys.stdin.buffer.read()
"""


class TestStderr(unittest.TestCase):
    def test_server_flooding_stderr_does_not_block(self):
        client = LanguageServerClient(
            logger=logging.getLogger("test"),
            name="Fake",
            server_args=[sys.executable, "-c", FAKE_SERVER],
            before_write=None,
            after_read=None,
        )

        initialized = threading.Event()

        client.initialize({}, lambda _: initialized.set())

        try:
            self.assertTrue(initialized.wait(5), "Server blocked writing to stderr")
            self.assertTrue(client.is_server_initialized())
        finally:
            client._server_process.kill()


def initialized_client() -> LanguageServerClient:
    """A client marked as initialized, without a server or worker threads."""
    client = LanguageServerClient(
        logger=logging.getLogger("test"),
        name="Fake",
        server_args=[],
        before_write=None,
        after_read=None,
    )
    client._server_status = LanguageServerStatus.INITIALIZED

    return client


class TestPut(unittest.TestCase):
    def test_put_does_not_block_when_nothing_is_sent(self):
        client = initialized_client()

        # No writer thread: nothing ever leaves the send queue.
        def put_many():
            for _ in range(1000):
                client._put(notification("textDocument/didChange", {}))

        t = threading.Thread(target=put_many, daemon=True)
        t.start()
        t.join(2)

        self.assertFalse(t.is_alive(), "_put blocked on a full send queue")
        self.assertEqual(client._send_queue.qsize(), 1000)

    def test_put_enqueues_without_holding_the_lock(self):
        client = initialized_client()

        lock_held_on_put = []

        class SpyQueue:
            def put(self, _):
                lock_held_on_put.append(client._lock._is_owned())

        client._send_queue = SpyQueue()

        message = request("textDocument/hover", {})
        callback = lambda _: None

        client._put(message, callback)

        self.assertEqual(lock_held_on_put, [False])
        self.assertIs(client._request_callback[message["id"]], callback)


if __name__ == "__main__":
    unittest.main()
