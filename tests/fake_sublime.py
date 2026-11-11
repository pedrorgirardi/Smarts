"""
A fake of Sublime Text's API - just enough to import the plugin, and to test
the functions which don't need an editor.

Sublime Text's modules (`sublime`, `sublime_plugin`) only exist inside the
editor; `load_smarts` installs fakes of them, and imports the plugin.
"""

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent


class Region:
    def __init__(self, a: int, b: int | None = None):
        self.a = a
        self.b = a if b is None else b

    def __eq__(self, other):
        return isinstance(other, Region) and (self.a, self.b) == (other.a, other.b)

    def __repr__(self):
        return f"Region({self.a}, {self.b})"

    def begin(self) -> int:
        return min(self.a, self.b)

    def end(self) -> int:
        return max(self.a, self.b)

    def empty(self) -> bool:
        return self.a == self.b

    def contains(self, point: int) -> bool:
        return self.begin() <= point <= self.end()


class Record:
    """An object which keeps the arguments it was created with."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class FakeModule(types.ModuleType):
    """
    A module which has every attribute: constants - upper case names - are 0,
    so flags can be combined, and anything else is a class.
    """

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        value = 0 if name.isupper() else type(name, (Record,), {"NONE": 0})

        setattr(self, name, value)

        return value


def load_smarts() -> types.ModuleType:
    """Returns the plugin module - `smarts.py` - imported with a fake API."""
    if module := sys.modules.get("Smarts.smarts"):
        return module

    sublime = FakeModule("sublime")
    setattr(sublime, "Region", Region)

    sys.modules["sublime"] = sublime
    sys.modules["sublime_plugin"] = FakeModule("sublime_plugin")

    # The plugin is a module of a package - it imports `.lib` - named after
    # its directory in Sublime Text's Packages.
    package = types.ModuleType("Smarts")
    package.__path__ = [str(ROOT)]

    sys.modules["Smarts"] = package

    spec = importlib.util.spec_from_file_location("Smarts.smarts", ROOT / "smarts.py")

    assert spec and spec.loader

    module = importlib.util.module_from_spec(spec)

    sys.modules["Smarts.smarts"] = module

    spec.loader.exec_module(module)

    return module
