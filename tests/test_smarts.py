import logging
import unittest
from typing import Any

from fake_sublime import Region, load_smarts

# (Any: the module is loaded dynamically, so its attributes aren't known.)
smarts: Any = load_smarts()

# (The plugin imports its own copy of the client - as a module of its package.)
smarts_client = smarts.smarts_client

PYTHON_SYNTAX = "Packages/Python/Python.sublime-syntax"


class FakeSyntax:
    scope = "source.python"


class FakeBuffer:
    def __init__(self, id=1):
        self._id = id
        self._views = []

    def views(self):
        return self._views


class FakeWindow:
    def __init__(self, id=1):
        self._id = id
        self.commands = []
        self.panel = FakeView(file_name=None)
        self.open_files = {}

    def id(self):
        return self._id

    def run_command(self, name, args=None):
        self.commands.append((name, args))

    def find_output_panel(self, name):
        return self.panel

    def find_open_file(self, path):
        return self.open_files.get(path)

    def project_data(self):
        return None


class FakeView:
    """A view of an ASCII document - so a column is a character, in any encoding."""

    ids = iter(range(1, 1_000_000))

    def __init__(
        self,
        text="",
        file_name: str | None = "/tmp/a.py",
        sel=(),
        window=None,
        buffer=None,
    ):
        self.text = text
        self._id = next(FakeView.ids)
        self._file_name = file_name
        self._sel = list(sel)
        self._change_count = 0
        self._window = window
        self._buffer = buffer or FakeBuffer()
        self._buffer._views.append(self)
        self.commands = []
        self.status = {}

    def id(self):
        return self._id

    def window(self):
        return self._window

    def buffer(self):
        return self._buffer

    def buffer_id(self):
        return self._buffer._id

    def settings(self):
        return {"syntax": PYTHON_SYNTAX}

    def syntax(self):
        return FakeSyntax()

    def size(self):
        return len(self.text)

    def substr(self, region):
        return self.text[region.begin() : region.end()]

    def erase_regions(self, key):
        pass

    def erase_status(self, key):
        self.status.pop(key, None)

    def set_status(self, key, value):
        self.status[key] = value

    def file_name(self):
        return self._file_name

    def sel(self):
        return self._sel

    def change_count(self):
        return self._change_count

    def run_command(self, name, args=None):
        self.commands.append((name, args))

    def rowcol_utf16(self, point):
        before = self.text[:point]

        return (before.count("\n"), len(before) - (before.rfind("\n") + 1))

    rowcol_utf8 = rowcol_utf16


def add_smart(window: FakeWindow, text_document_sync, name="Fake"):
    """
    Add an initialized Smart to `window` - without a server: messages stay in
    the send queue of its client; See `sent`.
    """
    client = smarts_client.LanguageServerClient(
        logger=logging.getLogger("test"),
        name=name,
        server_args=[],
    )
    client._server_status = smarts_client.LanguageServerStatus.INITIALIZED
    client._server_capabilities = {}
    client._text_document_sync = smarts_client.textDocumentSyncOptions(
        text_document_sync
    )

    smart = smarts.PgSmart(
        uuid=name,
        window=window.id(),
        config={"name": name, "start": [], "applicable_to": [PYTHON_SYNTAX]},
        client=client,
    )

    smarts._SMARTS.append(smart)

    return smart


def sent(smart) -> list[tuple[str, dict]]:
    """Returns (method, params) of the messages `smart` sent, and forgets them."""
    queue = smart.client._send_queue

    messages = []

    while not queue.empty():
        message = queue.get()

        messages.append((message["method"], message["params"]))

    return messages


