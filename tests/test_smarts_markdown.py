import sys
from pathlib import Path

# Add lib directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "lib"))

from smarts_markdown import markdown_to_html


class TestCodeBlocks:
    def test_fenced_code_block(self):
        md = "```\ncode here\n```"
        html = markdown_to_html(md)
        assert "<pre>code here</pre>" in html

    def test_fenced_code_block_multiline(self):
        md = "```\nline1\nline2\nline3\n```"
        html = markdown_to_html(md)
        assert "<pre>line1<br>line2<br>line3</pre>" in html

    def test_fenced_code_block_with_language(self):
        md = "```python\nprint('hello')\n```"
        html = markdown_to_html(md)
        assert "<pre>print(&#x27;hello&#x27;)</pre>" in html

    def test_code_block_escapes_html(self):
        md = "```\n<script>alert('xss')</script>\n```"
        html = markdown_to_html(md)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestInlineCode:
    def test_inline_code(self):
        md = "Use `code` here"
        html = markdown_to_html(md)
        assert "<code>code</code>" in html

    def test_inline_code_escapes_html(self):
        md = "Use `<tag>` here"
        html = markdown_to_html(md)
        assert "<code>&lt;tag&gt;</code>" in html


class TestLinks:
    def test_link(self):
        md = "[text](https://example.com)"
        html = markdown_to_html(md)
        assert '<a href="https://example.com">text</a>' in html

    def test_link_with_special_chars_in_text(self):
        md = "[click & go](https://example.com)"
        html = markdown_to_html(md)
        assert "click &amp; go" in html


class TestBold:
    def test_bold_asterisks(self):
        md = "This is **bold** text"
        html = markdown_to_html(md)
        assert "<strong>bold</strong>" in html

    def test_bold_underscores(self):
        md = "This is __bold__ text"
        html = markdown_to_html(md)
        assert "<strong>bold</strong>" in html


class TestItalic:
    def test_italic_asterisk(self):
        md = "This is *italic* text"
        html = markdown_to_html(md)
        assert "<em>italic</em>" in html

    def test_italic_underscore(self):
        md = "This is _italic_ text"
        html = markdown_to_html(md)
        assert "<em>italic</em>" in html


class TestHeaders:
    def test_h1(self):
        md = "# Header 1"
        html = markdown_to_html(md)
        assert "<h1>Header 1</h1>" in html

    def test_h2(self):
        md = "## Header 2"
        html = markdown_to_html(md)
        assert "<h2>Header 2</h2>" in html

    def test_h3(self):
        md = "### Header 3"
        html = markdown_to_html(md)
        assert "<h3>Header 3</h3>" in html

    def test_header_with_inline_formatting(self):
        md = "## Header with **bold**"
        html = markdown_to_html(md)
        assert "<h2>Header with <strong>bold</strong></h2>" in html


class TestHorizontalRule:
    def test_hr_dashes(self):
        md = "---"
        html = markdown_to_html(md)
        assert "<div class='hr'></div>" in html

    def test_hr_asterisks(self):
        md = "***"
        html = markdown_to_html(md)
        assert "<div class='hr'></div>" in html

    def test_hr_underscores(self):
        md = "___"
        html = markdown_to_html(md)
        assert "<div class='hr'></div>" in html

    def test_hr_long(self):
        md = "----"
        html = markdown_to_html(md)
        assert "<div class='hr'></div>" in html


class TestLists:
    def test_unordered_list_dash(self):
        md = "- item 1\n- item 2"
        html = markdown_to_html(md)
        assert "<ul>" in html
        assert "<li>item 1</li>" in html
        assert "<li>item 2</li>" in html
        assert "</ul>" in html

    def test_unordered_list_asterisk(self):
        md = "* item 1\n* item 2"
        html = markdown_to_html(md)
        assert "<ul>" in html
        assert "<li>item 1</li>" in html
        assert "<li>item 2</li>" in html

    def test_unordered_list_plus(self):
        md = "+ item 1\n+ item 2"
        html = markdown_to_html(md)
        assert "<ul>" in html
        assert "<li>item 1</li>" in html

    def test_list_with_inline_code(self):
        md = "- `code` item"
        html = markdown_to_html(md)
        assert "<li><code>code</code> item</li>" in html

    def test_list_multiline_item(self):
        md = "- item line 1\ncontinuation line\n- item 2"
        html = markdown_to_html(md)
        assert "<li>item line 1 continuation line</li>" in html
        assert "<li>item 2</li>" in html


class TestParagraphs:
    def test_simple_paragraph(self):
        md = "This is a paragraph."
        html = markdown_to_html(md)
        assert "<p>This is a paragraph.</p>" in html

    def test_paragraph_escapes_html(self):
        md = "Text with <html> tags"
        html = markdown_to_html(md)
        assert "&lt;html&gt;" in html

    def test_multiline_paragraph(self):
        md = "Line 1\nLine 2"
        html = markdown_to_html(md)
        assert "<p>Line 1 Line 2</p>" in html


class TestEmptyLines:
    def test_empty_line_creates_br(self):
        md = "Para 1\n\nPara 2"
        html = markdown_to_html(md)
        assert "<div><br /></div>" in html

    def test_multiple_empty_lines(self):
        md = "Para 1\n\n\nPara 2"
        html = markdown_to_html(md)
        assert html.count("<div><br /></div>") == 2


class TestComplexDocuments:
    def test_code_block_followed_by_paragraph(self):
        md = "```\ncode\n```\n\nParagraph after code"
        html = markdown_to_html(md)
        assert "<pre>code</pre>" in html
        assert "<div><br /></div>" in html
        assert "<p>Paragraph after code</p>" in html

    def test_list_followed_by_hr(self):
        md = "- item 1\n- item 2\n\n---\n\n- item 3"
        html = markdown_to_html(md)
        assert "<ul><li>item 1</li><li>item 2</li></ul>" in html
        assert "<div class='hr'></div>" in html
