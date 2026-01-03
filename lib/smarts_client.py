import json
import logging
import shlex
import subprocess
import threading
import uuid
from enum import Enum, auto
from queue import Queue
from typing import Any, Callable, Dict, List, Literal, Optional, TypedDict, Union, cast

LSPPositionEncoding = Literal[
    # Character offsets count UTF-8 code units (e.g bytes).
    "utf-8",
    #  Character offsets count UTF-16 code units.
    # This is the default and must always be supported by servers.
    "utf-16",
    # Character offsets count UTF-32 code units.
    # Implementation note: these are the same as Unicode code points,
    # so this `PositionEncodingKind` may also be used for an
    #  encoding-agnostic representation of character offsets.
    "utf-32",
]


class LSPServerCapabilities(TypedDict, total=False):
    positionEncoding: Optional[LSPPositionEncoding]


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


class LSPResponseMessage(TypedDict, total=False):
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
    error: LSPResponseError


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


class _LSPMarkedString(TypedDict):
    language: str
    value: str


# The pair of a language and a value is an equivalent to markdown:
# ```${language}
# ${value}
# ```
# @deprecated use MarkupContent instead.
#
# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#markedString
LSPMarkedString = Union[str, _LSPMarkedString]


class LSPMarkupContent(TypedDict):
    """
    A `MarkupContent` literal represents a string value which content is
    interpreted base on its kind flag. Currently the protocol supports
    `plaintext` and `markdown` as markup kinds.

    Please Note* that clients might sanitize the return markdown.
    A client could decide to remove HTML from the markdown to avoid script execution.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#markupContentInnerDefinition
    """

    # The type of the Markup
    kind: Literal["plaintext", "markdown"]

    # The content itself
    value: str


class LSPHover(TypedDict, total=False):
    """
    The result of a hover request.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#hover
    """

    contents: Union[
        LSPMarkedString,
        List[LSPMarkedString],
        LSPMarkupContent,
    ]

    # An optional range is a range inside a text document
    # that is used to visualize a hover, e.g. by changing the background color.
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


# A symbol kind.
# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#symbolKind
LSPSymbolKind = Literal[
    1,  # File
    2,  # Module
    3,  # Namespace
    4,  # Package
    5,  # Class
    6,  # Method
    7,  # Property
    8,  # Field
    9,  # Constructor
    10,  # Enum
    11,  # Interface
    12,  # Function
    13,  # Variable
    14,  # Constant
    15,  # String
    16,  # Number
    17,  # Boolean
    18,  # Array
    19,  # Object
    20,  # Key
    21,  # Null
    22,  # EnumMember
    23,  # Struct
    24,  # Event
    25,  # Operator
    26,  # TypeParameter
]


# Symbol tags are extra annotations that tweak the rendering of a symbol.
# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#symbolTag
LSPSymbolTag = Literal[
    1,  # Deprecated - Render a symbol as obsolete, usually using a strike-out.
]


class LSPDocumentSymbol(TypedDict, total=False):
    """
    Represents programming constructs like variables, classes, interfaces etc.
    that appear in a document. Document symbols can be hierarchical and they
    have two ranges: one that encloses its definition and one that points to
    its most interesting range, e.g. the range of an identifier.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#documentSymbol
    """

    # The name of this symbol. Will be displayed in the user interface and
    # therefore must not be an empty string or a string only consisting of white spaces.
    name: str

    # More detail for this symbol, e.g the signature of a function.
    detail: str

    # The kind of this symbol.
    kind: LSPSymbolKind

    # Tags for this document symbol.
    # @since 3.16.0
    tags: List[LSPSymbolTag]

    # Indicates if this symbol is deprecated.
    # @deprecated Use tags instead
    deprecated: bool

    # The range enclosing this symbol not including leading/trailing whitespace
    # but everything else like comments. This information is typically used to
    # determine if the clients cursor is inside the symbol to reveal in the symbol in the UI.
    range: LSPRange

    # The range that should be selected and revealed when this symbol is being picked,
    # e.g the name of a function. Must be contained by the `range`.
    selectionRange: LSPRange

    # Children of this symbol, e.g. properties of a class.
    children: List["LSPDocumentSymbol"]


class LSPSymbolInformation(TypedDict, total=False):
    """
    Represents information about programming constructs like variables, classes,
    interfaces etc.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#symbolInformation
    """

    # The name of this symbol.
    name: str

    # The kind of this symbol.
    kind: LSPSymbolKind

    # Tags for this symbol.
    # @since 3.16.0
    tags: List[LSPSymbolTag]

    # The name of the symbol containing this symbol. This information is for
    # user interface purposes (e.g. to render a qualifier in the user interface
    # if necessary). It can't be used to re-infer a hierarchy for the document symbols.
    containerName: str

    # Indicates if this symbol is deprecated.
    # @deprecated Use tags instead
    deprecated: bool

    # The location of this symbol. The location's range is used by a tool
    # to reveal the location in the editor. If the symbol is selected in the
    # tool the range's start information is used to position the cursor.
    location: LSPLocation


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


