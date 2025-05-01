> [!WARNING]
> Work in progress

**Smarts** is a [Language Server Protocol](https://microsoft.github.io/language-server-protocol/) client for [Sublime Text](https://www.sublimetext.com).

## Example Key Bindings

```jsonc
[
    // Go to Definition
    {
        "keys": [
            "ctrl+d"
        ],
        "command": "pg_smarts_goto_definition"
    },
    // Go to Reference
    {
        "keys": [
            "ctrl+u"
        ],
        "command": "pg_smarts_goto_reference"
    },
    // Go to Document Symbol
    {
        "keys": [
            "shift+super+o"
        ],
        "command": "pg_smarts_goto_document_symbol"
    },
    // Go to Document Diagnostic
    {
        "keys": [
            "shift+super+/"
        ],
        "command": "pg_smarts_goto_document_diagnostic"
    },
    // Format Document
    {
        "keys": [
            "ctrl+\\"
        ],
        "command": "pg_smarts_format_document"
    },
    // Select
    {
        "keys": [
            "ctrl+s",
            "s"
        ],
        "command": "pg_smarts_select"
    },
    // Jump: Up & Down
    {
        "keys": [
            "ctrl+alt+up"
        ],
        "command": "pg_smarts_jump",
        "args": {
            "movement": "back"
        }
    },
    {
        "keys": [
            "ctrl+alt+down"
        ],
        "command": "pg_smarts_jump",
        "args": {
            "movement": "forward"
        }
    },
    // Show Hover
    {
        "keys": [
            "ctrl+i"
        ],
        "command": "pg_smarts_show_hover"
    }
]
```

## Development

### [Project Python versions](https://docs.astral.sh/uv/concepts/python-versions/#project-python-versions)

By default `uv python install` will verify that a managed Python version is installed or install the latest version.

However, a project may include a `.python-version` file specifying a default Python version. If present, uv will install the Python version listed in the file.

### [Finding a Python executable](https://docs.astral.sh/uv/concepts/python-versions/#finding-a-python-executable)

To find a Python executable, use the `uv python find` command:

```
$ uv python find
```

By default, this will display the path to the first available Python executable.