class TestDocuments(unittest.TestCase):
    def setUp(self):
        smarts._SMARTS = []
        smarts._DOCUMENT_URI.clear()

        self.window = FakeWindow()
        self.view = FakeView(text="x = 1", window=self.window)

    def test_document_is_opened_once(self):
        smart = add_smart(self.window, {"openClose": True})

        smarts.document_open(self.view)
        smarts.document_open(self.view)

        self.assertEqual(
            sent(smart),
            [
                (
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": "file:///tmp/a.py",
                            "languageId": "python",
                            "version": 0,
                            "text": "x = 1",
                        }
                    },
                )
            ],
        )

    def test_document_is_not_opened_by_servers_which_do_not_sync(self):
        smart = add_smart(self.window, None)

        smarts.document_open(self.view)

        self.assertEqual(sent(smart), [])

    def test_document_without_a_file_is_not_opened(self):
        smart = add_smart(self.window, {"openClose": True})

        smarts.document_open(FakeView(file_name=None, window=self.window))
        smarts.document_open(FakeView(window=None))

        self.assertEqual(sent(smart), [])

    def test_document_saved_as_another_file_closes_the_previous(self):
        smart = add_smart(self.window, {"openClose": True})

        smarts.document_open(self.view)

        sent(smart)

        self.view._file_name = "/tmp/b.py"

        smarts.document_open(self.view)

        messages = sent(smart)

        self.assertEqual(
            [(method, params["textDocument"]["uri"]) for method, params in messages],
            [
                ("textDocument/didClose", "file:///tmp/a.py"),
                ("textDocument/didOpen", "file:///tmp/b.py"),
            ],
        )

    def test_did_save(self):
        without_text = add_smart(self.window, {"openClose": True, "save": {}}, "A")
        with_text = add_smart(
            self.window, {"openClose": True, "save": {"includeText": True}}, "B"
        )
        without_save = add_smart(self.window, {"openClose": True}, "C")

        smarts.document_open(self.view)

        for smart in (without_text, with_text, without_save):
            sent(smart)

        smarts.document_did_save(self.view)

        identifier = {"uri": "file:///tmp/a.py"}

        self.assertEqual(
            sent(without_text),
            [("textDocument/didSave", {"textDocument": identifier})],
        )
        self.assertEqual(
            sent(with_text),
            [("textDocument/didSave", {"textDocument": identifier, "text": "x = 1"})],
        )
        self.assertEqual(sent(without_save), [])

    def test_resync_sends_the_full_text(self):
        smart = add_smart(self.window, 2)

        smarts.document_open(self.view)

        sent(smart)

        self.view.text = "y = 2"
        self.view._change_count = 7

        smarts.document_resync(self.view)

        self.assertEqual(
            sent(smart),
            [
                (
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": "file:///tmp/a.py", "version": 7},
                        "contentChanges": [{"text": "y = 2"}],
                    },
                )
            ],
        )

    def test_clones_are_views_of_the_buffer_in_the_same_window(self):
        buffer = FakeBuffer()

        view = FakeView(window=self.window, buffer=buffer)

        self.assertEqual(smarts.view_clones(view), [])

        # A clone in another window.
        FakeView(window=FakeWindow(id=2), buffer=buffer)

        self.assertEqual(smarts.view_clones(view), [])

        clone = FakeView(window=self.window, buffer=buffer)

        self.assertEqual(smarts.view_clones(view), [clone])


class TestFailure(unittest.TestCase):
    def setUp(self):
        smarts._SMARTS = []
        smarts._DIAGNOSTICS.clear()

        self.window = FakeWindow()

        smarts.sublime.windows = lambda: [self.window]

    def test_failed_smart_is_removed_with_its_diagnostics(self):
        add_smart(self.window, None, "A")
        add_smart(self.window, None, "B")

        view = FakeView(window=self.window)
        view.status["PG_SMARTS_DIAGNOSTICS"] = "Error: 1"

        self.window.open_files["/tmp/a.py"] = view

        smarts.set_server_diagnostics(1, "/tmp/a.py", "A", [{"message": "a"}])

        smarts.handle_failure("A", "Server exited unexpectedly")

        self.assertIsNone(smarts.find_smart("A"))
        self.assertIsNotNone(smarts.find_smart("B"))
        self.assertEqual(smarts.path_diagnostics(1, "/tmp/a.py"), [])

        # Diagnostics are presented again - there's none.
        self.assertEqual(view.status, {"PG_SMARTS_DIAGNOSTICS": ""})

        # The reason is logged in the Output panel, which is shown.
        (_, args), = self.window.panel.commands

        self.assertIn("Server exited unexpectedly", args["characters"])
        self.assertEqual(
            self.window.commands,
            [("show_panel", {"panel": "output.Smarts - Output"})],
        )

    def test_failure_of_a_removed_smart_is_ignored(self):
        smarts.handle_failure("A", "Server exited unexpectedly")

        self.assertEqual(self.window.panel.commands, [])


