from typing import TypedDict, List, Dict, Any


class SmartsServerConfig(TypedDict):
    name: str
    start: List[str]
    applicable_to: List[str]


class SmartsInitializeData(TypedDict, total=False):
    name: str
    rootPath: str  # Optional.


class SmartsProjectData(TypedDict):
    initialize: List[SmartsInitializeData]


LSPMessage = Dict[str, Any]
