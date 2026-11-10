import logging
import sys
import threading
import unittest

from smarts_client import LanguageServerClient

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


if __name__ == "__main__":
    unittest.main()