class TestUri(unittest.TestCase):
    def test_path_to_uri_and_back(self):
        path = "/Users/me/Application Support/ação/a b.py"

        uri = smarts.path_to_uri(path)

        self.assertEqual(
            uri, "file:///Users/me/Application%20Support/a%C3%A7%C3%A3o/a%20b.py"
        )
        self.assertEqual(smarts.uri_to_path(uri), path)

    def test_uri_to_path_keeps_encoded_delimiters(self):
        self.assertEqual(smarts.uri_to_path("file:///a/b%23c%3Fd.py"), "/a/b#c?d.py")


class TestDiagnostics(unittest.TestCase):
    def setUp(self):
        smarts._DIAGNOSTICS.clear()

    def diagnostic(self, message: str, **kwargs) -> dict:
        return {"message": message, "server": "Pyright", **kwargs}

    def test_servers_do_not_overwrite_each_other(self):
        a = [self.diagnostic("a")]
        b = [self.diagnostic("b")]

        self.assertTrue(smarts.set_server_diagnostics(1, "/x.py", "A", a))
        self.assertTrue(smarts.set_server_diagnostics(1, "/x.py", "B", b))

        self.assertEqual(smarts.path_diagnostics(1, "/x.py"), a + b)

        # A publish replaces the diagnostics of its server only.
        self.assertTrue(smarts.set_server_diagnostics(1, "/x.py", "A", []))

        self.assertEqual(smarts.path_diagnostics(1, "/x.py"), b)

    def test_same_diagnostics_are_not_a_change(self):
        a = [self.diagnostic("a")]

        self.assertTrue(smarts.set_server_diagnostics(1, "/x.py", "A", a))
        self.assertFalse(smarts.set_server_diagnostics(1, "/x.py", "A", list(a)))

        # Neither is clearing what's clear already.
        self.assertFalse(smarts.set_server_diagnostics(1, "/y.py", "A", []))

    def test_documents_without_diagnostics_are_not_kept(self):
        smarts.set_server_diagnostics(1, "/x.py", "A", [self.diagnostic("a")])
        smarts.set_server_diagnostics(1, "/x.py", "A", [])

        self.assertEqual(smarts._DIAGNOSTICS, {})

    def test_diagnostics_are_per_window(self):
        smarts.set_server_diagnostics(1, "/x.py", "A", [self.diagnostic("a")])

        self.assertEqual(smarts.path_diagnostics(2, "/x.py"), [])

    def test_remove_server_diagnostics(self):
        b = [self.diagnostic("b")]

        smarts.set_server_diagnostics(1, "/x.py", "A", [self.diagnostic("a")])
        smarts.set_server_diagnostics(1, "/x.py", "B", b)
        smarts.set_server_diagnostics(1, "/y.py", "A", [self.diagnostic("a")])
        smarts.set_server_diagnostics(2, "/z.py", "B", b)

        affected = smarts.remove_server_diagnostics({"A"})

        self.assertEqual(affected, {(1, "/x.py"), (1, "/y.py")})
        self.assertEqual(smarts.path_diagnostics(1, "/x.py"), b)
        self.assertEqual(
            smarts._DIAGNOSTICS, {(1, "/x.py"): {"B": b}, (2, "/z.py"): {"B": b}}
        )

    def test_severity_defaults_to_error(self):
        self.assertEqual(smarts.diagnostic_severity(self.diagnostic("a")), 1)
        self.assertEqual(
            smarts.diagnostic_severity(self.diagnostic("a", severity=None)), 1
        )
        self.assertEqual(smarts.diagnostic_severity(self.diagnostic("a", severity=3)), 3)

    def test_origin(self):
        self.assertEqual(smarts.diagnostic_origin(self.diagnostic("a")), "Pyright")
        self.assertEqual(
            smarts.diagnostic_origin(self.diagnostic("a", source="pyright")), "Pyright"
        )
        self.assertEqual(
            smarts.diagnostic_origin(self.diagnostic("a", source="mypy")),
            "Pyright (mypy)",
        )


