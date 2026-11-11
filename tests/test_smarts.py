import unittest

from fake_sublime import Region, load_smarts

smarts = load_smarts()


class FakeView:
    """A view of an ASCII document - so a column is a character, in any encoding."""

    def __init__(self, text="", file_name="/tmp/a.py", sel=()):
        self.text = text
        self._file_name = file_name
        self._sel = list(sel)
        self._change_count = 0
        self.commands = []

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
