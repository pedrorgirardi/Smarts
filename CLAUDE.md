# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Smarts** is a Language Server Protocol (LSP) client for Sublime Text. It enables LSP features like go-to-definition, hover, diagnostics, formatting, and auto-completion within Sublime Text by communicating with language servers (e.g., Pyright, Ruff).

## Core Architecture

### Two-Layer Design

1. **smarts_client.py** - Low-level LSP client
   - `LanguageServerClient` class handles LSP communication
   - Manages subprocess for language server (stdin/stdout communication)
   - Background threads per server: reader, writer, handler, and stderr drain
   - The reader detects server exit (EOF); `_fail` handles every failure
   - Implements LSP protocol: JSON-RPC message serialization, request/response matching
   - Type definitions for LSP messages and structures (TypedDict classes)

2. **smarts.py** - Sublime Text plugin integration
   - Sublime plugin commands (inheriting from `sublime_plugin.TextCommand`, `WindowCommand`, etc.)
   - Event listeners (`PgSmartsTextListener`, `PgSmartsViewListener`, `PgSmartsListener`)
   - Manages multiple "Smarts" (language server instances) per window
   - Routes LSP notifications to UI updates (diagnostics, hover, completions)

### Global State Management

- `_SMARTS: List[PgSmart]` - Global list tracking all active language server instances
- Each `PgSmart` contains: UUID, window ID, server config, and `LanguageServerClient` instance
- Helper functions filter smarts by window, initialization state, and applicability

### Server Configuration

Servers are configured in two places:

1. **Smarts.sublime-settings** - Global server definitions in `servers` array:
   ```json
   {
       "servers": [
           {
               "name": "Pyright",
               "start": ["pyright-langserver", "--stdio"],
               "applicable_to": ["Packages/Python/Python.sublime-syntax"]
           }
       ]
   }
   ```

2. **Project files** (.sublime-project) - Per-project initialization:
   ```json
   {
       "Smarts": {
           "initialize": [
               {"name": "Pyright"},
               {"name": "Ruff", "rootPath": "./src"}
           ]
       }
   }
   ```

The `applicable_to` field determines which file syntaxes trigger a server. The `rootPath` in project initialization can be relative (resolved against project path) or absolute.

### Lifecycle

1. **Plugin load** (`plugin_loaded`): Initializes loggers and auto-starts servers defined in project
2. **Initialization** (`initialize_project_smarts`): Creates `LanguageServerClient` instances based on project config
3. **Runtime**: Event listeners send LSP notifications: `on_load` and `on_post_save` (`didOpen`, `didSave`), `on_text_changed` (`didChange`), `on_pre_close` (`didClose`). They're synchronous - on the main thread - so servers are told about a change before any request which depends on it
4. **Shutdown**: Sends LSP `shutdown` request, then `exit` notification, terminates subprocess
5. **Failure**: If a server fails (crash, I/O error, rejected or timed out initialization), the client answers pending requests with an error (`CLIENT_ERROR`) and calls `on_failure`; the plugin (`handle_failure`) removes the Smart and its diagnostics. A client is initialized once - to retry, create a new one

### Communication Flow

```
User Action (e.g., type in file)
    ↓
Event Listener (PgSmartsTextListener)
    ↓
Find applicable Smarts (by syntax + server capabilities)
    ↓
LanguageServerClient.textDocument_didChange()
    ↓
Message queued → Writer thread → Server stdin
    ↓
Server processes, sends response
    ↓
Reader thread → Receive queue → Handler thread
    ↓
Callbacks invoked (e.g., update diagnostics UI)
```

## Development

### Python Environment

- Uses `uv` for Python version management
- Requires Python 3.14 (specified in pyproject.toml and .python-version)
- Find Python executable: `uv python find`
- Install Python version: `uv python install` (reads .python-version)

### Type Checking

- Configured via pyrightconfig.json
- Checks `smarts.py`, `lib/` and `tests/`
- Uses the typed Sublime API modules shipped with the local app bundle at `/Applications/Sublime Text.app/Contents/MacOS/Lib/python314`
- Run: `pyright` (if installed)

### Testing

- Tests are in `tests/` directory using unittest
- `test_smarts_client.py` runs the client against fake servers - Python scripts sharing `FAKE_SERVER_IO`
- `test_smarts.py` tests plugin functions which don't need an editor; `fake_sublime.py` fakes Sublime's API so `smarts.py` can be imported
- Run tests: `bin/test`
- Run with verbose output: `bin/test -v`
- Run specific tests: `bin/test -k <pattern>`

### Logging

One logger, used by the plugin and its clients, with a configurable level (in Smarts.sublime-settings):
- `logger.plugin.level` (default: INFO)

View logs in Sublime Text console (View → Show Console).

### Key Commands

All commands are prefixed with `pg_smarts_`:
- `pg_smarts_initialize` - Start a language server
- `pg_smarts_shutdown` - Stop a language server
- `pg_smarts_status` - Show active servers and their state
- `pg_smarts_goto_definition` - Jump to symbol definition
- `pg_smarts_goto_reference` - Find references
- `pg_smarts_goto_document_symbol` - Navigate document symbols
- `pg_smarts_format_document` - Format current file
- `pg_smarts_show_hover` - Show hover information
- `pg_smarts_toggle_output_panel` - Show/hide output panel

### Important Implementation Details

1. **Thread Safety**: `LanguageServerClient` uses one `RLock` for its state and unbounded queues for message passing; never block on a queue while holding the lock
2. **Diagnostics**: Stored in the `_DIAGNOSTICS` dict, keyed by (window ID, path) then server UUID, augmented with URI to function as locations
3. **Per-view state**: Kept in Python dicts keyed by view ID (or as view regions), not in view settings
4. **Position Encoding**: LSP uses 0-based line/character positions; Sublime uses 0-based points with UTF-16 encoding
5. **View Applicability**: A view is applicable if it has a file and its syntax matches a server's `applicable_to` list
6. **Multiple Servers**: Multiple servers can operate on the same view if applicable (e.g., Pyright + Ruff for Python)

### Common Patterns

**Finding applicable servers for a view:**
```python
smarts = applicable_smarts(view, method="textDocument/hover")
# or just the first:
smart = applicable_smart(view, method="textDocument/definition")
```

**Converting between Sublime and LSP positions:**
```python
position_encoding = smart.position_encoding()
lsp_position = point_position(view, position_encoding, point)
sublime_point = position_point(view, position_encoding, lsp_position)
```

**Sending LSP requests:**
```python
client.textDocument_definition(params, callback=on_response)
```

## Project-Specific Notes

- This is a work-in-progress plugin (see README warning)
- The plugin automatically initializes servers on `plugin_loaded` if defined in project
- Output panel (`Smarts`) shows server messages and errors