class LSPParameterInformation(TypedDict, total=False):
    """
    Represents a parameter of a callable signature.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#parameterInformation
    """

    # The label of this parameter information.
    # Either a string or an inclusive start and exclusive end offsets within its containing signature label.
    label: Union[str, List[int]]

    # The human-readable doc-comment of this parameter.
    documentation: Union[str, LSPMarkupContent]


class LSPSignatureInformation(TypedDict, total=False):
    """
    Represents the signature of a callable.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#signatureInformation
    """

    # The label of this signature. Will be shown in the UI.
    label: str

    # The human-readable doc-comment of this signature.
    documentation: Union[str, LSPMarkupContent]

    # The parameters of this signature.
    parameters: List[LSPParameterInformation]

    # The index of the active parameter.
    activeParameter: Optional[int]


class LSPSignatureHelpContext(TypedDict, total=False):
    """
    Additional information about the context in which a signature help request was triggered.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#signatureHelpContext
    """

    # Action that caused signature help to be triggered.
    # 1 = Invoked, 2 = TriggerCharacter, 3 = ContentChange
    triggerKind: Literal[1, 2, 3]

    # Character that caused signature help to be triggered.
    triggerCharacter: Optional[str]

    # true if signature help was already showing when it was triggered.
    isRetrigger: bool

    # The currently active SignatureHelp.
    activeSignatureHelp: Optional["LSPSignatureHelp"]


class LSPSignatureHelpParams(LSPTextDocumentPositionParams, total=False):
    """
    Parameters for a signature help request.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#signatureHelpParams
    """

    # The signature help context.
    context: LSPSignatureHelpContext


class LSPSignatureHelp(TypedDict, total=False):
    """
    Signature help represents the signature of something callable.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#signatureHelp
    """

    # One or more signatures.
    signatures: List[LSPSignatureInformation]

    # The active signature.
    activeSignature: Optional[int]

    # The active parameter of the active signature.
    activeParameter: Optional[int]


class LSPTextEdit(TypedDict):
    """
    A textual edit applicable to a text document.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textEdit
    """

    # The range of the text document to be manipulated. To insert text into a document
    # create a range where start === end.
    range: LSPRange

    # The string to be inserted. For delete operations use an empty string.
    newText: str


class LSPTextDocumentEdit(TypedDict):
    """
    Describes textual changes on a single text document. The text document is referred to as a
    VersionedTextDocumentIdentifier to allow clients to check the text document version before an
    edit is applied.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentEdit
    """

    # The text document to change.
    textDocument: LSPVersionedTextDocumentIdentifier

    # The edits to be applied.
    edits: List[LSPTextEdit]


class LSPWorkspaceEdit(TypedDict, total=False):
    """
    A workspace edit represents changes to many resources managed in the workspace.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#workspaceEdit
    """

    # Holds changes to existing resources.
    changes: Dict[str, List[LSPTextEdit]]

    # Depending on the client capability
    # `workspace.workspaceEdit.resourceOperations` document changes are either
    # an array of `TextDocumentEdit`s to express changes to n different text documents
    # where each text document edit addresses a specific version of a text document.
    documentChanges: List[LSPTextDocumentEdit]


class _LSPDocumentHighlightOptional(TypedDict, total=False):
    # The highlight kind, default is DocumentHighlightKind.Text.
    # 1: A textual occurrence.
    # 2: Read-access of a symbol, like reading a variable.
    # 3: Write-access of a symbol, like writing to a variable.
    kind: Literal[1, 2, 3]


class LSPDocumentHighlight(_LSPDocumentHighlightOptional):
    """
    A document highlight is a range inside a text document which deserves
    special attention. Usually a document highlight is visualized by changing
    the background color of its range.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#documentHighlight
    """

    # The range this highlight applies to.
    range: LSPRange


class LSPRenameParams(LSPTextDocumentPositionParams):
    """
    Parameters for a rename request.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#renameParams
    """

    # The new name of the symbol. If the given name is not valid the
    # request must return a ResponseError with an appropriate message set.
    newName: str


