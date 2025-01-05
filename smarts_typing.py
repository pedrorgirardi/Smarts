from typing import TypedDict, List


class SmartsServerConfig(TypedDict):
    name: str
    start: List[str]
    applicable_to: List[str]


class SmartsInitializeData(TypedDict, total=False):
    name: str
    rootPath: str  # Optional.


class SmartsProjectData(TypedDict):
    initialize: List[SmartsInitializeData]
