import json
import logging
import sys
import threading
import time
import unittest

from smarts_client import (
    CLIENT_ERROR,
    LanguageServerClient,
    LanguageServerStatus,
    notification,
    request,
    textDocumentSyncOptions,
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
        )

        initialized = threading.Event()

        client.initialize({}, lambda _: initialized.set())

        try:
            self.assertTrue(initialized.wait(5), "Server blocked writing to stderr")
            self.assertTrue(client.is_server_initialized())
        finally:
            client._server_process.kill()


# A minimal language server: answers 'initialize', sends two requests,
# and echoes the client's responses back in 'window/logMessage' notifications.
FAKE_SERVER_REQUESTS = r"""
import json, sys

def read():
    headers = {}
    while (line := sys.stdin.buffer.readline().decode().strip()):
        k, v = line.split(": ", 1)
        headers[k] = v
    return json.loads(sys.stdin.buffer.read(int(headers["Content-Length"])))

def write(message):
    content = json.dumps(message)
    sys.stdout.buffer.write(f"Content-Length: {len(content)}\r\n\r\n{content}".encode())
    sys.stdout.buffer.flush()

initialize = read()
write({"jsonrpc": "2.0", "id": initialize["id"], "result": {"capabilities": {}}})

# Note: 0 is a valid request ID.
write({"jsonrpc": "2.0", "id": 0, "method": "workspace/configuration",
       "params": {"items": [{"section": "a"}, {"section": "b"}]}})
write({"jsonrpc": "2.0", "id": 1, "method": "unknown/method", "params": {}})

responses = 0
while responses < 2:
    message = read()
    if "method" not in message:
        responses += 1
        write({"jsonrpc": "2.0", "method": "window/logMessage",
               "params": {"type": 3, "message": json.dumps(message)}})

sys.stdin.buffer.read()
"""


class TestServerRequests(unittest.TestCase):
    def test_server_requests_are_answered(self):
        responses = []
        received = threading.Event()

        def on_log_message(notification):
            responses.append(json.loads(notification["params"]["message"]))

            if len(responses) == 2:
                received.set()

        client = LanguageServerClient(
            logger=logging.getLogger("test"),
            name="Fake",
            server_args=[sys.executable, "-c", FAKE_SERVER_REQUESTS],
            on_notification=on_log_message,
        )

        client.initialize({}, lambda _: None)

        try:
            self.assertTrue(received.wait(5), "Server requests were not answered")
        finally:
            client._server_process.kill()

        by_id = {response["id"]: response for response in responses}

        self.assertEqual(by_id[0], {"jsonrpc": "2.0", "id": 0, "result": [None, None]})
        self.assertEqual(by_id[1]["error"]["code"], -32601)


# A minimal language server: answers 'initialize' - with `RESPONSE` - then
# exits with `EXIT_CODE`, or waits until it's killed if `EXIT_CODE` is None.
FAKE_SERVER_LIFECYCLE = r"""
import json, sys, time

headers = {}
while (line := sys.stdin.buffer.readline().decode().strip()):
    k, v = line.split(": ", 1)
    headers[k] = v

request = json.loads(sys.stdin.buffer.read(int(headers["Content-Length"])))

content = json.dumps({"jsonrpc": "2.0", "id": request["id"], **RESPONSE})
sys.stdout.buffer.write(f"Content-Length: {len(content)}\r\n\r\n{content}".encode())
sys.stdout.buffer.flush()

if EXIT_CODE is None:
    time.sleep(60)
else:
    time.sleep(0.2)
    sys.exit(EXIT_CODE)
"""


def lifecycle_client(response: dict, exit_code: int | None) -> LanguageServerClient:
    server = f"RESPONSE = {response!r}\nEXIT_CODE = {exit_code!r}\n" + (
        FAKE_SERVER_LIFECYCLE
    )

    return LanguageServerClient(
        logger=logging.getLogger("test"),
        name="Fake",
        server_args=[sys.executable, "-c", server],
    )


