from typing import TypedDict, List, Dict, Any, Optional, Union


class SmartsServerConfig(TypedDict):
    name: str
    start: List[str]
    applicable_to: List[str]


class SmartsInitializeData(TypedDict, total=False):
    name: str
    rootPath: str  # Optional.


class SmartsProjectData(TypedDict):
    initialize: List[SmartsInitializeData]


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


class LSPPosition(TypedDict):
    """
    Position in a text document expressed as zero-based line and zero-based character offset.

    A position is between two characters like an ‘insert’ cursor in an editor.

    Special values like for example -1 to denote the end of a line are not supported.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#position
    """

    line: int
    character: int


class LSPTextDocumentPositionParams(TypedDict):
    """
    A parameter literal used in requests to pass a text document and a position inside that document.

    It is up to the client to decide how a selection is converted into a position when issuing a request for a text document.
    The client can for example honor or ignore the selection direction to make LSP request consistent with features implemented internally.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentPositionParams
    """

    textDocument: LSPTextDocumentIdentifier
    position: LSPPosition