# Type alias for notification handlers
LSPNotificationHandler = Callable[[LSPNotificationMessage], None]

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_hover
LSPHoverResult = Union[
    LSPHover,
    None,
]

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_definition
LSPDefinitionResult = Union[
    LSPLocation,
    List[LSPLocation],
    None,
]

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_references
LSPReferencesResult = Union[
    List[LSPLocation],
    None,
]

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentSymbol
LSPDocumentSymbolResult = Union[
    List[LSPDocumentSymbol],
    List[LSPSymbolInformation],
    None,
]

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentHighlight
LSPDocumentHighlightResult = Union[
    List[LSPDocumentHighlight],
    None,
]

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_formatting
LSPFormattingResult = Union[
    List[LSPTextEdit],
    None,
]


LSPCompletionResultCallback = Callable[
    [Optional[Union[List[LSPCompletionItem], Dict[str, Any]]]], None
]

LSPSignatureHelpResultCallback = Callable[[Optional[LSPSignatureHelp]], None]

LSPWorkspaceSymbolResultCallback = Callable[[Optional[List[Dict[str, Any]]]], None]

LSPRenameResultCallback = Callable[[Optional[LSPWorkspaceEdit]], None]


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


class LanguageServerStatus(Enum):
    """Represents the lifecycle state of the language server.

    State transitions:
    NOT_STARTED -> INITIALIZING -> INITIALIZED -> SHUTDOWN
                                -> FAILED

    FAILED state can be reached from INITIALIZING (init error),
    INITIALIZED (server crash), or during I/O operations.
    """

    NOT_STARTED = auto()  # Server process hasn't been created yet
    INITIALIZING = auto()  # Initialize request sent, waiting for response
    INITIALIZED = auto()  # Successfully initialized and ready for requests
    FAILED = auto()  # Server crashed, I/O error, or initialization failed
    SHUTDOWN = auto()  # Server has been shutdown gracefully