def edit(line: int, character: int, text: str, end: tuple[int, int] | None = None):
    end_line, end_character = end or (line, character)

    return {
        "range": {
            "start": {"line": line, "character": character},
            "end": {"line": end_line, "character": end_character},
        },
        "newText": text,
    }


def apply_edits(text: str, edits: list) -> str:
    """Apply `edits` to `text`, one by one, in the order the plugin applies them."""
    for e in smarts.text_edits_application_order(edits):
        lines = text.split("\n")

        def offset(position):
            return sum(len(l) + 1 for l in lines[: position["line"]]) + position[
                "character"
            ]

        a, b = offset(e["range"]["start"]), offset(e["range"]["end"])

        text = text[:a] + e["newText"] + text[b:]

    return text


class TestTextEdits(unittest.TestCase):
    def test_edits_are_applied_from_the_end(self):
        edits = [
            edit(0, 0, "A", end=(0, 1)),
            edit(1, 0, "LONGER", end=(1, 1)),
            edit(0, 2, "C", end=(0, 3)),
        ]

        self.assertEqual(apply_edits("abc\nx", edits), "AbC\nLONGER")

    def test_inserts_at_the_same_position_keep_their_order(self):
        edits = [edit(0, 1, "1"), edit(0, 1, "2"), edit(0, 1, "3")]

        self.assertEqual(apply_edits("ab", edits), "a123b")

    def test_edits_are_not_mutated(self):
        edits = [edit(0, 0, "a"), edit(1, 0, "b")]

        smarts.text_edits_application_order(edits)

        self.assertEqual([e["newText"] for e in edits], ["a", "b"])

    def test_applier_applies_edits(self):
        view = FakeView()

        on_result = smarts.text_edits_applier(view, "utf-16")

        on_result(None)
        on_result([])

        self.assertEqual(view.commands, [])

        edits = [edit(0, 0, "a")]

        on_result(edits)

        self.assertEqual(
            view.commands,
            [("pg_smarts_apply_edits", {"position_encoding": "utf-16", "edits": edits})],
        )

    def test_applier_drops_edits_if_document_changed(self):
        view = FakeView()

        on_result = smarts.text_edits_applier(view, "utf-16")

        view._change_count += 1

        on_result([edit(0, 0, "a")])

        self.assertEqual(view.commands, [])


class TestDocumentSymbols(unittest.TestCase):
    def test_flat_symbols_are_kept(self):
        symbols = [{"name": "a", "containerName": "m"}, {"name": "b"}]

        self.assertEqual(smarts.flatten_document_symbols(symbols), symbols)

    def test_children_are_flattened_in_document_order(self):
        symbols = [
            {
                "name": "A",
                "children": [
                    {"name": "f", "children": [{"name": "x"}]},
                    {"name": "g"},
                ],
            },
            {"name": "B"},
        ]

        flat = smarts.flatten_document_symbols(symbols)

        self.assertEqual(
            [(s["name"], s.get("containerName")) for s in flat],
            [("A", None), ("f", "A"), ("x", "A.f"), ("g", "A"), ("B", None)],
        )


class TestLanguageId(unittest.TestCase):
    def test_scope_names_the_language(self):
        self.assertEqual(smarts.scope_languageId("source.python"), "python")
        self.assertEqual(smarts.scope_languageId("source.go"), "go")
        self.assertEqual(smarts.scope_languageId("source.json.sublime"), "json")

    def test_scope_with_another_name(self):
        self.assertEqual(smarts.scope_languageId("source.ts"), "typescript")
        self.assertEqual(smarts.scope_languageId("source.c++"), "cpp")

    def test_scope_without_a_language(self):
        self.assertEqual(smarts.scope_languageId("text"), "")
        self.assertEqual(smarts.scope_languageId(""), "")


