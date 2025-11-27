# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Smarts** is a Language Server Protocol (LSP) client for Sublime Text. It enables LSP features like go-to-definition, hover, diagnostics, formatting, and auto-completion within Sublime Text by communicating with language servers (e.g., Pyright, Ruff).

## Core Architecture

### Two-Layer Design

1. **smarts_client.py** - Low-level LSP client
   - `LanguageServerClient` class handles LSP communication
   - Manages subprocess for language server (stdin/stdout communication)
   - Three background threads: reader, writer, handler
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
3. **Runtime**: Event listeners (`on_activated`, `on_modified`) send LSP notifications (`textDocument/didOpen`, `didChange`)
4. **Shutdown**: Sends LSP `shutdown` request, then `exit` notification, terminates subprocess

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
- Requires Python ~3.8 (specified in pyproject.toml and .python-version)
- Find Python executable: `uv python find`
- Install Python version: `uv python install` (reads .python-version)

### Type Checking

- Configured via pyrightconfig.json
- Only checks smarts.py (not smarts_client.py)
- Custom type stubs in `typings/` directory for Sublime Text API (sublime.pyi, sublime_plugin.pyi)
- Run: `pyright` (if installed)

### Logging

Two separate loggers with configurable levels (in Smarts.sublime-settings):
- `logger.plugin.level` - Plugin-level events (default: INFO)
- `logger.client.level` - LSP client communication (default: INFO)

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

1. **Thread Safety**: `LanguageServerClient` uses `threading.Lock` for initialization and queues for message passing
2. **Diagnostics**: Stored in view settings under `PG_SMARTS_DIAGNOSTICS` key, augmented with URI to function as locations
3. **Position Encoding**: LSP uses 0-based line/character positions; Sublime uses 0-based points with UTF-16 encoding
4. **View Applicability**: A view is applicable if it has a file and its syntax matches a server's `applicable_to` list
5. **Multiple Servers**: Multiple servers can operate on the same view if applicable (e.g., Pyright + Ruff for Python)

### Common Patterns

**Finding applicable servers for a view:**
```python
smarts = applicable_smarts(view, method="textDocument/hover")
# or just the first:
smart = applicable_smart(view, method="textDocument/definition")
```

**Converting between Sublime and LSP positions:**
```python
lsp_position = to_lsp_position(view, point)
sublime_point = from_lsp_position(view, lsp_position)
```

**Sending LSP requests:**
```python
client.textDocument_definition(params, callback=on_response)
```

## Project-Specific Notes

- This is a work-in-progress plugin (see README warning)
- No test suite currently exists
- The plugin automatically initializes servers on `plugin_loaded` if defined in project
- Output panel (`Smarts`) shows server messages and errors
