import html
import re
from collections.abc import Callable


# List item per list tag: `<marker> <content>`
#   ul: dash, asterisk, or plus
#   ol: a number followed by a dot or a parenthesis
_LIST_ITEM = {
    "ul": re.compile(r"^[-*+]\s+(.+)$"),
    "ol": re.compile(r"^\d+[.)]\s+(.+)$"),
}


def _list_tag(line: str) -> str | None:
    """Returns the tag of the list `line` is an item of - 'ul', 'ol' - or None."""
    for tag, item in _LIST_ITEM.items():
        if item.match(line):
            return tag

    return None


def markdown_to_html(
    text: str,
    *,
    code_block_renderer: Callable[[str], str],
) -> str:
    """Convert markdown string to HTML string.

    Handles:
    - Fenced code blocks (```) → rendered by callback
    - Inline code (`) → <code>
    - Links [text](url) → <a href="url">text</a>
    - Bold **text** → <strong>
    - Italic *text* → <em>
    - Headers # → <h1>, ## → <h2>, etc.
    - Lists: unordered (-, *, +) → <ul>, ordered (1.) → <ol>
    - Paragraphs
    """
    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.startswith("```"):
            block_start = i
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                i += 1
            if i < len(lines):
                i += 1
            code_block = "\n".join(lines[block_start:i])
            result.append(code_block_renderer(code_block))
            continue

        # Empty line - add line break (wrapped in div for block-level spacing)
        if not line.strip():
            result.append("<div><br /></div>")
            i += 1
            continue

        # Header: ^(#{1,6})\s+(.+)$
        #   (#{1,6})  - 1 to 6 hash characters (capture: heading level)
        #   \s+       - one or more spaces
        #   (.+)$     - rest of line (capture: heading content)
        header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if header_match:
            level = len(header_match.group(1))
            content = _process_inline(header_match.group(2))
            result.append(f"<h{level}>{content}</h{level}>")
            i += 1
            continue

        # Horizontal rule: ^(-{3,}|\*{3,}|_{3,})$
        #   -{3,}  - three or more dashes, OR
        #   \*{3,} - three or more asterisks, OR
        #   _{3,}  - three or more underscores
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", line.strip()):
            result.append("<div class='hr'></div>")
            i += 1
            continue

        # List - unordered or ordered; See _LIST_ITEM.
        list_tag = _list_tag(line)
        if list_tag:
            list_items = []
            current_item_lines = []
            while i < len(lines):
                current = lines[i]
                item_match = _LIST_ITEM[list_tag].match(current)
                if item_match:
                    # Save previous item if any
                    if current_item_lines:
                        item_text = " ".join(current_item_lines)
                        list_items.append(f"<li>{_process_inline(item_text)}</li>")
                    # Start new item
                    current_item_lines = [item_match.group(1)]
                    i += 1
                elif not current.strip():
                    # Empty line ends the list
                    break
                elif re.match(r"^(-{3,}|\*{3,}|_{3,})$", current.strip()):
                    # Horizontal rule ends the list
                    break
                elif current.startswith("```"):
                    # Code block ends the list
                    break
                elif _list_tag(current):
                    # An item of the other kind of list ends the list
                    break
                else:
                    # Continuation line - append to current item
                    current_item_lines.append(current)
                    i += 1
            # Save final item
            if current_item_lines:
                item_text = " ".join(current_item_lines)
                list_items.append(f"<li>{_process_inline(item_text)}</li>")
            result.append(f"<{list_tag}>{''.join(list_items)}</{list_tag}>")
            continue

        # Paragraph: collect consecutive non-empty, non-special lines
        para_lines = []
        while i < len(lines):
            current = lines[i]
            if not current.strip():
                break
            if current.startswith("```"):
                break
            if re.match(r"^#{1,6}\s+", current):
                break
            if re.match(r"^(-{3,}|\*{3,}|_{3,})$", current.strip()):
                break
            if _list_tag(current):
                break
            para_lines.append(current)
            i += 1

        if para_lines:
            # Preserve line breaks for indented lines (e.g., docstring Args/Returns).
            processed_lines = []
            for line in para_lines:
                is_indented = line.startswith((" ", "\t", "&nbsp;", "\u00a0"))
                if is_indented:
                    # Indented line: add line break before it.
                    processed_lines.append(f"<br>{_process_inline(line)}")
                else:
                    # Non-indented line: join with space.
                    if processed_lines and not processed_lines[-1].startswith("<br>"):
                        processed_lines.append(f" {_process_inline(line)}")
                    else:
                        processed_lines.append(_process_inline(line))
            result.append(f"<p>{''.join(processed_lines)}</p>")

    return "\n".join(result)


def _process_inline(text: str) -> str:
    """Process inline markdown elements."""
    # Escape HTML first
    text = html.escape(text)

    # Restore common HTML entities that were double-escaped.
    text = text.replace("&amp;nbsp;", "&nbsp;")

    # Inline code: `([^`]+)`
    #   `        - opening backtick
    #   ([^`]+)  - one or more non-backtick characters (capture: code)
    #   `        - closing backtick
    #
    # Code is literal: it's set aside - replaced by a placeholder - so the
    # patterns below never apply to it (e.g. `__init__` is not bold), and
    # it's put back at the end.
    codes: list[str] = []

    def set_code_aside(match: re.Match) -> str:
        codes.append(match.group(1))

        return f"\x00{len(codes) - 1}\x00"

    text = re.sub(r"`([^`]+)`", set_code_aside, text)

    # Links: \[([^\]]+)\]\(([^)]+)\)
    #   \[([^\]]+)\]  - [text] (capture: link text)
    #   \(([^)]+)\)   - (url) (capture: link url)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)

    # Bold: \*\*([^*]+)\*\*  or  __([^_]+)__
    #   \*\*([^*]+)\*\*  - **text** (capture: bold text)
    #   __([^_]+)__      - __text__ (capture: bold text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", text)

    # Italic: (?<!\w)\*([^*]+)\*(?!\w)  or  (?<!\w)_([^_]+)_(?!\w)
    #   (?<!\w)    - not preceded by a word character (avoid mid-word matches)
    #   \*([^*]+)\* - *text* (capture: italic text)
    #   _([^_]+)_   - _text_ (capture: italic text)
    #   (?!\w)     - not followed by a word character
    text = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"<em>\1</em>", text)

    # Handle markdown escape sequences.
    text = text.replace(r"\_", "_")
    text = text.replace(r"\*", "*")

    # Put inline code back.
    text = re.sub(
        r"\x00(\d+)\x00",
        lambda match: f"<code>{codes[int(match.group(1))]}</code>",
        text,
    )

    return text
