"""
Tests which run inside Sublime Text - with UnitTesting; See unittesting.json.

They cover what the fake of Sublime's API (tests/fake_sublime.py) can't:
the editor's own conversion of points, and the changes it reports as text is
edited - with characters which are wider than one code unit in UTF-16, and
than one byte in UTF-8.

The editor is the user's own: every test works in a window of its own, with
Smarts of its own - without a server - and leaves both as they were.
"""

import logging
import os
import shutil
import tempfile

import sublime
from unittesting import DeferrableTestCase

from Smarts import smarts

# (The plugin's own copy of the client; See the reload in smarts.py.)
smarts_client = smarts.smarts_client

# 'é' is 1 UTF-16 code unit, and 2 UTF-8 bytes; '😀' is 2 code units, 4 bytes.
TEXT = "é = 1\n😀 = 2\n"


def new_window() -> sublime.Window:
    sublime.run_command("new_window")

    return sublime.active_window()


def close_window(window: sublime.Window):
    # (Scratch views - closing doesn't ask to save.)
    for view in window.views():
        view.set_scratch(True)

    window.run_command("close_window")


def add_smart(window: sublime.Window, name: str, position_encoding: str):
    """
    Add an initialized Smart to `window` - without a server: messages stay in
    the send queue of its client; See `Sent`.
    """
    client = smarts_client.LanguageServerClient(
        logger=logging.getLogger("Smarts.editor_tests"),
        name=name,
        server_args=[],
    )
    client._server_status = smarts_client.LanguageServerStatus.INITIALIZED
    client._server_capabilities = {"positionEncoding": position_encoding}
    # Incremental: changes are reported with their ranges.
    client._text_document_sync = smarts_client.textDocumentSyncOptions(2)

    smart = smarts.PgSmart(
        uuid=f"editor-tests-{name}",
        window=window.id(),
        config={
            "name": name,
            "start": [],
            "applicable_to": ["Packages/Python/Python.sublime-syntax"],
        },
        client=client,
    )

    with smarts._SMARTS_LOCK:
        smarts._SMARTS.append(smart)

    return smart


def remove_smart(smart):
    with smarts._SMARTS_LOCK:
        smarts._SMARTS[:] = [s for s in smarts._SMARTS if s.uuid != smart.uuid]


class Sent:
    """The notifications a Smart sent - read from the send queue of its client."""

    def __init__(self, smart):
        self.queue = smart.client._send_queue
        self.messages = []

    def __call__(self, method: str) -> list[dict]:
        """Returns the params of every `method` sent so far."""
        while not self.queue.empty():
            self.messages.append(self.queue.get())

        return [m["params"] for m in self.messages if m["method"] == method]


class TestPositions(DeferrableTestCase):
    """Points to LSP positions and back - by the editor's own conversion."""

    @classmethod
    def setUpClass(cls):
        cls.window = new_window()
        cls.view = cls.window.new_file()
        cls.view.set_scratch(True)
        cls.view.run_command("append", {"characters": TEXT})

    @classmethod
    def tearDownClass(cls):
        close_window(cls.window)

    def point(self, s: str) -> int:
        return self.view.find(s, 0, sublime.LITERAL).begin()

    def assert_position(self, s: str, utf16: tuple, utf8: tuple):
        point = self.point(s)

        for encoding, (line, character) in (("utf-16", utf16), ("utf-8", utf8)):
            position = {"line": line, "character": character}

            with self.subTest(s=s, encoding=encoding):
                self.assertEqual(
                    position, smarts.point_position(self.view, encoding, point)
                )
                self.assertEqual(
                    point, smarts.position_point(self.view, encoding, position)
                )

    def test_after_a_character_of_one_utf16_code_unit(self):
        self.assert_position(" = 1", utf16=(0, 1), utf8=(0, 2))

    def test_after_a_character_of_two_utf16_code_units(self):
        self.assert_position(" = 2", utf16=(1, 2), utf8=(1, 4))

    def test_column_past_the_end_of_a_line_is_clamped(self):
        end_of_line = self.point("\n😀")

        for encoding in ("utf-16", "utf-8"):
            with self.subTest(encoding=encoding):
                self.assertEqual(
                    end_of_line,
                    smarts.position_point(
                        self.view, encoding, {"line": 0, "character": 100}
                    ),
                )


