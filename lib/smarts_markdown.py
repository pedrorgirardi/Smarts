import html
import re


def markdown_to_html(text: str) -> str:
    """Convert markdown string to HTML string.

    Handles:
    - Fenced code blocks (```) → <pre>
    - Inline code (`) → <code>
    - Links [text](url) → <a href="url">text</a>
    - Bold **text** → <strong>
    - Italic *text* → <em>
    - Headers # → <h1>, ## → <h2>, etc.
    - Paragraphs
    """
    lines = text.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.startswith("```"):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(html.escape(lines[i]))
                i += 1
            code_content = "<br>".join(code_lines)
            result.append(f"<pre>{code_content}</pre>")
            i += 1
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

        # Unordered list: ^[-*+]\s+(.+)$
        #   [-*+]  - dash, asterisk, or plus (list marker)
        #   \s+    - one or more spaces
        #   (.+)$  - rest of line (capture: item content)
        list_match = re.match(r"^[-*+]\s+(.+)$", line)
        if list_match:
            list_items = []
            current_item_lines = []
            while i < len(lines):
                current = lines[i]
                item_match = re.match(r"^[-*+]\s+(.+)$", current)
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
                else:
                    # Continuation line - append to current item
                    current_item_lines.append(current)
                    i += 1
            # Save final item
            if current_item_lines:
                item_text = " ".join(current_item_lines)
                list_items.append(f"<li>{_process_inline(item_text)}</li>")
            result.append(f"<ul>{''.join(list_items)}</ul>")
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
            if re.match(r"^[-*+]\s+", current):
                break
            para_lines.append(current)
            i += 1

        if para_lines:
            para_text = " ".join(para_lines)
            result.append(f"<p>{_process_inline(para_text)}</p>")

    return "\n".join(result)


def _process_inline(text: str) -> str:
    """Process inline markdown elements."""
    # Escape HTML first
    text = html.escape(text)

    # Inline code: `([^`]+)`
    #   `        - opening backtick
    #   ([^`]+)  - one or more non-backtick characters (capture: code)
    #   `        - closing backtick
    # Must be before other patterns to avoid conflicts.
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)

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

    return text