def wait_for(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if predicate():
            return True

        time.sleep(0.01)

    return False


class TestFailure(unittest.TestCase):
    def test_server_crash_fails_client(self):
        client = lifecycle_client({"result": {"capabilities": {}}}, exit_code=3)

        initialized = threading.Event()

        client.initialize({}, lambda _: initialized.set())

        self.assertTrue(initialized.wait(5))
        self.assertTrue(wait_for(client.is_server_failed), "Crash was not detected")
        self.assertEqual(client._server_process.returncode, 3)

    def test_rejected_initialization_kills_server(self):
        error = {"code": -32603, "message": "Nope", "data": None}

        client = lifecycle_client({"error": error}, exit_code=None)

        responded = threading.Event()

        client.initialize({}, lambda _: responded.set())

        self.assertTrue(responded.wait(5))
        self.assertTrue(client.is_server_failed())

        # The server would otherwise linger for 60s.
        self.assertTrue(
            wait_for(lambda: client._server_process.poll() is not None),
            "Failed server was not killed",
        )


# A minimal language server: answers 'initialize', reads one more message
# - which it never answers - and crashes.
FAKE_SERVER_CRASH_ON_REQUEST = r"""
import json, sys

def read():
    headers = {}
    while (line := sys.stdin.buffer.readline().decode().strip()):
        k, v = line.split(": ", 1)
        headers[k] = v
    return json.loads(sys.stdin.buffer.read(int(headers["Content-Length"])))

initialize = read()
content = json.dumps({"jsonrpc": "2.0", "id": initialize["id"], "result": {"capabilities": {}}})
sys.stdout.buffer.write(f"Content-Length: {len(content)}\r\n\r\n{content}".encode())
sys.stdout.buffer.flush()

while read().get("method") != "textDocument/hover":
    pass

sys.exit(1)
"""


class TestFailureCallbacks(unittest.TestCase):
    def test_crash_during_initialization_calls_callback(self):
        failures = []

        client = LanguageServerClient(
            logger=logging.getLogger("test"),
            name="Fake",
            server_args=[sys.executable, "-c", "import sys; sys.exit(7)"],
            on_failure=failures.append,
        )

        responses = []
        responded = threading.Event()

        def callback(response):
            responses.append(response)
            responded.set()

        client.initialize({}, callback)

        self.assertTrue(responded.wait(5), "Callback was not called")
        self.assertTrue(client.is_server_failed())
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["code"], CLIENT_ERROR)
        self.assertTrue(wait_for(lambda: len(failures) == 1))

    def test_initialization_timeout_calls_callback_once(self):
        failures = []

        # Never answers.
        client = LanguageServerClient(
            logger=logging.getLogger("test"),
            name="Fake",
            server_args=[sys.executable, "-c", "import time; time.sleep(60)"],
            on_failure=failures.append,
        )

        responses = []
        responded = threading.Event()

        def callback(response):
            responses.append(response)
            responded.set()

        client.initialize({}, callback, timeout=0.2)

        self.assertTrue(responded.wait(5), "Callback was not called")
        self.assertTrue(client.is_server_failed())
        self.assertIn("timed out", responses[0]["error"]["message"])
        self.assertTrue(
            wait_for(lambda: client._server_process.poll() is not None),
            "Hung server was not killed",
        )

        time.sleep(0.1)

        self.assertEqual(len(responses), 1)
        self.assertEqual(len(failures), 1)

    def test_failure_to_start_calls_callbacks(self):
        failures = []

        client = LanguageServerClient(
            logger=logging.getLogger("test"),
            name="Fake",
            server_args=["/nonexistent/language-server"],
            on_failure=failures.append,
        )

        responses = []

        client.initialize({}, responses.append)

        self.assertTrue(client.is_server_failed())
        self.assertEqual(responses[0]["error"]["code"], CLIENT_ERROR)
        self.assertEqual(len(failures), 1)

    def test_pending_requests_are_answered_when_server_crashes(self):
        failures = []

        client = LanguageServerClient(
            logger=logging.getLogger("test"),
            name="Fake",
            server_args=[sys.executable, "-c", FAKE_SERVER_CRASH_ON_REQUEST],
            on_failure=failures.append,
        )

        initialized = threading.Event()

        client.initialize({}, lambda _: initialized.set())

        self.assertTrue(initialized.wait(5))

        results = []
        errors = []
        answered = threading.Event()

        def on_error(error):
            errors.append(error)
            answered.set()

        client.textDocument_hover({}, results.append, on_error)  # type: ignore

        self.assertTrue(answered.wait(5), "Pending request was not answered")
        self.assertEqual(results, [])
        self.assertEqual(errors[0]["code"], CLIENT_ERROR)
        self.assertTrue(wait_for(lambda: len(failures) == 1))

    def test_shutdown_is_not_a_failure(self):
        failures = []

        client = LanguageServerClient(
            logger=logging.getLogger("test"),
            name="Fake",
            server_args=[sys.executable, "-c", FAKE_SERVER_SHUTDOWN],
            on_failure=failures.append,
        )

        initialized = threading.Event()

        client.initialize({}, lambda _: initialized.set())

        self.assertTrue(initialized.wait(5))

        client.shutdown()

        self.assertTrue(wait_for(client.is_server_shutdown), "Server didn't shutdown")
        self.assertTrue(wait_for(lambda: client._server_process.poll() is not None))

        time.sleep(0.1)

        self.assertEqual(failures, [])


# A minimal language server: answers 'initialize' and 'shutdown', exits on 'exit'.
FAKE_SERVER_SHUTDOWN = r"""
import json, sys

def read():
    headers = {}
    while (line := sys.stdin.buffer.readline().decode().strip()):
        k, v = line.split(": ", 1)
        headers[k] = v
    return json.loads(sys.stdin.buffer.read(int(headers["Content-Length"])))

def write(message):
    content = json.dumps(message)
    sys.stdout.buffer.write(f"Content-Length: {len(content)}\r\n\r\n{content}".encode())
    sys.stdout.buffer.flush()

while True:
    message = read()
    method = message.get("method")
    if method == "initialize":
        write({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
    elif method == "shutdown":
        write({"jsonrpc": "2.0", "id": message["id"], "result": None})
    elif method == "exit":
        sys.exit(0)
"""


def initialized_client() -> LanguageServerClient:
    """A client marked as initialized, without a server or worker threads."""
    client = LanguageServerClient(
        logger=logging.getLogger("test"),
        name="Fake",
        server_args=[],
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


class TestCapabilities(unittest.TestCase):
    def test_text_document_sync_options(self):
        self.assertEqual(
            textDocumentSyncOptions(None), {"openClose": False, "change": 0}
        )
        self.assertEqual(textDocumentSyncOptions(2), {"openClose": True, "change": 2})
        # Missing options get defaults.
        self.assertEqual(
            textDocumentSyncOptions({"change": 1}), {"openClose": False, "change": 1}
        )

    def test_support_method(self):
        client = initialized_client()

        self.assertIsNone(client.support_method("textDocument/hover"))

        client._server_capabilities = {"hoverProvider": True}  # type: ignore
        client._text_document_sync = textDocumentSyncOptions(2)

        self.assertTrue(client.support_method("textDocument/hover"))
        self.assertFalse(client.support_method("textDocument/rename"))
        self.assertFalse(client.support_method("unknown/method"))
        self.assertTrue(client.support_method("textDocument/didOpen"))
        self.assertTrue(client.support_method("textDocument/didChange"))


if __name__ == "__main__":
    unittest.main()