class TestCompletions(unittest.TestCase):
    def test_every_completion_item_kind_is_known(self):
        self.assertEqual(list(smarts.kCOMPLETION_ITEM_KIND), list(range(1, 26)))

    def test_completion_item_kinds(self):
        sublime = smarts.sublime

        self.assertNotEqual(sublime.KIND_ID_FUNCTION, sublime.KIND_ID_KEYWORD)

        kinds = smarts.kCOMPLETION_ITEM_KIND

        self.assertEqual(kinds[2], sublime.KIND_ID_FUNCTION)  # Method
        self.assertEqual(kinds[6], sublime.KIND_ID_VARIABLE)  # Variable
        self.assertEqual(kinds[7], sublime.KIND_ID_TYPE)  # Class
        self.assertEqual(kinds[9], sublime.KIND_ID_NAMESPACE)  # Module
        self.assertEqual(kinds[14], sublime.KIND_ID_KEYWORD)  # Keyword
        self.assertEqual(kinds[15], sublime.KIND_ID_SNIPPET)  # Snippet
        self.assertEqual(kinds[21], sublime.KIND_ID_VARIABLE)  # Constant

    def test_completions(self):
        items = smarts.completions(
            [
                {"label": "a"},
                {
                    "label": "b",
                    "insertText": "b()",
                    "detail": "def b()",
                    "documentation": {"kind": "markdown", "value": "Docs"},
                    "kind": 3,
                },
                {"label": "c", "documentation": "Text", "kind": 999},
            ]
        )

        self.assertEqual(
            [
                (
                    item.kwargs["trigger"],
                    item.kwargs["completion"],
                    item.kwargs["annotation"],
                    item.kwargs["details"],
                )
                for item in items
            ],
            [
                ("a", "a", "", ""),
                ("b", "b()", "def b()", "Docs"),
                ("c", "c", "", "Text"),
            ],
        )

        # The kind of the item; Unknown kinds are ambiguous.
        self.assertEqual(
            [item.kwargs["kind"][0] for item in items],
            [
                smarts.sublime.KIND_ID_AMBIGUOUS,
                smarts.sublime.KIND_ID_FUNCTION,
                smarts.sublime.KIND_ID_AMBIGUOUS,
            ],
        )


class TestPosition(unittest.TestCase):
    def test_point_zero_is_a_point(self):
        view = FakeView(text="ab\ncd", sel=[Region(4)])

        params = smarts.view_textDocumentPositionParams(view, "utf-16", 0)

        self.assertEqual(params["position"], {"line": 0, "character": 0})

    def test_point_defaults_to_the_caret(self):
        view = FakeView(text="ab\ncd", sel=[Region(4)])

        params = smarts.view_textDocumentPositionParams(view, "utf-16")

        self.assertEqual(params["position"], {"line": 1, "character": 1})
        self.assertEqual(params["textDocument"], {"uri": "file:///tmp/a.py"})

    def test_selection_is_not_read_if_point_is_given(self):
        view = FakeView(text="ab\ncd", sel=[])

        params = smarts.view_textDocumentPositionParams(view, "utf-16", 3)

        self.assertEqual(params["position"], {"line": 1, "character": 0})


class TestHover(unittest.TestCase):
    def markdown(self, contents) -> str:
        # (The command isn't needed: `self` is not used.)
        return smarts.PgSmartsShowHoverCommand._hover_markdown(
            None, {"contents": contents}
        )

    def test_markup_content(self):
        self.assertEqual(self.markdown({"kind": "markdown", "value": "# A"}), "# A")

    def test_marked_string(self):
        self.assertEqual(self.markdown("text"), "text")

    def test_marked_strings(self):
        self.assertEqual(
            self.markdown(["text", {"language": "python", "value": "x = 1"}]),
            "text\n\n```python\nx = 1\n```",
        )


class TestHtml(unittest.TestCase):
    def test_text_to_html(self):
        self.assertEqual(
            smarts.text_to_html("a <b>\n\tc"),
            "a&nbsp;&lt;b&gt;<br/>" + "&nbsp;" * 4 + "c",
        )


if __name__ == "__main__":
    unittest.main()
