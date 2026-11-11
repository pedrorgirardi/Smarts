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

# What every fake language server starts with: reading and writing messages.
FAKE_SERVER_IO = r"""
import json, sys, time

def read():
    headers = {}
    while (line := sys.stdin.buffer.readline().decode().strip()):
        k, v = line.split(": ", 1)
        headers[k] = v
    return json.loads(sys.stdin.buffer.read(int(headers["Content-Length"])))

def write(message, header="Content-Length"):
    content = json.dumps(message).encode()
    sys.stdout.buffer.write(f"{header}: {len(content)}\r\n\r\n".encode() + content)
    sys.stdout.buffer.flush()

def respond(request, **response):
    write({"jsonrpc": "2.0", "id": request["id"], **response})

def notify(method, params):
    write({"jsonrpc": "2.0", "method": method, "params": params})

def initialize():
    respond(read(), result={"capabilities": {}})

def wait_until_killed():
    sys.stdin.buffer.read()
"""


def fake_client(server: str, **kwargs) -> LanguageServerClient:
    """
    A client of a fake language server: a Python script, `server`,
    which can use the functions of FAKE_SERVER_IO.
    """
    return LanguageServerClient(
        logger=logging.getLogger("test"),
        name="Fake",
        server_args=[sys.executable, "-c", FAKE_SERVER_IO + server],
        **kwargs,
    )


def initialize(client: LanguageServerClient):
    """Initialize `client`, and wait for the response."""
    initialized = threading.Event()

    client.initialize({}, lambda _: initialized.set())

    assert initialized.wait(5), "Server didn't answer 'initialize'"


def initialized_client() -> LanguageServerClient:
    """A client marked as initialized, without a server or worker threads."""
    client = LanguageServerClient(
        logger=logging.getLogger("test"),
        name="Fake",
        server_args=[],
    )
    client._server_status = LanguageServerStatus.INITIALIZED

    return client


