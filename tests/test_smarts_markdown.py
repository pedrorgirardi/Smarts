import unittest

from smarts_markdown import markdown_to_html


class TestCodeBlocks(unittest.TestCase):
    def test_fenced_code_block(self):
        md = "```\ncode here\n```"
        html = markdown_to_html(md)
        self.assertIn("<pre>code here</pre>", html)

    def test_fenced_code_block_multiline(self):
        md = "```\nline1\nline2\nline3\n```"
        html = markdown_to_html(md)
        self.assertIn("<pre>line1<br>line2<br>line3</pre>", html)

    def test_fenced_code_block_with_language(self):
        md = "```python\nprint('hello')\n```"
        html = markdown_to_html(md)
        self.assertIn("<pre>print(&#x27;hello&#x27;)</pre>", html)

    def test_code_block_escapes_html(self):
        md = "```\n<script>alert('xss')</script>\n```"
        html = markdown_to_html(md)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestInlineCode(unittest.TestCase):
    def test_inline_code(self):
        md = "Use `code` here"
        html = markdown_to_html(md)
        self.assertIn("<code>code</code>", html)

    def test_inline_code_escapes_html(self):
        md = "Use `<tag>` here"
        html = markdown_to_html(md)
        self.assertIn("<code>&lt;tag&gt;</code>", html)


class TestLinks(unittest.TestCase):
    def test_link(self):
        md = "[text](https://example.com)"
        html = markdown_to_html(md)
        self.assertIn('<a href="https://example.com">text</a>', html)

    def test_link_with_special_chars_in_text(self):
        md = "[click & go](https://example.com)"
        html = markdown_to_html(md)
        self.assertIn("click &amp; go", html)


class TestBold(unittest.TestCase):
    def test_bold_asterisks(self):
        md = "This is **bold** text"
        html = markdown_to_html(md)
        self.assertIn("<strong>bold</strong>", html)

    def test_bold_underscores(self):
        md = "This is __bold__ text"
        html = markdown_to_html(md)
        self.assertIn("<strong>bold</strong>", html)


class TestItalic(unittest.TestCase):
    def test_italic_asterisk(self):
        md = "This is *italic* text"
        html = markdown_to_html(md)
        self.assertIn("<em>italic</em>", html)

    def test_italic_underscore(self):
        md = "This is _italic_ text"
        html = markdown_to_html(md)
        self.assertIn("<em>italic</em>", html)


class TestHeaders(unittest.TestCase):
    def test_h1(self):
        md = "# Header 1"
        html = markdown_to_html(md)
        self.assertIn("<h1>Header 1</h1>", html)

    def test_h2(self):
        md = "## Header 2"
        html = markdown_to_html(md)
        self.assertIn("<h2>Header 2</h2>", html)

    def test_h3(self):
        md = "### Header 3"
        html = markdown_to_html(md)
        self.assertIn("<h3>Header 3</h3>", html)

    def test_header_with_inline_formatting(self):
        md = "## Header with **bold**"
        html = markdown_to_html(md)
        self.assertIn("<h2>Header with <strong>bold</strong></h2>", html)


class TestHorizontalRule(unittest.TestCase):
    def test_hr_dashes(self):
        md = "---"
        html = markdown_to_html(md)
        self.assertIn("<div class='hr'></div>", html)

    def test_hr_asterisks(self):
        md = "***"
        html = markdown_to_html(md)
        self.assertIn("<div class='hr'></div>", html)

    def test_hr_underscores(self):
        md = "___"
        html = markdown_to_html(md)
        self.assertIn("<div class='hr'></div>", html)

    def test_hr_long(self):
        md = "----"
        html = markdown_to_html(md)
        self.assertIn("<div class='hr'></div>", html)


class TestLists(unittest.TestCase):
    def test_unordered_list_dash(self):
        md = "- item 1\n- item 2"
        html = markdown_to_html(md)
        self.assertIn("<ul>", html)
        self.assertIn("<li>item 1</li>", html)
        self.assertIn("<li>item 2</li>", html)
        self.assertIn("</ul>", html)

    def test_unordered_list_asterisk(self):
        md = "* item 1\n* item 2"
        html = markdown_to_html(md)
        self.assertIn("<ul>", html)
        self.assertIn("<li>item 1</li>", html)
        self.assertIn("<li>item 2</li>", html)
        self.assertIn("</ul>", html)

    def test_unordered_list_plus(self):
        md = "+ item 1\n+ item 2"
        html = markdown_to_html(md)
        self.assertIn("<ul>", html)
        self.assertIn("<li>item 1</li>", html)
        self.assertIn("<li>item 2</li>", html)
        self.assertIn("</ul>", html)

    def test_list_with_inline_code(self):
        md = "- `code` item"
        html = markdown_to_html(md)
        self.assertIn("<li><code>code</code> item</li>", html)

    def test_list_multiline_item(self):
        md = "- item line 1\ncontinuation line\n- item 2"
        html = markdown_to_html(md)
        self.assertIn("<li>item line 1 continuation line</li>", html)
        self.assertIn("<li>item 2</li>", html)


class TestParagraphs(unittest.TestCase):
    def test_simple_paragraph(self):
        md = "This is a paragraph."
        html = markdown_to_html(md)
        self.assertIn("<p>This is a paragraph.</p>", html)

    def test_paragraph_escapes_html(self):
        md = "Text with <html> tags"
        html = markdown_to_html(md)
        self.assertIn("&lt;html&gt;", html)

    def test_multiline_paragraph(self):
        md = "Line 1\nLine 2"
        html = markdown_to_html(md)
        self.assertIn("<p>Line 1 Line 2</p>", html)


class TestEmptyLines(unittest.TestCase):
    def test_empty_line_creates_br(self):
        md = "Para 1\n\nPara 2"
        html = markdown_to_html(md)
        self.assertIn("<div><br /></div>", html)

    def test_multiple_empty_lines(self):
        md = "Para 1\n\n\nPara 2"
        html = markdown_to_html(md)
        self.assertEqual(html.count("<div><br /></div>"), 2)


class TestComplexDocuments(unittest.TestCase):
    def test_code_block_followed_by_paragraph(self):
        md = "```\ncode\n```\n\nParagraph after code"
        html = markdown_to_html(md)
        self.assertIn("<pre>code</pre>", html)
        self.assertIn("<div><br /></div>", html)
        self.assertIn("<p>Paragraph after code</p>", html)

    def test_list_followed_by_hr(self):
        md = "- item 1\n- item 2\n\n---\n\n- item 3"
        html = markdown_to_html(md)
        self.assertIn("<ul><li>item 1</li><li>item 2</li></ul>", html)
        self.assertIn("<div class='hr'></div>", html)