class TestDocumentSync(DeferrableTestCase):
    """
    A file is opened, and edited, in the editor: its servers are notified -
    each in its own position encoding.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="smarts-editor-tests-")
        self.path = os.path.join(self.directory, "a.py")

        with open(self.path, "w", encoding="utf-8") as f:
            f.write(TEXT)

        self.window = new_window()
        self.utf16 = add_smart(self.window, "UTF-16", "utf-16")
        self.utf8 = add_smart(self.window, "UTF-8", "utf-8")
        self.sent16 = Sent(self.utf16)
        self.sent8 = Sent(self.utf8)

        # (Cleanups run even if a test fails - last added, first.)
        #
        # The Smarts are removed before the window is closed: closing it
        # shuts down its Smarts - and these don't have a server.
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.addCleanup(close_window, self.window)
        self.addCleanup(remove_smart, self.utf16)
        self.addCleanup(remove_smart, self.utf8)

    def open_file(self):
        view = self.window.open_file(self.path)

        yield lambda: not view.is_loading()

        self.view = view
        self.uri = smarts.path_to_uri(view.file_name())

    def insert(self, characters: str, after: str):
        point = self.view.find(after, 0, sublime.LITERAL).end()

        self.view.sel().clear()
        self.view.sel().add(point)
        self.view.run_command("insert", {"characters": characters})

    def test_opened_file_is_opened_for_every_server(self):
        yield from self.open_file()

        for sent in (self.sent16, self.sent8):
            yield lambda: sent("textDocument/didOpen")

            [params] = sent("textDocument/didOpen")

            self.assertEqual(self.uri, params["textDocument"]["uri"])
            self.assertEqual("python", params["textDocument"]["languageId"])
            self.assertEqual(TEXT, params["textDocument"]["text"])

    def test_changes_are_ranges_in_the_encoding_of_each_server(self):
        yield from self.open_file()

        # After '😀': 2 code units, 4 bytes into the line.
        self.insert("x", after="😀")

        expected = {
            self.sent16: {"line": 1, "character": 2},
            self.sent8: {"line": 1, "character": 4},
        }

        for sent, position in expected.items():
            yield lambda: sent("textDocument/didChange")

            [params] = sent("textDocument/didChange")

            self.assertEqual(
                {"uri": self.uri, "version": self.view.change_count()},
                params["textDocument"],
            )
            self.assertEqual(
                [
                    {
                        "range": {"start": position, "end": position},
                        "rangeLength": 0,
                        "text": "x",
                    }
                ],
                params["contentChanges"],
            )

    def test_replaced_text_has_its_length_in_the_encoding_of_each_server(self):
        yield from self.open_file()

        # Replace '😀' - 2 code units, 4 bytes - with 'y'.
        start = self.view.find("😀", 0, sublime.LITERAL)

        self.view.sel().clear()
        self.view.sel().add(start)
        self.view.run_command("insert", {"characters": "y"})

        expected = {
            self.sent16: ({"line": 1, "character": 2}, 2),
            self.sent8: ({"line": 1, "character": 4}, 4),
        }

        for sent, (end, length) in expected.items():
            yield lambda: sent("textDocument/didChange")

            [params] = sent("textDocument/didChange")

            self.assertEqual(
                [
                    {
                        "range": {"start": {"line": 1, "character": 0}, "end": end},
                        "rangeLength": length,
                        "text": "y",
                    }
                ],
                params["contentChanges"],
            )