def wait_for(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if predicate():
            return True

        time.sleep(0.01)

    return False


def kill(client: LanguageServerClient):
    if process := client._server_process:
        process.kill()


# Echoes the messages it receives in 'window/logMessage' notifications.
FAKE_SERVER_ECHO = r"""
initialize()

while True:
    notify("window/logMessage", {"type": 3, "message": json.dumps(read())})
"""


def echo_client(echoed: list) -> LanguageServerClient:
    """An initialized client of a server which echoes messages into `echoed`."""
    client = fake_client(
        FAKE_SERVER_ECHO,
        on_notification=lambda n: echoed.append(json.loads(n["params"]["message"])),
    )

    initialize(client)

    return client


class TestStderr(unittest.TestCase):
    def test_server_flooding_stderr_does_not_block(self):
        client = fake_client(
            r"""
sys.stderr.write("x" * 1_000_000)
sys.stderr.flush()

initialize()
wait_until_killed()
"""
        )

        initialized = threading.Event()

        client.initialize({}, lambda _: initialized.set())

        try:
            self.assertTrue(initialized.wait(5), "Server blocked writing to stderr")
            self.assertTrue(client.is_server_initialized())
        finally:
            kill(client)


class TestServerRequests(unittest.TestCase):
    def test_server_requests_are_answered(self):
        responses = []

        # Sends two requests, and echoes the responses.
        client = fake_client(
            r"""
initialize()

# Note: 0 is a valid request ID.
write({"jsonrpc": "2.0", "id": 0, "method": "workspace/configuration",
       "params": {"items": [{"section": "a"}, {"section": "b"}]}})
write({"jsonrpc": "2.0", "id": 1, "method": "unknown/method", "params": {}})

while True:
    message = read()
    if "method" not in message:
        notify("window/logMessage", {"type": 3, "message": json.dumps(message)})
""",
            on_notification=lambda n: responses.append(
                json.loads(n["params"]["message"])
            ),
        )

        client.initialize({}, lambda _: None)

        try:
            self.assertTrue(
                wait_for(lambda: len(responses) == 2),
                "Server requests were not answered",
            )
        finally:
            kill(client)

        by_id = {response["id"]: response for response in responses}

        self.assertEqual(by_id[0], {"jsonrpc": "2.0", "id": 0, "result": [None, None]})
        self.assertEqual(by_id[1]["error"]["code"], -32601)


class TestResponses(unittest.TestCase):
    # Answers 'textDocument/hover' with an error, anything else with its method.
    SERVER = r"""
initialize()

while True:
    message = read()
    if "id" in message:
        if message["method"] == "textDocument/hover":
            respond(message, error={"code": -32603, "message": "Nope", "data": None})
        else:
            respond(message, result=message["method"])
    else:
        notify("test/notification", message["method"])
"""

    def test_result_and_error_reach_their_callbacks(self):
        client = fake_client(self.SERVER)

        initialize(client)

        try:
            results = []
            errors = []

            client.textDocument_hover({}, results.append, errors.append)  # type: ignore
            client.textDocument_definition({}, results.append, errors.append)  # type: ignore

            self.assertTrue(wait_for(lambda: len(results) + len(errors) == 2))
            self.assertEqual(results, ["textDocument/definition"])
            self.assertEqual(errors[0]["code"], -32603)
        finally:
            kill(client)

    def test_callback_errors_do_not_stop_handler(self):
        def raise_error(_):
            raise RuntimeError("Callback error")

        notifications = []

        def on_notification(notification):
            notifications.append(notification)

            raise RuntimeError("Notification handler error")

        client = fake_client(self.SERVER, on_notification=on_notification)

        initialize(client)

        try:
            results = []

            client.textDocument_definition({}, raise_error)  # type: ignore
            client._put(notification("test/a"))
            client.textDocument_definition({}, results.append)  # type: ignore

            self.assertTrue(
                wait_for(lambda: len(results) == 1),
                "Handler stopped after an error in a callback",
            )
            # ('initialized' is a notification too.)
            self.assertIn("test/a", [n["params"] for n in notifications])
        finally:
            kill(client)


def lifecycle_client(
    response: dict, exit_code: int | None, **kwargs
) -> LanguageServerClient:
    """
    A client of a server which answers 'initialize' with `response`, then exits
    with `exit_code` - or waits until it's killed, if `exit_code` is None.
    """
    return fake_client(
        f"""
respond(read(), **{response!r})

if {exit_code!r} is None:
    time.sleep(60)
else:
    time.sleep(0.2)
    sys.exit({exit_code!r})
""",
        **kwargs,
    )


class TestFailure(unittest.TestCase):
    def test_server_crash_fails_client(self):
        client = lifecycle_client({"result": {"capabilities": {}}}, exit_code=3)

        initialize(client)

        self.assertTrue(wait_for(client.is_server_failed), "Crash was not detected")
        self.assertEqual(client._server_process.returncode, 3)

    def test_rejected_initialization_kills_server(self):
        error = {"code": -32603, "message": "Nope", "data": None}

        failures = []

        client = lifecycle_client(
            {"error": error}, exit_code=None, on_failure=failures.append
        )

        responses = []

        client.initialize({}, responses.append)

        self.assertTrue(wait_for(lambda: len(responses) == 1))
        self.assertEqual(responses[0]["error"], error)
        self.assertTrue(client.is_server_failed())
        self.assertEqual(len(failures), 1)

        # The server would otherwise linger for 60s.
        self.assertTrue(
            wait_for(lambda: client._server_process.poll() is not None),
            "Failed server was not killed",
        )

    def test_crash_during_initialization_calls_callback(self):
        failures = []

        client = fake_client("sys.exit(7)", on_failure=failures.append)

        responses = []

        client.initialize({}, responses.append)

        self.assertTrue(wait_for(lambda: len(responses) == 1), "Callback not called")
        self.assertTrue(client.is_server_failed())
        self.assertEqual(responses[0]["error"]["code"], CLIENT_ERROR)
        self.assertTrue(wait_for(lambda: len(failures) == 1))

    def test_initialization_timeout_calls_callback_once(self):
        failures = []

        # Never answers.
        client = fake_client("time.sleep(60)", on_failure=failures.append)

        responses = []

        client.initialize({}, responses.append, timeout=0.2)

        self.assertTrue(wait_for(lambda: len(responses) == 1), "Callback not called")
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

    def test_failed_client_can_not_be_initialized_again(self):
        client = LanguageServerClient(
            logger=logging.getLogger("test"),
            name="Fake",
            server_args=["/nonexistent/language-server"],
        )

        responses = []

        client.initialize({}, responses.append)
        client.initialize({}, responses.append)

        self.assertEqual(len(responses), 1)
        self.assertTrue(client.is_server_failed())

    def test_pending_requests_are_answered_when_server_crashes(self):
        failures = []

        # Crashes instead of answering 'textDocument/hover'.
        client = fake_client(
            r"""
initialize()

while read().get("method") != "textDocument/hover":
    pass

sys.exit(1)
""",
            on_failure=failures.append,
        )

        initialize(client)

        results = []
        errors = []

        client.textDocument_hover({}, results.append, errors.append)  # type: ignore

        self.assertTrue(
            wait_for(lambda: len(errors) == 1), "Pending request was not answered"
        )
        self.assertEqual(results, [])
        self.assertEqual(errors[0]["code"], CLIENT_ERROR)
        self.assertTrue(wait_for(lambda: len(failures) == 1))


class TestShutdown(unittest.TestCase):
    # Answers 'shutdown' - unless IGNORE_SHUTDOWN - and exits on 'exit'.
    SERVER = r"""
initialize()

while True:
    message = read()
    method = message.get("method")
    if method == "shutdown" and not IGNORE_SHUTDOWN:
        respond(message, result=None)
    elif method == "exit":
        sys.exit(0)
"""

    def shutdown_client(self, ignore_shutdown: bool, failures: list):
        client = fake_client(
            f"IGNORE_SHUTDOWN = {ignore_shutdown!r}\n" + self.SERVER,
            on_failure=failures.append,
        )

        initialize(client)

        return client

    def assert_shutdown(self, client: LanguageServerClient, failures: list):
        self.assertTrue(wait_for(client.is_server_shutdown), "Server didn't shutdown")
        self.assertTrue(
            wait_for(lambda: client._server_process.poll() is not None),
            "Server didn't exit",
        )
        self.assertEqual(client._server_process.returncode, 0)

        time.sleep(0.1)

        # A shutdown is not a failure - although the server exited.
        self.assertEqual(failures, [])

    def test_shutdown(self):
        failures = []

        client = self.shutdown_client(ignore_shutdown=False, failures=failures)

        client.shutdown()

        self.assert_shutdown(client, failures)

    def test_shutdown_timeout_forces_exit(self):
        failures = []

        client = self.shutdown_client(ignore_shutdown=True, failures=failures)

        client.shutdown(timeout=0.2)

        self.assert_shutdown(client, failures)

    def test_messages_are_dropped_after_shutdown(self):
        failures = []

        client = self.shutdown_client(ignore_shutdown=False, failures=failures)

        client.shutdown()

        self.assert_shutdown(client, failures)

        results = []

        client.textDocument_hover({}, results.append)  # type: ignore
        client.shutdown()

        self.assertEqual(client._request_callback, {})

    def test_shutdown_of_failed_server_is_a_noop(self):
        thread_errors = []
        excepthook = threading.excepthook
        threading.excepthook = lambda args: thread_errors.append(args.exc_value)

        try:
            client = LanguageServerClient(
                logger=logging.getLogger("test"),
                name="Fake",
                server_args=["/nonexistent/language-server"],
            )

            client.initialize({}, lambda _: None)
            client.shutdown(timeout=0.1)

            time.sleep(0.3)

            self.assertEqual(thread_errors, [])
            self.assertTrue(client.is_server_failed())
            self.assertEqual(client._send_queue.qsize(), 0)
        finally:
            threading.excepthook = excepthook


class TestReader(unittest.TestCase):
    def assert_notified_after(self, header="Content-Length", preamble=b""):
        """
        Assert that a notification is handled if its header is named `header`,
        and it's sent after `preamble` - raw bytes.
        """
        notifications = []

        client = fake_client(
            f"""
initialize()

sys.stdout.buffer.write({preamble!r})
sys.stdout.buffer.flush()

write({{"jsonrpc": "2.0", "method": "test/after", "params": {{}}}}, header={header!r})

wait_until_killed()
""",
            on_notification=notifications.append,
        )

        client.initialize({}, lambda _: None)

        try:
            self.assertTrue(
                wait_for(lambda: len(notifications) == 1),
                "Notification was not handled",
            )
            self.assertEqual(notifications[0]["method"], "test/after")
            self.assertTrue(client.is_server_initialized())
        finally:
            kill(client)

    def test_message_which_is_not_an_object_is_skipped(self):
        self.assert_notified_after(preamble=b"Content-Length: 2\r\n\r\n[]")

    def test_message_which_is_not_json_is_skipped(self):
        self.assert_notified_after(preamble=b"Content-Length: 2\r\n\r\n{x")

    def test_header_names_are_case_insensitive(self):
        self.assert_notified_after(header="content-length")

    def test_incomplete_message_fails_client(self):
        # Exits in the middle of a message.
        client = fake_client(
            r"""
initialize()

sys.stdout.buffer.write(b"Content-Length: 100\r\n\r\n{")
sys.stdout.buffer.flush()
"""
        )

        initialize(client)

        self.assertTrue(wait_for(client.is_server_failed), "EOF was not detected")


class TestWriter(unittest.TestCase):
    def test_unserializable_message_does_not_stop_writer(self):
        echoed = []

        client = echo_client(echoed)

        try:
            errors = []

            # A set is not JSON serializable.
            client.textDocument_hover({"bad": {1}}, lambda _: None, errors.append)  # type: ignore
            client._put(notification("test/good", {}))

            self.assertTrue(
                wait_for(lambda: any(m.get("method") == "test/good" for m in echoed)),
                "Writer stopped after a message it couldn't serialize",
            )
            self.assertTrue(client.is_server_initialized())

            # The request was answered - with an error.
            self.assertTrue(wait_for(lambda: len(errors) == 1))
            self.assertEqual(errors[0]["code"], CLIENT_ERROR)
        finally:
            kill(client)

    def test_content_length_counts_bytes(self):
        echoed = []

        client = echo_client(echoed)

        try:
            client._put(notification("test/text", {"text": "ação 🎉"}))

            self.assertTrue(
                wait_for(lambda: any(m.get("method") == "test/text" for m in echoed))
            )

            message = next(m for m in echoed if m.get("method") == "test/text")

            self.assertEqual(message["params"]["text"], "ação 🎉")
        finally:
            kill(client)


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

    def test_messages_are_dropped_unless_initialized(self):
        client = initialized_client()
        client._server_status = LanguageServerStatus.INITIALIZING

        client._put(request("textDocument/hover", {}), lambda _: None)

        self.assertEqual(client._send_queue.qsize(), 0)
        self.assertEqual(client._request_callback, {})


class TestOpenDocuments(unittest.TestCase):
    def methods(self, client) -> list[str]:
        return [message["method"] for message in list(client._send_queue.queue)]

    def test_open_and_close_are_balanced(self):
        client = initialized_client()

        open_params = {"textDocument": {"uri": "file:///a.py"}}

        # Change, save and close need a previous open.
        client.textDocument_didChange(open_params)  # type: ignore
        client.textDocument_didSave(open_params)  # type: ignore
        client.textDocument_didClose(open_params)

        self.assertFalse(client.is_document_open("file:///a.py"))

        client.textDocument_didOpen(open_params)  # type: ignore
        client.textDocument_didOpen(open_params)  # type: ignore

        self.assertTrue(client.is_document_open("file:///a.py"))

        client.textDocument_didChange(open_params)  # type: ignore
        client.textDocument_didSave(open_params)  # type: ignore
        client.textDocument_didClose(open_params)
        client.textDocument_didClose(open_params)

        self.assertFalse(client.is_document_open("file:///a.py"))

        self.assertEqual(
            self.methods(client),
            [
                "textDocument/didOpen",
                "textDocument/didChange",
                "textDocument/didSave",
                "textDocument/didClose",
            ],
        )

    def test_document_is_not_open_if_server_is_not_initialized(self):
        client = initialized_client()
        client._server_status = LanguageServerStatus.INITIALIZING

        open_params = {"textDocument": {"uri": "file:///a.py"}}

        client.textDocument_didOpen(open_params)  # type: ignore

        self.assertEqual(self.methods(client), [])

        # Once initialized, the document can be opened.
        client._server_status = LanguageServerStatus.INITIALIZED

        client.textDocument_didOpen(open_params)  # type: ignore

        self.assertEqual(self.methods(client), ["textDocument/didOpen"])


class TestCapabilities(unittest.TestCase):
    def test_text_document_sync_options(self):
        self.assertEqual(
            textDocumentSyncOptions(None),
            {"openClose": False, "change": 0, "save": False},
        )
        self.assertEqual(
            textDocumentSyncOptions(2),
            {"openClose": True, "change": 2, "save": True},
        )
        # Missing options get defaults.
        self.assertEqual(
            textDocumentSyncOptions({"change": 1}),
            {"openClose": False, "change": 1, "save": False},
        )
        self.assertEqual(
            textDocumentSyncOptions({"save": {"includeText": True}}),
            {"openClose": False, "change": 0, "save": {"includeText": True}},
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
        self.assertTrue(client.support_method("textDocument/didSave"))

        client._text_document_sync = textDocumentSyncOptions({"openClose": True})

        self.assertFalse(client.support_method("textDocument/didSave"))

    def test_capabilities_of_the_server_are_kept(self):
        client = fake_client(
            r"""
respond(read(), result={
    "capabilities": {"positionEncoding": "utf-8", "textDocumentSync": 2},
    "serverInfo": {"name": "Fake", "version": "1.0"},
})

wait_until_killed()
"""
        )

        initialize(client)

        try:
            self.assertEqual(client.position_encoding(), "utf-8")
            self.assertEqual(client.text_document_sync()["change"], 2)
            self.assertEqual(client._server_info, {"name": "Fake", "version": "1.0"})
        finally:
            kill(client)


if __name__ == "__main__":
    unittest.main()
