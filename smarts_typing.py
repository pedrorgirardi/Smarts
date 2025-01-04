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
