import json
import logging
import subprocess
import threading
import uuid
from queue import Queue
from typing import cast, TypedDict, Any, Literal, Callable, List, Dict, Optional, Union


class LSPMarkupContent(TypedDict):
    # The type of the Markup
    kind: Literal["plaintext", "markdown"]

    # The content itself
    value: str


class LSPServerCapabilities(TypedDict):
    positionEncoding: Optional[
        Literal[
            # Character offsets count UTF-8 code units (e.g bytes).
            "utf-8",
            #  Character offsets count UTF-16 code units.
            # This is the default and must always be supported by servers.
            "'utf-16",
            # Character offsets count UTF-32 code units.
            # Implementation note: these are the same as Unicode code points,
            # so this `PositionEncodingKind` may also be used for an
            #  encoding-agnostic representation of character offsets.
            "utf-32",
        ]
    ]


class LSPServerInfo(TypedDict):
    # The name of the server as defined by the server.
    name: str

    # The server's version as defined by the server.
    version: Optional[str]


class LSPInitializeResult(TypedDict):
    # The capabilities the language server provides.
    capabilities: LSPServerCapabilities

    # Information about the server.
    serverInfo: Optional[LSPServerInfo]


class LSPMessage(TypedDict):
    jsonrpc: str


class LSPNotificationMessage(LSPMessage):
    """
    A notification message.

    A processed notification message must not send a response back. They work like events.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#notificationMessage
    """

    method: str
    params: Optional[Any]


class LSPRequestMessage(LSPMessage):
    """
    A request message to describe a request between the client and the server.

    Every processed request must send a response back to the sender of the request.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#requestMessage
    """

    id: Union[int, str]
    method: str
    params: Optional[Any]


class LSPResponseError(TypedDict):
    code: int
    message: str
    data: Optional[Any]


class LSPResponseMessage(TypedDict):
    """
    A Response Message sent as a result of a request.

    If a request doesn’t provide a result value the receiver of a request
    still needs to return a response message to conform to the JSON-RPC specification.

    The result property of the ResponseMessage should be set to null
    in this case to signal a successful request.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#responseMessage
    """

    id: Optional[Union[int, str]]
    result: Optional[Any]
    error: Optional[LSPResponseError]


class LSPTextDocumentIdentifier(TypedDict):
    """
    Text documents are identified using a URI. On the protocol level, URIs are passed as strings.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentIdentifier
    """

    uri: str


class LSPVersionedTextDocumentIdentifier(LSPTextDocumentIdentifier):
    """
    An identifier to denote a specific version of a text document.

    This information usually flows from the client to the server.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#versionedTextDocumentIdentifier
    """

    version: int


class LSPTextDocumentItem(TypedDict):
    """
    An item to transfer a text document from the client to the server.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentItem
    """

    uri: str
    languageId: str
    version: int
    text: str


class LSPPosition(TypedDict):
    """
    Position in a text document expressed as zero-based line and zero-based character offset.

    A position is between two characters like an ‘insert’ cursor in an editor.

    Special values like for example -1 to denote the end of a line are not supported.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#position
    """

    line: int
    character: int


class LSPFormattingOptions(TypedDict):
    """
    Value-object describing what options formatting should use.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#formattingOptions
    """

    tabSize: int
    insertSpaces: bool
    insertFinalNewline: Optional[bool]
    trimTrailingWhitespace: Optional[bool]
    trimFinalNewlines: Optional[bool]


class LSPRange(TypedDict):
    """
    A range in a text document expressed as (zero-based) start and end positions.

    A range is comparable to a selection in an editor. Therefore, the end position is exclusive.

    If you want to specify a range that contains a line including the line ending character(s)
    then use an end position denoting the start of the next line

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#range
    """

    start: LSPPosition
    end: LSPPosition


class LSPLocation(TypedDict):
    """
    Represents a location inside a resource, such as a line inside a text file.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#location
    """

    uri: str
    range: LSPRange