class LanguageServerClient:
    """LSP client that manages communication with a language server subprocess.

    Thread Safety:
        - Uses a single RLock (_lock) to protect all shared state
        - Safe to call methods from multiple threads

    Resilience Features:
        - Detects server crashes via process monitoring thread
        - Handles initialization failures explicitly (transitions to FAILED state)
        - Recovers from I/O errors in reader/writer threads
        - Clears pending callbacks on failure to prevent memory leaks

    Known Limitations:
        1. Reader can hang on incomplete messages:
           If the server sends partial headers or crashes mid-message, readline()
           blocks indefinitely. The outer loop only checks is_server_shutdown()
           between messages, not during a read. Python's limited support for
           non-blocking pipe I/O makes this difficult to solve without select/poll.

        2. No automatic reconnection:
           Once the client enters FAILED state, it stays failed. Callers must detect
           the failure and create a new LanguageServerClient instance if they want
           to retry.

        3. Callback timeouts not enforced:
           If a server never responds to a request, the callback is never invoked.
           Callers should implement their own timeout logic if needed.

        4. Queue size limits:
           Send/receive queues have maxsize=100. If the server is slow to process
           messages, queue.put() will block. This is intentional backpressure but
           could cause deadlocks if not handled carefully.
    """

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
        self._lock = threading.RLock()
        self._logger = logger
        self._name = name
        self._server_status = LanguageServerStatus.NOT_STARTED
        self._server_args = server_args
        self._server_process: Optional[subprocess.Popen] = None
        self._server_info: Optional[LSPServerInfo] = None
        self._server_capabilities: Optional[LSPServerCapabilities] = None
        self._send_queue = Queue(maxsize=100)
        self._receive_queue = Queue(maxsize=100)
        self._reader: Optional[threading.Thread] = None
        self._writer: Optional[threading.Thread] = None
        self._handler: Optional[threading.Thread] = None
        self._monitor: Optional[threading.Thread] = None
        self._request_callback: Dict[
            Union[int, str], Callable[[LSPResponseMessage], None]
        ] = {}
        self._open_documents = set()
        self._on_logTrace = on_logTrace
        self._on_window_logMessage = on_window_logMessage
        self._on_window_showMessage = on_window_showMessage
        self._on_textDocument_publishDiagnostics = on_textDocument_publishDiagnostics

    def server_status(self) -> LanguageServerStatus:
        with self._lock:
            return self._server_status

    def is_server_initializing(self) -> bool:
        """
        Returns True if server is initializing.
        """
        with self._lock:
            return self._server_status == LanguageServerStatus.INITIALIZING

    def is_server_initialized(self) -> bool:
        """
        Returns True if server is up and running and successfully processed an 'initialize' request.
        """
        with self._lock:
            return self._server_status == LanguageServerStatus.INITIALIZED

    def is_server_shutdown(self) -> bool:
        """
        Returns True if server processed a 'shutdown' request and this client sent an 'exit' notification.
        """
        with self._lock:
            return self._server_status == LanguageServerStatus.SHUTDOWN

    def is_server_failed(self) -> bool:
        """
        Returns True if server failed to initialize, crashed, or encountered an I/O error.
        """
        with self._lock:
            return self._server_status == LanguageServerStatus.FAILED

    def position_encoding(self) -> Optional[LSPPositionEncoding]:
        if capabilities := self._server_capabilities:
            return capabilities.get("positionEncoding")

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
        elif method == "textDocument/signatureHelp":
            return bool(self._server_capabilities.get("signatureHelpProvider"))
        elif method == "textDocument/rename":
            return bool(self._server_capabilities.get("renameProvider"))
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

    def _clear_callbacks(self):
        """Clear all pending request callbacks (must be called with lock held).

        When the server fails or crashes, any in-flight requests will never receive
        responses. Without clearing the callback dict:
        1. Memory leak - callbacks stay in memory forever
        2. Confusion - callers might wait indefinitely for responses that will never come

        By explicitly clearing callbacks, we free memory and make the failure mode explicit.
        Callers should handle the case where their callback is never invoked (e.g., with timeouts).
        """
        n = len(self._request_callback)

        if n > 0:
            self._logger.warning(
                f"[{self._name}] Clearing {n} pending request callback(s)"
            )
            self._request_callback.clear()

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

    def _start_monitor(self):
        """Monitor the server process and detect unexpected exits.

        Without process monitoring, if the server crashes unexpectedly (segfault,
        killed by OS, etc.), the client wouldn't know about it until trying to perform I/O,
        which could hang or fail in confusing ways.

        This thread blocks on process.wait() and immediately detects when the server exits,
        allowing us to transition to FAILED state and clean up resources promptly.

        The monitor distinguishes between:
        - Expected shutdown (status is already SHUTDOWN): No action needed
        - Unexpected crash (status is INITIALIZING/INITIALIZED): Transition to FAILED
        """
        self._logger.debug(f"[{self._name}] Monitor started 🟢")

        try:
            # Block until process exits
            returncode = self._server_process.wait()

            with self._lock:
                # Only mark as failed if we weren't expecting the shutdown
                if self._server_status not in (
                    LanguageServerStatus.SHUTDOWN,
                    LanguageServerStatus.FAILED,
                ):
                    self._logger.error(
                        f"[{self._name}] Server crashed unexpectedly with exit code {returncode}"
                    )
                    self._server_status = LanguageServerStatus.FAILED
                    self._clear_callbacks()

                    # Signal worker threads to stop
                    self._send_queue.put(None)
                    self._receive_queue.put(None)
                else:
                    self._logger.debug(
                        f"[{self._name}] Server exited with code {returncode}"
                    )

        except Exception as e:
            self._logger.error(f"[{self._name}] Monitor thread error: {e}")

        finally:
            self._logger.debug(f"[{self._name}] Monitor stopped 🔴")

    def _start_reader(self):
        self._logger.debug(f"[{self._name}] Reader started 🟢")

        # The reader performs I/O operations (readline, read) that can raise exceptions
        # if the server subprocess crashes, closes stdout unexpectedly, or the pipe breaks.
        # Without this try-except, the reader thread would crash silently, leaving the client
        # in an inconsistent state where it thinks the server is running but can't receive messages.
        #
        # By catching exceptions and transitioning to FAILED state, we make crashes explicit
        # and allow the client to handle them gracefully (e.g., show error to user, attempt restart).
        try:
            # Only run while server is in an active state (INITIALIZING or INITIALIZED).
            while self.server_status() in (
                LanguageServerStatus.INITIALIZING,
                LanguageServerStatus.INITIALIZED,
            ):
                out = self._server_process.stdout

                # The base protocol consists of a header and a content part (comparable to HTTP).
                # The header and content part are separated by a '\r\n'.
                #
                # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#baseProtocol

                # -- HEADER

                headers = {}

                while True:
                    line = out.readline().decode("ascii").strip()

                    # Empty line can mean either end of headers OR EOF (stdout closed).
                    # If it's EOF and there are no headers yet, the server has died.
                    # We need to detect this and exit the loop, otherwise we'll spin forever
                    # reading EOF repeatedly.
                    if line == "":
                        break

                    k, v = line.split(": ", 1)

                    headers[k] = v

                # If we got no headers at all, stdout is closed (EOF).
                # Break the outer loop so the reader can exit cleanly.
                if not headers:
                    self._logger.debug(
                        f"[{self._name}] Reader detected EOF (stdout closed)"
                    )
                    break

                # -- CONTENT

                if content_length := headers.get("Content-Length"):
                    content = (
                        self._read(out, int(content_length)).decode("utf-8").strip()
                    )

                    try:
                        message = json.loads(content)

                        # Enqueue message; Blocks if queue is full.
                        self._receive_queue.put(message)

                    except json.JSONDecodeError:
                        # The effect of not being able to decode a message,
                        # is that an 'in-flight' request won't have its callback called.
                        self._logger.error(f"Failed to decode message: {content}")

        except Exception as e:
            # Server crashed or I/O error occurred
            self._logger.error(f"[{self._name}] Reader thread crashed: {e}")

            with self._lock:
                # Only transition to FAILED if we're not already shutting down
                # (normal shutdown can cause I/O errors as pipes close)
                if self._server_status not in (
                    LanguageServerStatus.SHUTDOWN,
                    LanguageServerStatus.FAILED,
                ):
                    self._server_status = LanguageServerStatus.FAILED
                    self._clear_callbacks()

            # Signal other threads to stop
            self._send_queue.put(None)
            self._receive_queue.put(None)

        finally:
            self._logger.debug(f"[{self._name}] Reader stopped 🔴")

    def _start_writer(self):
        self._logger.debug(f"[{self._name}] Writer started 🟢")

        while (message := self._send_queue.get()) is not None:
            task_done_called = False

            try:
                content = json.dumps(message)

                header = f"Content-Length: {len(content)}\r\n\r\n"

                encoded = header.encode("ascii") + content.encode("utf-8")
                self._server_process.stdin.write(encoded)
                self._server_process.stdin.flush()

            except BrokenPipeError as e:
                # BrokenPipeError means the server's stdin pipe is closed, which happens
                # when the server crashes or exits unexpectedly. Continuing to loop would just
                # keep trying to write to a broken pipe, logging errors repeatedly.
                #
                # By transitioning to FAILED and breaking, we stop the writer cleanly and
                # signal to other parts of the client that the server is no longer functional.
                self._logger.error(
                    f"[{self._name}] Can't write to server's stdin (broken pipe): {e}"
                )

                with self._lock:
                    # Only transition to FAILED if we're not already shutting down
                    if self._server_status not in (
                        LanguageServerStatus.SHUTDOWN,
                        LanguageServerStatus.FAILED,
                    ):
                        self._server_status = LanguageServerStatus.FAILED
                        self._clear_callbacks()

                # Signal other threads to stop
                self._receive_queue.put(None)

                # Mark current task as done before breaking
                self._send_queue.task_done()
                task_done_called = True
                break

            finally:
                # Only call task_done if we didn't already call it in the except block
                if not task_done_called:
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
                with self._lock:
                    callback = self._request_callback.pop(request_id, None)

                if callback:
                    try:
                        callback(cast(LSPResponseMessage, message))
                    except Exception:
                        self._logger.exception(f"{self._name} - Request callback error")

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
        with self._lock:
            method = message["method"]

            # WHY: Allow certain lifecycle methods through regardless of state:
            # - initialize: needed to start the server
            # - shutdown/exit: needed to clean up FAILED or stuck servers
            #
            # This prevents situations where a FAILED server can't be cleaned up
            # because shutdown messages are dropped.
            lifecycle_methods = {"initialize", "shutdown", "exit"}

            # Drop message if server is not ready - unless it's a lifecycle message.
            if (
                self._server_status != LanguageServerStatus.INITIALIZED
                and method not in lifecycle_methods
            ):
                self._logger.debug(
                    f"Server {self._name} is not initialized; Will drop {method}"
                )
                return

            # WHY: Drop messages to SHUTDOWN servers to prevent queue buildup.
            # Once shutdown, the server won't process any more requests.
            # Exception: still allow "exit" through in case shutdown didn't complete properly.
            if (
                self._server_status == LanguageServerStatus.SHUTDOWN
                and method != "exit"
            ):
                self._logger.warn(
                    f"Server {self._name} was shutdown; Will drop {method}"
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

    def _make_callback(
        self,
        on_result: Callable[[Optional[Any]], None],
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ) -> Callable[[LSPResponseMessage], None]:
        """
        Wraps a typed result callback to handle LSPResponseMessage extraction.

        This wrapper:
        1. Extracts the result from the LSPResponseMessage
        2. Handles errors (either via on_error callback or logging)
        3. Calls the user's callback with the typed result or None

        Args:
            on_result: User callback that receives the typed result or None
            on_error: Optional callback for handling errors. If not provided, errors are logged.

        Returns:
            A callback that accepts LSPResponseMessage
        """

        def callback(response: LSPResponseMessage) -> None:
            if error := response.get("error"):
                self._logger.error(
                    f"[{self._name}] Error: code={error.get('code')}, message={error.get('message')}, data={error.get('data')}"
                )

                if on_error:
                    on_error(error)

            else:
                on_result(response.get("result"))

        return callback

    def initialize(
        self,
        params,
        callback: Callable[[LSPResponseMessage], None],
        timeout: float = 30.0,
    ):
        """
        The initialize request is sent as the first request from the client to the server.
        Until the server has responded to the initialize request with an InitializeResult,
        the client must not send any additional requests or notifications to the server.

        timeout: Maximum time to wait for initialization response. If exceeded, transition
        to FAILED state. Without a timeout, a hung server would leave the client stuck forever.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initialize
        """

        current_status = self.server_status()

        # Allow reinitializing FAILED servers for retry logic.
        # If a server failed due to temporary issue (network, resource), caller should be
        # able to try again without creating a new client instance.
        if current_status not in (
            LanguageServerStatus.NOT_STARTED,
            LanguageServerStatus.FAILED,
        ):
            self._logger.warning(
                f"[{self._name}] Cannot initialize - already in state {current_status.name}"
            )
            return

        self._logger.debug(f"Initialize {self._name} `{shlex.join(self._server_args)}`")

        try:
            server_process = subprocess.Popen(
                self._server_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            # If we can't start the subprocess, we need to handle it gracefully.
            # Common failures: command not found, permission denied, no memory.
            # Without this try-except, we'd crash and leave client in inconsistent state.
            self._logger.error(f"[{self._name}] Failed to start server process: {e}")

            with self._lock:
                self._server_status = LanguageServerStatus.FAILED

            # Still call user callback with synthetic error response
            error_response: LSPResponseMessage = {
                "id": None,
                "result": None,
                "error": {
                    "code": -1,
                    "message": f"Failed to start server process: {e}",
                    "data": None,
                },
            }
            callback(error_response)

            return

        with self._lock:
            self._server_status = LanguageServerStatus.INITIALIZING
            self._server_process = server_process

        self._logger.debug(f"Initializing {self._name} ({self._server_process.pid}) ⏳")

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

        # Thread responsible for monitoring the server process.
        self._monitor = threading.Thread(
            name="Monitor",
            target=self._start_monitor,
            daemon=True,
        )
        self._monitor.start()

        # Use threading.Timer for timeout instead of blocking on Event.wait().
        # Blocking would freeze the calling thread (often Sublime's main thread),
        # making the UI unresponsive. With a timer, initialize() returns immediately (async),
        # and the timeout check happens in a background thread.

        def _timeout_handler():
            with self._lock:
                # Only trigger timeout if still INITIALIZING
                # If status changed (INITIALIZED or FAILED), the response already arrived
                if self._server_status != LanguageServerStatus.INITIALIZING:
                    return

                self._logger.error(
                    f"[{self._name}] Initialization timed out after {timeout}s"
                )

                self._server_status = LanguageServerStatus.FAILED

                self._clear_callbacks()

            # Call user callback with synthetic timeout error
            timeout_response: LSPResponseMessage = {
                "id": None,
                "result": None,
                "error": {
                    "code": -2,
                    "message": f"Initialization timed out after {timeout}s",
                    "data": None,
                },
            }

            callback(timeout_response)

        # Start timeout timer
        timeout_timer = threading.Timer(timeout, _timeout_handler)
        timeout_timer.daemon = True
        timeout_timer.start()

        def _callback(response: LSPResponseMessage):
            # Cancel timeout timer since we got a response
            timeout_timer.cancel()

            with self._lock:
                # Only process if still INITIALIZING
                # If status changed (FAILED by timeout), don't process the response
                if self._server_status != LanguageServerStatus.INITIALIZING:
                    return

                # Without handling errors, the client stays in INITIALIZING state forever
                # if the server rejects initialization. This leads to confusing behavior where
                # the client appears stuck and won't accept new requests.
                #
                # By transitioning to FAILED state, we make the error explicit and allow
                # higher-level code to detect the failure and potentially retry or alert the user.
                if error := response.get("error"):
                    self._server_status = LanguageServerStatus.FAILED

                    self._clear_callbacks()

                    self._logger.error(
                        f"[{self._name}] Initialization failed: "
                        f"code={error.get('code')}, message={error.get('message')} 🔴"
                    )
                else:
                    self._server_status = LanguageServerStatus.INITIALIZED

                    self._logger.info(
                        f"{self._name} ({self._server_process.pid}) initialized 🚀"
                    )

                    if result := cast(LSPInitializeResult, response.get("result")):
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

    def shutdown(self, timeout: float = 5.0):
        """
        The shutdown request is sent from the client to the server.
        It asks the server to shut down,
        but to not exit (otherwise the response might not be delivered correctly to the client).
        There is a separate exit notification that asks the server to exit.

        Clients must not send any notifications other than exit
        or requests to a server to which they have sent a shutdown request.

        Clients should also wait with sending the exit notification until they have received a response from the shutdown request.

        timeout: If the server is hung or misbehaving, we need a way to force
        shutdown after a reasonable time. Without a timeout, shutdown could hang forever.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown
        """

        self._logger.info(f"Shutdown {self._name}")

        current_status = self.server_status()

        # If already SHUTDOWN, don't send another shutdown request
        if current_status == LanguageServerStatus.SHUTDOWN:
            self._logger.debug(f"[{self._name}] Already shutdown")
            return

        # If NOT_STARTED, there's nothing to shutdown
        if current_status == LanguageServerStatus.NOT_STARTED:
            self._logger.debug(f"[{self._name}] Server was never started")
            return

        # Use threading.Timer for timeout instead of blocking on Event.wait().
        # Blocking would freeze the calling thread, making the UI unresponsive during shutdown.
        # With a timer, shutdown() returns immediately (async).

        def _timeout_handler():
            # Only force exit if not already shutdown
            if self.server_status() != LanguageServerStatus.SHUTDOWN:
                self._logger.warning(
                    f"[{self._name}] Shutdown request timed out after {timeout}s, forcing exit"
                )
                self._exit()

        # Start timeout timer
        timeout_timer = threading.Timer(timeout, _timeout_handler)
        timeout_timer.daemon = True
        timeout_timer.start()

        def _callback(response):
            # Cancel timeout timer since we got a response
            timeout_timer.cancel()

            # Only process if not already shutdown
            if self.server_status() == LanguageServerStatus.SHUTDOWN:
                return

            # Always call exit(), even if shutdown returned an error.
            # Rationale:
            # 1. If server returned an error, it's likely in a bad state and we need to clean up
            # 2. Not calling exit() leaves threads running and resources leaked
            # 3. The LSP spec says to exit after shutdown response, regardless of error
            #
            # We log the error but proceed with cleanup.
            if response.get("error"):
                error = response["error"]
                self._logger.error(
                    f"[{self._name}] Shutdown request returned error: "
                    f"code={error.get('code')}, message={error.get('message')}"
                )

            self._exit()

        # Try to send graceful shutdown request
        self._put(request("shutdown"), _callback)

    def _exit(self):
        """
        Internal method to send exit notification and clean up resources.

        This should only be called internally after shutdown completes.
        The LSP protocol requires clients to call shutdown first, wait for response,
        then call exit.

        The server should exit with success code 0 if the shutdown request has been received before;
        otherwise with error code 1.

        NOTE: This method blocks on process.wait(). It's safe because it's only called
        from background threads (shutdown callback or timeout handler).

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#exit
        """

        # Make this method idempotent - safe to call multiple times
        with self._lock:
            if self._server_status == LanguageServerStatus.SHUTDOWN:
                return

            self._logger.info(f"Exit {self._name}")

            self._server_status = LanguageServerStatus.SHUTDOWN

            # Clear pending callbacks during shutdown to prevent memory leaks.
            # Any in-flight requests won't receive responses since the server is exiting.
            self._clear_callbacks()

        self._put(notification("exit"))

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

        The document's content is now managed by the client
        and the server must not try to read the document's content using the document's Uri.

        Open in this sense means it is managed by the client.
        It doesn't necessarily mean that its content is presented in an editor.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didOpen
        """

        # An open notification must not be sent more than once without a corresponding close notification send before.
        # This means open and close notification must be balanced and the max open count for a particular textDocument is one.
        textDocument_uri = params["textDocument"]["uri"]

        with self._lock:
            if textDocument_uri in self._open_documents:
                return

            self._open_documents.add(textDocument_uri)

        self._put(notification("textDocument/didOpen", params))

    def textDocument_didClose(
        self,
        params: LSPDidCloseTextDocumentParams,
    ):
        """
        The document close notification is sent from the client to the server
        when the document got closed in the client.

        The document's master now exists where
        the document's Uri points to (e.g. if the document's Uri is a file Uri the master now exists on disk).

        As with the open notification the close notification
        is about managing the document's content.
        Receiving a close notification doesn't mean that the document was open in an editor before.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didClose
        """

        textDocument_uri = params["textDocument"]["uri"]

        with self._lock:
            # A close notification requires a previous open notification to be sent.
            if textDocument_uri not in self._open_documents:
                return

            self._open_documents.remove(textDocument_uri)

        self._put(notification("textDocument/didClose", params))

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
        with self._lock:
            if params["textDocument"]["uri"] not in self._open_documents:
                return

        self._put(notification("textDocument/didChange", params))

    def textDocument_hover(
        self,
        params: LSPTextDocumentPositionParams,
        on_result: Callable[[LSPHoverResult], None],
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The hover request is sent from the client to the server to request
        hover information at a given text document position.

        Response result: Hover | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_hover
        """

        self._put(
            request("textDocument/hover", params),
            self._make_callback(on_result, on_error),
        )

    def textDocument_definition(
        self,
        params: LSPTextDocumentPositionParams,
        on_result: Callable[[LSPDefinitionResult], None],
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The go to definition request is sent from the client to the server
        to resolve the definition location of a symbol at a given text document position.

        Response result: Location | Location[] | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_definition
        """

        self._put(
            request("textDocument/definition", params),
            self._make_callback(on_result, on_error),
        )

    def textDocument_references(
        self,
        params,
        on_result: Callable[[LSPReferencesResult], None],
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The references request is sent from the client to the server
        to resolve project-wide references for the symbol denoted by the given text document position.

        Response result: Location[] | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_references
        """

        self._put(
            request("textDocument/references", params),
            self._make_callback(on_result, on_error),
        )

    def textDocument_documentHighlight(
        self,
        params: LSPTextDocumentPositionParams,
        callback: Callable[[LSPDocumentHighlightResult], None],
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The document highlight request is sent from the client to
        the server to resolve document highlights for a given text document position.

        For programming languages this usually highlights all references to the symbol scoped to this file.

        Response result: DocumentHighlight[] | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentHighlight
        """

        self._put(
            request("textDocument/documentHighlight", params),
            self._make_callback(callback, on_error),
        )

    def textDocument_documentSymbol(
        self,
        params,
        on_result: Callable[[LSPDocumentSymbolResult], None],
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The document symbol request is sent from the client to the server.

        Response result: DocumentSymbol[] | SymbolInformation[] | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentSymbol
        """

        self._put(
            request("textDocument/documentSymbol", params),
            self._make_callback(on_result, on_error),
        )

    def textDocument_formatting(
        self,
        params: LSPDocumentFormattingParams,
        on_result: Callable[[LSPFormattingResult], None],
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The document formatting request is sent from the client to the server to format a whole document.

        Response result: TextEdit[] | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_formatting
        """
        self._put(
            request("textDocument/formatting", params),
            self._make_callback(on_result, on_error),
        )

    def textDocument_rangeFormatting(
        self,
        params,
        on_result: Callable[[LSPFormattingResult], None],
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The document range formatting request is sent from the client to the server to format a specific range in a document.

        Response result: TextEdit[] | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#documentRangeFormattingParams
        """
        self._put(
            request("textDocument/rangeFormatting", params),
            self._make_callback(on_result, on_error),
        )

    def textDocument_completion(
        self,
        params: LSPTextDocumentPositionParams,
        callback: LSPCompletionResultCallback,
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The completion request is sent from the client to the server to compute completion items at a given cursor position.

        Response result: CompletionItem[] | CompletionList | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_completion
        """
        self._put(
            request("textDocument/completion", params),
            self._make_callback(callback, on_error),
        )

    def textDocument_signatureHelp(
        self,
        params: LSPSignatureHelpParams,
        callback: LSPSignatureHelpResultCallback,
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The signature help request is sent from the client to the server to request signature information at a given cursor position.

        Response result: SignatureHelp | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_signatureHelp
        """
        self._put(
            request("textDocument/signatureHelp", params),
            self._make_callback(callback, on_error),
        )

    def workspace_symbol(
        self,
        params: LSPWorkspaceSymbolParams,
        callback: LSPWorkspaceSymbolResultCallback,
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The workspace symbol request is sent from the client to the server to list project-wide symbols matching the query string.

        Response result: SymbolInformation[] | WorkspaceSymbol[] | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#workspace_symbol
        """

        self._put(
            request("workspace/symbol", params),
            self._make_callback(callback, on_error),
        )

    def textDocument_rename(
        self,
        params: LSPRenameParams,
        callback: LSPRenameResultCallback,
        on_error: Optional[Callable[[LSPResponseError], None]] = None,
    ):
        """
        The rename request is sent from the client to the server to perform a workspace-wide rename of a symbol.

        Response result: WorkspaceEdit | null

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_rename
        """

        self._put(
            request("textDocument/rename", params),
            self._make_callback(callback, on_error),
        )
