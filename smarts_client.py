import json
import logging
import subprocess
import threading
import uuid
from queue import Queue
from typing import Any, Callable, Dict, Optional

from .smarts_typing import SmartsServerConfig, LSPMessage


class LanguageServerClient:
    def __init__(
        self,
        logger: logging.Logger,
        config: SmartsServerConfig,
        on_send: Optional[Callable[[LSPMessage], None]] = None,
        on_receive: Optional[Callable[[LSPMessage], None]] = None,
    ):
        self._logger = logger
        self._config = config
        self._server_process: Optional[subprocess.Popen] = None
        self._server_shutdown = threading.Event()
        self._server_initialized = False
        self._server_info: Optional[dict] = None
        self._server_capabilities: Optional[dict] = None
        self._on_send = on_send
        self._on_receive = on_receive
        self._send_queue = Queue(maxsize=1)
        self._receive_queue = Queue(maxsize=1)
        self._reader: Optional[threading.Thread] = None
        self._writer: Optional[threading.Thread] = None
        self._handler: Optional[threading.Thread] = None
        self._request_callback: Dict[str, Callable[[Dict], None]] = {}
        self._open_documents = set()

    def capabilities_textDocumentSync(self):
        """
        Defines how text documents are synced.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentSyncOptions
        """
        if capabilities := self._server_capabilities:
            # If omitted it defaults to `TextDocumentSyncKind.None`.
            textDocumentSync = capabilities.get(
                "textDocumentSync",
                {
                    "change": 0,
                },
            )

            # Is either a detailed structure defining each notification
            # or for backwards compatibility the TextDocumentSyncKind number.
            if not isinstance(textDocumentSync, dict):
                textDocumentSync = {
                    "change": textDocumentSync,
                }

            return textDocumentSync

    def _read(self, out, n):
        remaining = n

        chunks = []

        while remaining > 0:
            chunk = out.read(remaining)

            # End of file or stream
            if not chunk:
                break

            chunks.append(chunk)

            remaining -= len(chunk)

        return b"".join(chunks)

    def _start_reader(self):
        self._logger.debug(f"[{self._config['name']}] Reader started 🟢")

        while not self._server_shutdown.is_set():
            out = self._server_process.stdout

            # The base protocol consists of a header and a content part (comparable to HTTP).
            # The header and content part are separated by a ‘\r\n’.
            #
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#baseProtocol

            # -- HEADER

            headers = {}

            while True:
                line = out.readline().decode("ascii").strip()

                if line == "":
                    break

                k, v = line.split(": ", 1)

                headers[k] = v

            # -- CONTENT

            if content_length := headers.get("Content-Length"):
                content = self._read(out, int(content_length)).decode("utf-8").strip()

                try:
                    message = json.loads(content)

                    # Enqueue message; Blocks if queue is full.
                    self._receive_queue.put(message)

                except json.JSONDecodeError:
                    # The effect of not being able to decode a message,
                    # is that an 'in-flight' request won't have its callback called.
                    self._logger.error(f"Failed to decode message: {content}")

        self._logger.debug(f"[{self._config['name']}] Reader stopped 🔴")

    def _start_writer(self):
        self._logger.debug(f"[{self._config['name']}] Writer started 🟢")

        while (message := self._send_queue.get()) is not None:
            try:
                content = json.dumps(message)

                header = f"Content-Length: {len(content)}\r\n\r\n"

                try:
                    encoded = header.encode("ascii") + content.encode("utf-8")
                    self._server_process.stdin.write(encoded)
                    self._server_process.stdin.flush()
                except BrokenPipeError as e:
                    self._logger.error(
                        f"{self._config['name']} - Can't write to server's stdin: {e}"
                    )

                if self._on_send:
                    try:
                        self._on_send(message)
                    except Exception:
                        self._logger.exception("Error handling sent message")

            finally:
                self._send_queue.task_done()

        # 'None Task' is complete.
        self._send_queue.task_done()

        self._logger.debug(f"[{self._config['name']}] Writer stopped 🔴")

    def _start_handler(self):
        self._logger.debug(f"[{self._config['name']}] Handler started 🟢")

        while (message := self._receive_queue.get()) is not None:  # noqa
            if self._on_receive:
                try:
                    self._on_receive(message)
                except Exception:
                    self._logger.exception("Error handling received message")

            if request_id := message.get("id"):
                if callback := self._request_callback.get(request_id):
                    try:
                        callback(message)
                    except Exception:
                        self._logger.exception(
                            f"{self._config['name']} - Request callback error"
                        )
                    finally:
                        del self._request_callback[request_id]

            self._receive_queue.task_done()

        # 'None Task' is complete.
        self._receive_queue.task_done()

        self._logger.debug(f"[{self._config['name']}] Handler stopped 🔴")

    def _put(
        self,
        message: LSPMessage,
        callback: Optional[Callable[[LSPMessage], None]] = None,
        on_put: Optional[Callable[[], None]] = None,
    ):
        # Drop message if server is not ready - unless it's an initization message.
        if not self._server_initialized and not message["method"] == "initialize":
            self._logger.debug(
                f"Server {self._config['name']} is not initialized; Will drop {message['method']}"
            )

            return

        self._send_queue.put(message)

        if on_put:
            on_put()

        if message_id := message.get("id"):
            # A mapping of request ID to callback.
            #
            # callback will be called once the response for the request is received.
            #
            # callback might not be called if there's an error reading the response,
            # or the server never returns a response.
            if callback:
                self._request_callback[message_id] = callback

    def initialize(self, params, callback):
        """
        The initialize request is sent as the first request from the client to the server.
        Until the server has responded to the initialize request with an InitializeResult,
        the client must not send any additional requests or notifications to the server.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initialize
        """

        if self._server_initialized:
            return

        self._logger.debug(f"Initialize {self._config['name']} {self._config['start']}")

        self._server_process = subprocess.Popen(
            self._config["start"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._logger.info(
            f"{self._config['name']} is up and running; PID {self._server_process.pid}"
        )

        # Thread responsible for handling received messages.
        self._handler = threading.Thread(
            name="Handler",
            target=self._start_handler,
            daemon=True,
        )
        self._handler.start()

        # Thread responsible for sending/writing messages.
        self._writer = threading.Thread(
            name="Writer",
            target=self._start_writer,
            daemon=True,
        )
        self._writer.start()

        # Thread responsible for reading messages.
        self._reader = threading.Thread(
            name="Reader",
            target=self._start_reader,
            daemon=True,
        )
        self._reader.start()

        def _callback(response):
            self._server_initialized = True
            self._server_capabilities = response.get("result").get("capabilities")
            self._server_info = response.get("result").get("serverInfo")

            self._put(
                {
                    "jsonrpc": "2.0",
                    "method": "initialized",
                    "params": {},
                }
            )

            callback(response)

        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "initialize",
                "params": params,
            },
            _callback,
        )

    def shutdown(self, callback=None):
        """
        The shutdown request is sent from the client to the server.
        It asks the server to shut down,
        but to not exit (otherwise the response might not be delivered correctly to the client).
        There is a separate exit notification that asks the server to exit.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown
        """

        if self._server_shutdown.is_set():
            self._logger.info(f"Server {self._config['name']} is down")
            return

        self._logger.info(f"Shutdown {self._config['name']}")

        def _callback(message):
            self.exit()

            if callback:
                callback(message)

        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "shutdown",
                "params": {},
            },
            _callback,
        )

    def exit(self):
        """
        A notification to ask the server to exit its process.
        The server should exit with success code 0 if the shutdown request has been received before;
        otherwise with error code 1.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#exit
        """
        self._logger.info(f"Exit {self._config['name']}")

        self._put(
            {
                "jsonrpc": "2.0",
                "method": "exit",
                "params": {},
            }
        )

        self._server_shutdown.set()

        # Enqueue `None` to signal that workers must stop:
        self._send_queue.put(None)
        self._receive_queue.put(None)

        returncode = None

        try:
            returncode = self._server_process.wait(30)
        except subprocess.TimeoutExpired:
            # Explicitly kill the process if it did not terminate.
            self._server_process.kill()

            returncode = self._server_process.wait()

        self._logger.info(
            f"{self._config['name']} terminated with returncode {returncode}"
        )

    def textDocument_didOpen(self, params):
        """
        The document open notification is sent from the client to the server
        to signal newly opened text documents.

        The document’s content is now managed by the client
        and the server must not try to read the document’s content using the document’s Uri.

        Open in this sense means it is managed by the client.
        It doesn’t necessarily mean that its content is presented in an editor.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didOpen
        """

        # An open notification must not be sent more than once without a corresponding close notification send before.
        # This means open and close notification must be balanced and the max open count for a particular textDocument is one.
        textDocument_uri = params["textDocument"]["uri"]

        if textDocument_uri in self._open_documents:
            return

        self._put(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": params,
            },
            on_put=lambda: self._open_documents.add(textDocument_uri),
        )

    def textDocument_didClose(self, params):
        """
        The document close notification is sent from the client to the server
        when the document got closed in the client.

        The document’s master now exists where
        the document’s Uri points to (e.g. if the document’s Uri is a file Uri the master now exists on disk).

        As with the open notification the close notification
        is about managing the document’s content.
        Receiving a close notification doesn’t mean that the document was open in an editor before.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didClose
        """

        textDocument_uri = params["textDocument"]["uri"]

        # A close notification requires a previous open notification to be sent.
        if textDocument_uri not in self._open_documents:
            return

        self._put(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didClose",
                "params": params,
            },
            on_put=lambda: self._open_documents.remove(textDocument_uri),
        )

    def textDocument_didChange(self, params):
        """
        The document change notification is sent from the client to the server to signal changes to a text document.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didChange
        """

        # Before a client can change a text document it must claim
        # ownership of its content using the textDocument/didOpen notification.
        if params["textDocument"]["uri"] not in self._open_documents:
            return

        self._put(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": params,
            }
        )

    def textDocument_hover(self, params, callback):
        """
        The hover request is sent from the client to the server to request
        hover information at a given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_hover
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/hover",
                "params": params,
            },
            callback,
        )

    def textDocument_definition(self, params, callback):
        """
        The go to definition request is sent from the client to the server
        to resolve the definition location of a symbol at a given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_definition
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/definition",
                "params": params,
            },
            callback,
        )

    def textDocument_references(self, params, callback):
        """
        The references request is sent from the client to the server
        to resolve project-wide references for the symbol denoted by the given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_references
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/references",
                "params": params,
            },
            callback,
        )

    def textDocument_documentHighlight(self, params, callback):
        """
        The document highlight request is sent from the client to
        the server to resolve document highlights for a given text document position.

        For programming languages this usually highlights all references to the symbol scoped to this file.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentHighlight
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/documentHighlight",
                "params": params,
            },
            callback,
        )

    def textDocument_documentSymbol(self, params, callback):
        """
        The document symbol request is sent from the client to the server.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentSymbol
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/documentSymbol",
                "params": params,
            },
            callback,
        )

    def textDocument_formatting(self, params, callback):
        """
        The document formatting request is sent from the client to the server to format a whole document.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_formatting
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/formatting",
                "params": params,
            },
            callback,
        )