class LSPCodeDescription(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#codeDescription
    """

    # An URI to open with more information about the diagnostic error.
    href: str


class LSPDiagnostic(TypedDict):
    """
    Represents a diagnostic, such as a compiler error or warning. Diagnostic objects are only valid in the scope of a resource.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#diagnostic
    """

    # The range at which the message applies.
    range: LSPRange

    #  The diagnostic's severity. To avoid interpretation mismatches when a
    # server is used with different clients it is highly recommended that
    # servers always provide a severity value. If omitted, it’s recommended
    # for the client to interpret it as an Error severity.
    severity: Optional[Literal[1, 2, 3, 4]]

    # The diagnostic's code, which might appear in the user interface.
    code: Optional[Union[int, str]]

    #  An optional property to describe the error code.
    codeDescription: Optional[LSPCodeDescription]

    # A human-readable string describing the source of this
    # diagnostic, e.g. 'typescript' or 'super lint'.
    source: Optional[str]

    # The diagnostic's message.
    message: str


class LSPTextDocumentContentChangeEventFull(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentContentChangeEvent
    """

    text: str


class LSPTextDocumentContentChangeEventIncremental(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentContentChangeEvent
    """

    range: LSPRange
    rangeLength: Optional[int]
    text: str


LSPTextDocumentContentChangeEvent = Union[
    LSPTextDocumentContentChangeEventFull,
    LSPTextDocumentContentChangeEventIncremental,
]


class LSPDidChangeTextDocumentParams(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#didChangeTextDocumentParams
    """

    textDocument: LSPVersionedTextDocumentIdentifier
    contentChanges: List[LSPTextDocumentContentChangeEvent]


class LSPTextDocumentPositionParams(TypedDict):
    """
    A parameter literal used in requests to pass a text document and a position inside that document.

    It is up to the client to decide how a selection is converted into a position when issuing a request for a text document.
    The client can for example honor or ignore the selection direction to make LSP request consistent with features implemented internally.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentPositionParams
    """

    textDocument: LSPTextDocumentIdentifier
    position: LSPPosition


class LSPDidOpenTextDocumentParams(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#didOpenTextDocumentParams
    """

    textDocument: LSPTextDocumentItem


class LSPDidCloseTextDocumentParams(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#didCloseTextDocumentParams
    """

    textDocument: LSPTextDocumentIdentifier


class LSPDocumentFormattingParams(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#documentFormattingParams
    """

    textDocument: LSPTextDocumentIdentifier
    options: LSPFormattingOptions


class LSPWorkspaceSymbolParams(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#workspaceSymbolParams
    """

    # A query string to filter symbols by. Clients may send an empty string here to request all symbols.
    query: str


class LSPPublishDiagnosticsParams(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#publishDiagnosticsParams
    """

    # The URI for which diagnostic information is reported.
    uri: str

    # Optional the version number of the document the diagnostics are published for.
    version: Optional[int]

    # An array of diagnostic information items.
    diagnostics: List[LSPDiagnostic]


class LSPCompletionItem(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#completionItem
    """

    # The label of this completion item.
    # The label property is also by default the text that is inserted when selecting this completion.
    #
    # If label details are provided the label itself should be an unqualified name of the completion item.
    label: str

    # The kind of this completion item.
    # Based of the kind an icon is chosen by the editor.
    kind: Optional[int]

    # A human-readable string with additional information about this item, like type or symbol information.
    detail: Optional[str]

    # A human-readable string that represents a doc-comment.
    documentation: Optional[Union[str, LSPMarkupContent]]

    # A string that should be inserted into a document when selecting this completion.
    # When omitted the label is used as the insert text for this item.
    insertText: Optional[str]


class LSPCompletionList(TypedDict):
    """
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#completionList
    """

    # This list is not complete. Further typing should result in recomputing this list.
    # Recomputed lists have all their items replaced (not appended) in the incomplete completion sessions.
    isIncomplete: bool

    # The completion items.
    items: List[LSPCompletionItem]


# --------------------------------------------------------------------------------


def request(
    method: str,
    params: Optional[Any] = None,
) -> LSPRequestMessage:
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }


def notification(
    method: str,
    params: Optional[Any] = None,
) -> LSPNotificationMessage:
    return {
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
    }


def textDocumentSyncOptions(
    textDocumentSync: Optional[Union[dict, int]],
) -> Dict[str, Any]:
    if textDocumentSync is None:
        return {
            "openClose": False,
            "change": 0,
        }

    elif isinstance(textDocumentSync, int):
        return {
            "openClose": False if textDocumentSync == 0 else True,
            "change": textDocumentSync,
        }

    else:
        return textDocumentSync


# Type alias for notification handlers
LSPNotificationHandler = Callable[[LSPNotificationMessage], None]


class LanguageServerClient:
    def __init__(
        self,
        logger: logging.Logger,
        name: str,
        server_args: List[str],
        on_logTrace: Optional[LSPNotificationHandler] = None,
        on_window_logMessage: Optional[LSPNotificationHandler] = None,
        on_window_showMessage: Optional[LSPNotificationHandler] = None,
        on_textDocument_publishDiagnostics: Optional[LSPNotificationHandler] = None,
    ):
        self._init_lock = threading.Lock()
        self._logger = logger
        self._name = name
        self._server_args = server_args
        self._server_process: Optional[subprocess.Popen] = None
        self._server_shutdown = threading.Event()
        self._server_initializing = None
        self._server_initialized = False
        self._server_info: Optional[LSPServerInfo] = None
        self._server_capabilities: Optional[dict] = None
        self._send_queue = Queue(maxsize=1)
        self._receive_queue = Queue(maxsize=1)
        self._reader: Optional[threading.Thread] = None
        self._writer: Optional[threading.Thread] = None
        self._handler: Optional[threading.Thread] = None
        self._request_callback: Dict[
            Union[int, str], Callable[[LSPResponseMessage], None]
        ] = {}
        self._open_documents = set()
        self._on_logTrace = on_logTrace
        self._on_window_logMessage = on_window_logMessage
        self._on_window_showMessage = on_window_showMessage
        self._on_textDocument_publishDiagnostics = on_textDocument_publishDiagnostics

    def is_server_initializing(self) -> Optional[bool]:
        """
        Returns True if server is initializing.
        """
        return self._server_initializing

    def is_server_initialized(self) -> bool:
        """
        Returns True if server is up and running and successfuly processed a 'initialize' request.
        """
        return self._server_initialized

    def is_server_shutdown(self) -> bool:
        """
        Returns True if server processed a 'shutdown' request and this client sent a 'exit' notification.
        """
        return self._server_shutdown.is_set()

    def support_method(self, method: str) -> Optional[bool]:
        if not self._server_capabilities:
            return None

        if method == "textDocument/formatting":
            return bool(self._server_capabilities.get("documentFormattingProvider"))
        elif method == "textDocument/rangeFormatting":
            return bool(
                self._server_capabilities.get("documentRangeFormattingProvider")
            )
        elif method == "textDocument/documentSymbol":
            return bool(self._server_capabilities.get("documentSymbolProvider"))
        elif method == "textDocument/documentHighlight":
            return bool(self._server_capabilities.get("documentHighlightProvider"))
        elif method == "textDocument/references":
            return bool(self._server_capabilities.get("referencesProvider"))
        elif method == "textDocument/definition":
            return bool(self._server_capabilities.get("definitionProvider"))
        elif method == "textDocument/hover":
            return bool(self._server_capabilities.get("hoverProvider"))
        elif method == "textDocument/completion":
            return bool(self._server_capabilities.get("completionProvider"))
        elif method == "textDocument/didOpen" or method == "textDocument/didClose":
            options = textDocumentSyncOptions(
                self._server_capabilities.get("textDocumentSync")
            )
            return options.get("openClose", False)
        elif method == "textDocument/didChange":
            options = textDocumentSyncOptions(
                self._server_capabilities.get("textDocumentSync")
            )
            return options["change"] != 0
        elif method == "workspace/symbol":
            return bool(self._server_capabilities.get("workspaceSymbolProvider"))
        else:
            return False

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
        self._logger.debug(f"[{self._name}] Reader started 🟢")

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

        self._logger.debug(f"[{self._name}] Reader stopped 🔴")

    def _start_writer(self):
        self._logger.debug(f"[{self._name}] Writer started 🟢")

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
                        f"{self._name} - Can't write to server's stdin: {e}"
                    )

            finally:
                self._send_queue.task_done()

        # 'None Task' is complete.
        self._send_queue.task_done()

        self._logger.debug(f"[{self._name}] Writer stopped 🔴")

    def _start_handler(self):
        self._logger.debug(f"[{self._name}] Handler started 🟢")

        while (message := self._receive_queue.get()) is not None:
            message = cast(Union[LSPNotificationMessage, LSPResponseMessage], message)

            # A Response Message sent as a result of a request.
            #
            # If a request doesn’t provide a result value the receiver of a request
            # still needs to return a response message to conform to the JSON-RPC specification.
            # The result property of the ResponseMessage should be set to null in this case to signal a successful request.
            #
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#responseMessage
            if request_id := message.get("id"):
                if callback := self._request_callback.get(request_id):
                    try:
                        callback(cast(LSPResponseMessage, message))
                    except Exception:
                        self._logger.exception(f"{self._name} - Request callback error")
                    finally:
                        del self._request_callback[request_id]
            else:
                notification = cast(LSPNotificationMessage, message)

                method = notification["method"]

                try:
                    if method == "$/logTrace":
                        if f := self._on_logTrace:
                            f(notification)

                    elif method == "window/logMessage":
                        if f := self._on_window_logMessage:
                            f(notification)

                    elif method == "window/showMessage":
                        if f := self._on_window_showMessage:
                            f(notification)

                    elif method == "textDocument/publishDiagnostics":
                        if f := self._on_textDocument_publishDiagnostics:
                            f(notification)

                except Exception:
                    self._logger.exception(f"{self._name} - Error handling '{method}'")

            self._receive_queue.task_done()

        # 'None Task' is complete.
        self._receive_queue.task_done()

        self._logger.debug(f"[{self._name}] Handler stopped 🔴")

    def _put(
        self,
        message: Union[LSPNotificationMessage, LSPRequestMessage],
        callback: Optional[Callable[[LSPResponseMessage], None]] = None,
    ):
        # Drop message if server is not ready - unless it's an initization message.
        if not self._server_initialized and not message["method"] == "initialize":
            self._logger.debug(
                f"Server {self._name} is not initialized; Will drop {message['method']}"
            )

            return

        # Drop message if server was shutdown.
        if self._server_shutdown.is_set():
            self._logger.warn(
                f"Server {self._name} was shutdown; Will drop {message['method']}"
            )

            return

        self._send_queue.put(message)

        if message_id := message.get("id"):
            # A mapping of request ID to callback.
            #
            # callback will be called once the response for the request is received.
            #
            # callback might not be called if there's an error reading the response,
            # or the server never returns a response.
            if callback:
                self._request_callback[message_id] = callback

    def initialize(
        self,
        params,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The initialize request is sent as the first request from the client to the server.
        Until the server has responded to the initialize request with an InitializeResult,
        the client must not send any additional requests or notifications to the server.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initialize
        """

        with self._init_lock:
            if self._server_initializing or self._server_initialized:
                return

            self._server_initializing = True

            self._logger.debug(f"Initialize {self._name} {self._server_args}")

            self._server_process = subprocess.Popen(
                self._server_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self._logger.info(
                f"{self._name} is up and running; PID {self._server_process.pid}"
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

            def _callback(response: LSPResponseMessage):
                self._server_initializing = False

                # The server should not be considered 'initialized' if there's an error.
                if not response.get("error"):
                    self._server_initialized = True

                    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initializeResult
                    result = response.get("result")

                    if result is not None:
                        self._server_capabilities = result.get("capabilities")
                        self._server_info = result.get("serverInfo")

                    # The initialized notification is sent from the client to the server
                    # after the client received the result of the initialize request
                    # but before the client is sending any other request or notification to the server.
                    #
                    # The server can use the initialized notification, for example, to dynamically register capabilities.
                    # The initialized notification may only be sent once.
                    #
                    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initialized
                    self._put(notification("initialized", {}))

                callback(response)

            self._put(request("initialize", params), _callback)

    def shutdown(self):
        """
        The shutdown request is sent from the client to the server.
        It asks the server to shut down,
        but to not exit (otherwise the response might not be delivered correctly to the client).
        There is a separate exit notification that asks the server to exit.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown
        """

        self._logger.info(f"Shutdown {self._name}")

        def _callback(message):
            self.exit()

        self._put(request("shutdown"), _callback)

    def exit(self):
        """
        A notification to ask the server to exit its process.
        The server should exit with success code 0 if the shutdown request has been received before;
        otherwise with error code 1.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#exit
        """
        self._logger.info(f"Exit {self._name}")

        self._put(notification("exit"))

        self._server_shutdown.set()

        # Enqueue `None` to signal that workers must stop:
        self._send_queue.put(None)
        self._receive_queue.put(None)

        returncode = None

        try:
            self._logger.info(f"Waiting for server {self._name} to terminate...")

            returncode = self._server_process.wait(30)
        except subprocess.TimeoutExpired:
            self._logger.info(
                f"Terminate timeout expired; Will explicitly kill server {self._name}"
            )

            # Explicitly kill the process if it did not terminate.
            self._server_process.kill()

            returncode = self._server_process.wait()

        self._logger.info(
            f"{self._name} server terminated with returncode {returncode}"
        )

    def textDocument_didOpen(
        self,
        params: LSPDidOpenTextDocumentParams,
    ):
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

        self._put(notification("textDocument/didOpen", params))

        self._open_documents.add(textDocument_uri)

    def textDocument_didClose(
        self,
        params: LSPDidCloseTextDocumentParams,
    ):
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

        self._put(notification("textDocument/didClose", params))

        self._open_documents.remove(textDocument_uri)

    def textDocument_didChange(
        self,
        params: LSPDidChangeTextDocumentParams,
    ):
        """
        The document change notification is sent from the client to the server to signal changes to a text document.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didChange
        """

        # Before a client can change a text document it must claim
        # ownership of its content using the textDocument/didOpen notification.
        if params["textDocument"]["uri"] not in self._open_documents:
            return

        self._put(notification("textDocument/didChange", params))

    def textDocument_hover(
        self,
        params: LSPTextDocumentPositionParams,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The hover request is sent from the client to the server to request
        hover information at a given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_hover
        """

        self._put(request("textDocument/hover", params), callback)

    def textDocument_definition(
        self,
        params: LSPTextDocumentPositionParams,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The go to definition request is sent from the client to the server
        to resolve the definition location of a symbol at a given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_definition
        """

        self._put(request("textDocument/definition", params), callback)

    def textDocument_references(
        self,
        params,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The references request is sent from the client to the server
        to resolve project-wide references for the symbol denoted by the given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_references
        """

        self._put(request("textDocument/references", params), callback)

    def textDocument_documentHighlight(
        self,
        params: LSPTextDocumentPositionParams,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The document highlight request is sent from the client to
        the server to resolve document highlights for a given text document position.

        For programming languages this usually highlights all references to the symbol scoped to this file.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentHighlight
        """

        self._put(request("textDocument/documentHighlight", params), callback)

    def textDocument_documentSymbol(
        self,
        params,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The document symbol request is sent from the client to the server.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentSymbol
        """

        self._put(request("textDocument/documentSymbol", params), callback)

    def textDocument_formatting(
        self,
        params: LSPDocumentFormattingParams,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The document formatting request is sent from the client to the server to format a whole document.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_formatting
        """
        self._put(request("textDocument/formatting", params), callback)

    def textDocument_rangeFormatting(
        self,
        params,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The document range formatting request is sent from the client to the server to format a specific range in a document.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#documentRangeFormattingParams
        """
        self._put(request("textDocument/rangeFormatting", params), callback)

    def textDocument_completion(
        self,
        params: LSPTextDocumentPositionParams,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The completion request is sent from the client to the server to compute completion items at a given cursor position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_completion
        """
        self._put(request("textDocument/completion", params), callback)

    def workspace_symbol(
        self,
        params: LSPWorkspaceSymbolParams,
        callback: Callable[[LSPResponseMessage], None],
    ):
        """
        The workspace symbol request is sent from the client to the server to list project-wide symbols matching the query string.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#workspace_symbol
        """

        self._put(request("workspace/symbol", params), callback)
