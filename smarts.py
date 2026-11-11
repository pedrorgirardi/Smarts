import html

import importlib
import json
import logging
import os
import pprint
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from itertools import groupby
from pathlib import Path
from typing import Any, Literal, TypedDict, cast
from urllib.parse import unquote, urlparse
from zipfile import ZipFile

import sublime
import sublime_plugin

from .lib import smarts_client, smarts_markdown

# Force reload of lib/ modules when Sublime reloads this plugin.
importlib.reload(smarts_client)
importlib.reload(smarts_markdown)


# -- Logging

logging_formatter = logging.Formatter(fmt="[{name} {threadName}] {message}", style="{")

# Handler to log on the Console.
console_logging_handler = logging.StreamHandler()
console_logging_handler.setFormatter(logging_formatter)

# Logger used to log 'everything-plugin'.
plugin_logger = logging.getLogger(__package__)
plugin_logger.propagate = False

# ---------------------------------------------------------------------------------------


# -- CONSTANTS

kSETTING_SERVERS = "servers"

kOUTPUT_PANEL_NAME = "Smarts - Output"

kOUTPUT_PANEL_MESSAGES_NAME = "Smarts - Messages"
kMESSAGES_PANEL_SYNTAX = "Packages/JSON/JSON.sublime-syntax"
kOUTPUT_PANEL_MESSAGES_NAME_PREFIXED = f"output.{kOUTPUT_PANEL_MESSAGES_NAME}"

kDIAGNOSTICS = "PG_SMARTS_DIAGNOSTICS"
kSMARTS_HIGHLIGHTS = "PG_SMARTS_HIGHLIGHTS"

kCOMPLETION_FLAGS = (
    sublime.INHIBIT_WORD_COMPLETIONS
    | sublime.INHIBIT_EXPLICIT_COMPLETIONS
    | sublime.INHIBIT_REORDER
    | sublime.DYNAMIC_COMPLETIONS
)

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#diagnosticSeverity
kDIAGNOSTIC_SEVERITY_ERROR = 1
kDIAGNOSTIC_SEVERITY_WARNING = 2
kDIAGNOSTIC_SEVERITY_INFORMATION = 3
kDIAGNOSTIC_SEVERITY_HINT = 4

# Severity: (name, scope, quick panel kind, quick panel kind letter).
kSEVERITY_STYLE: dict[int, tuple[str, str, sublime.KindId, str]] = {
    kDIAGNOSTIC_SEVERITY_ERROR: ("Error", "region.redish", sublime.KIND_ID_COLOR_REDISH, "E"),
    kDIAGNOSTIC_SEVERITY_WARNING: ("Warning", "region.orangish", sublime.KIND_ID_COLOR_ORANGISH, "W"),
    kDIAGNOSTIC_SEVERITY_INFORMATION: ("Info", "region.bluish", sublime.KIND_ID_COLOR_BLUISH, "I"),
    kDIAGNOSTIC_SEVERITY_HINT: ("Hint", "region.purplish", sublime.KIND_ID_COLOR_PURPLISH, "H"),
}

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#messageType
kMESSAGE_TYPE_NAME = {
    1: "Error",
    2: "Warning",
    3: "Info",
    4: "Log",
    5: "Debug",
}

# The kind of a completion entry
# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#completionItemKind
kCOMPLETION_ITEM_KIND = {
    1: sublime.KIND_ID_AMBIGUOUS,  # Text
    2: sublime.KIND_ID_FUNCTION,  # Method
    3: sublime.KIND_ID_FUNCTION,  # Function
    4: sublime.KIND_ID_FUNCTION,  # Constructor
    5: sublime.KIND_ID_VARIABLE,  # Field
    6: sublime.KIND_ID_VARIABLE,  # Variable
    7: sublime.KIND_ID_TYPE,  # Class
    8: sublime.KIND_ID_TYPE,  # Interface
    9: sublime.KIND_ID_NAMESPACE,  # Module
    10: sublime.KIND_ID_VARIABLE,  # Property
    11: sublime.KIND_ID_VARIABLE,  # Unit
    12: sublime.KIND_ID_VARIABLE,  # Value
    13: sublime.KIND_ID_TYPE,  # Enum
    14: sublime.KIND_ID_KEYWORD,  # Keyword
    15: sublime.KIND_ID_SNIPPET,  # Snippet
    16: sublime.KIND_ID_VARIABLE,  # Color
    17: sublime.KIND_ID_NAVIGATION,  # File
    18: sublime.KIND_ID_NAVIGATION,  # Reference
    19: sublime.KIND_ID_NAVIGATION,  # Folder
    20: sublime.KIND_ID_VARIABLE,  # EnumMember
    21: sublime.KIND_ID_VARIABLE,  # Constant
    22: sublime.KIND_ID_TYPE,  # Struct
    23: sublime.KIND_ID_FUNCTION,  # Event
    24: sublime.KIND_ID_KEYWORD,  # Operator
    25: sublime.KIND_ID_TYPE,  # TypeParameter
}

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#symbolKind
kSYMBOL_KIND_NAME = {
    1: "File",
    2: "Module",
    3: "Namespace",
    4: "Package",
    5: "Class",
    6: "Method",
    7: "Property",
    8: "Field",
    9: "Constructor",
    10: "Enum",
    11: "Interface",
    12: "Function",
    13: "Variable",
    14: "Constant",
    15: "String",
    16: "Number",
    17: "Boolean",
    18: "Array",
    19: "Object",
    20: "Key",
    21: "Null",
    22: "EnumMember",
    23: "Struct",
    24: "Event",
    25: "Operator",
    26: "TypeParameter",
}

kMINIHTML_STYLES = """
.rounded {
    border-radius: 0.25rem;
}

.rounded-lg {
    border-radius: 0.75rem;
}

.m-0 {
    margin: 0px;
}

.p-0 {
    padding: 0px;
}

.p-2 {
    padding: 0.5rem;
}

.p-3 {
    padding: 0.75rem;
}

.bg-accent {
    background-color: var(--accent);
}

.bg-background-50 {
    background-color: color(var(--background) alpha(0.50));
}

.font-bold {
    font-weight: bold;
}

.text-foreground {
    color: var(--foreground);
}

.text-pinkish {
    color: var(--pinkish);
}

.text-background-50 {
    color: color(var(--background) alpha(0.50));
}

.text-foreground-07 {
    color: color(var(--foreground) alpha(0.7));
}

.text-accent {
    color: var(--accent);
}

.text-xs {
    font-size: 0.75rem;
    line-height: 1rem;
}

.text-sm {
    font-size: 0.875rem;
    line-height: 1.25rem;
}
"""


# ---------------------------------------------------------------------------------------


class PgSmartsDiagnostic(smarts_client.LSPDiagnostic):
    """
    LSPDiagnostic with URI.

    Including URI to a Diagnostic make it equivalent to a Location - `uri` and `range`.
    (Anything that works with a Location also works with this Diagnostic.)
    """

    uri: str
    position_encoding: smarts_client.LSPPositionEncoding

    # Name of the server which published the diagnostic.
    server: str


class PgSmartsServerConfig(TypedDict):
    name: str
    start: list[str]
    applicable_to: list[str]


class PgSmartsInitializeData(TypedDict, total=False):
    name: str
    rootPath: str  # Optional.


class PgSmartsProjectData(TypedDict):
    initialize: list[PgSmartsInitializeData]


class PgSmart:
    def __init__(
        self,
        uuid: str,
        window: int,
        config: PgSmartsServerConfig,
        client: smarts_client.LanguageServerClient,
    ):
        self.uuid = uuid
        self.window = window
        self.config = config
        self.client = client

    def position_encoding(self) -> smarts_client.LSPPositionEncoding:
        return self.client.position_encoding() or "utf-16"


# ---------------------------------------------------------------------------------------


# -- Global Variables

_SMARTS: list[PgSmart] = []
_SMARTS_LOCK = threading.Lock()

# Tracks view ID when document symbol quick panel is open.
# Used to close the quick panel if the user closes the view (e.g., cmd+w).
_DOCUMENT_SYMBOL_VIEW_ID: int | None = None

# Diagnostics published by Language Servers.
# Keyed by (window ID, file path), then by Smart UUID.
#
# Each server owns its entry: a publish replaces only that server's diagnostics,
# so servers never overwrite each other. Every client calls the publish handler
# from its own handler thread, hence the lock. Entries are replaced, never mutated
# in place.
_DIAGNOSTICS: dict[tuple[int, str], dict[str, list[PgSmartsDiagnostic]]] = {}
_DIAGNOSTICS_LOCK = threading.Lock()

# Pending diagnostics presentation per view, keyed by view ID.
# Used to defer diagnostics rendering while actively editing.
# Only accessed on the main thread.
_DIAGNOSTICS_PENDING: dict[int, object] = {}

# Time of the last modification per view, keyed by view ID.
# Used by the diagnostics and highlights cool-down periods.
# Written on the main thread; Entries are removed when the view is closed.
_LAST_MODIFIED: dict[int, float] = {}

# URI of the document of a buffer in a window, keyed by (window ID, buffer ID),
# as of the last time servers were notified that it's open; See `document_open`.
# Used to close the document when it's saved as another file.
# Only accessed on the main thread; Entries are removed when the document is closed.
_DOCUMENT_URI: dict[tuple[int, int], str] = {}

# Syntax per code fence language, e.g. "python"; See `language_syntax`.
_LANGUAGE_SYNTAX: dict[str, sublime.Syntax] = {}


# ---------------------------------------------------------------------------------------


## -- API


def settings() -> sublime.Settings:
    return sublime.load_settings("Smarts.sublime-settings")


def smarts_project_data(
    window: sublime.Window,
) -> PgSmartsProjectData | None:
    if project_data_ := window.project_data():
        return project_data_.get("Smarts")

    return None


def setting(window: sublime.Window, k: str, not_found: Any):
    """
    Get setting k from project's data or Smarts.sublime-settings.

    Returns not_found if setting k is is not set.
    """
    project_data = smarts_project_data(window) or {}

    if k in project_data:
        return project_data[k]

    return settings().get(k, not_found)


def window_project_path(window: sublime.Window) -> Path | None:
    if project_path := window.extract_variables().get("project_path"):
        return Path(project_path)

    return None


def available_servers() -> list[PgSmartsServerConfig]:
    return settings().get(kSETTING_SERVERS, [])


def remove_smarts(uuids: set[str]):
    plugin_logger.debug(f"Remove Smarts {uuids}")

    global _SMARTS
    with _SMARTS_LOCK:
        _SMARTS = [smart for smart in _SMARTS if smart.uuid not in uuids]

    for window_id, path in remove_server_diagnostics(uuids):
        if window := find_window(window_id):
            if view := window.find_open_file(path):
                present_diagnostics(view, view_diagnostics(view))


def find_smart(uuid: str) -> PgSmart | None:
    with _SMARTS_LOCK:
        for smart in _SMARTS:
            if smart.uuid == uuid:
                return smart

    return None


def find_window(id: int) -> sublime.Window | None:
    for window in sublime.windows():
        if window.id() == id:
            return window

    return None


def window_smarts(window: sublime.Window) -> list[PgSmart]:
    """
    Returns Smarts associated with `window`.
    """
    with _SMARTS_LOCK:
        return [smart for smart in _SMARTS if smart.window == window.id()]


def window_running_smarts(window: sublime.Window) -> list[PgSmart]:
    """
    Returns Smarts associated with `window` which are not shutdown.
    """
    return [
        smart
        for smart in window_smarts(window)
        if not smart.client.is_server_shutdown()
    ]


def window_initialized_smarts(window: sublime.Window) -> list[PgSmart]:
    """
    Returns Smarts associated with `window` which are initialized.
    """
    # (An initialized server is not shutdown.)
    return [
        smart for smart in window_smarts(window) if smart.client.is_server_initialized()
    ]


def set_server_diagnostics(
    window_id: int,
    path: str,
    smart_uuid: str,
    diagnostics: list[PgSmartsDiagnostic],
) -> bool:
    """
    Replace diagnostics published by `smart_uuid` for `path`.

    Diagnostics of other servers are kept as is.

    Returns False if diagnostics didn't change.
    """
    key = (window_id, path)

    with _DIAGNOSTICS_LOCK:
        server_diagnostics = _DIAGNOSTICS.get(key, {})

        if server_diagnostics.get(smart_uuid, []) == diagnostics:
            return False

        server_diagnostics = {
            uuid: uuid_diagnostics
            for uuid, uuid_diagnostics in server_diagnostics.items()
            if uuid != smart_uuid
        }

        if diagnostics:
            server_diagnostics[smart_uuid] = diagnostics

        if server_diagnostics:
            _DIAGNOSTICS[key] = server_diagnostics
        else:
            _DIAGNOSTICS.pop(key, None)

        return True


def remove_server_diagnostics(uuids: set[str]) -> set[tuple[int, str]]:
    """
    Remove diagnostics published by servers `uuids`.

    Returns keys - (window ID, path) - of affected documents.
    """
    affected = set()

    with _DIAGNOSTICS_LOCK:
        for key, server_diagnostics in list(_DIAGNOSTICS.items()):
            if not uuids & server_diagnostics.keys():
                continue

            affected.add(key)

            server_diagnostics = {
                uuid: uuid_diagnostics
                for uuid, uuid_diagnostics in server_diagnostics.items()
                if uuid not in uuids
            }

            if server_diagnostics:
                _DIAGNOSTICS[key] = server_diagnostics
            else:
                del _DIAGNOSTICS[key]

    return affected


def path_diagnostics(window_id: int, path: str) -> list[PgSmartsDiagnostic]:
    """
    Returns diagnostics of all servers for `path`.
    """
    with _DIAGNOSTICS_LOCK:
        server_diagnostics = _DIAGNOSTICS.get((window_id, path), {})

        return [
            diagnostic
            for diagnostics in server_diagnostics.values()
            for diagnostic in diagnostics
        ]


def view_diagnostics(view: sublime.View) -> list[PgSmartsDiagnostic]:
    window = view.window()
    file_name = view.file_name()

    if window is None or file_name is None:
        return []

    return path_diagnostics(window.id(), file_name)


def window_diagnostics(window: sublime.Window) -> list[PgSmartsDiagnostic]:
    """
    Returns diagnostics of all servers for all documents in `window`,
    including documents which are not open.
    """
    window_id = window.id()

    with _DIAGNOSTICS_LOCK:
        return [
            diagnostic
            for (id, _), server_diagnostics in _DIAGNOSTICS.items()
            if id == window_id
            for diagnostics in server_diagnostics.values()
            for diagnostic in diagnostics
        ]


def shutdown_smarts(window: sublime.Window):
    shutdown_uuids = set()

    for smart in window_running_smarts(window):
        smart.client.shutdown()

        shutdown_uuids.add(smart.uuid)

    remove_smarts(shutdown_uuids)


def initialize_project_smarts(window: sublime.Window):
    """
    Initialize Language Servers configured in a Sublime Project.
    """
    if project_data_ := smarts_project_data(window):
        # It's expected a list of server (dict) with 'name', and 'rootPath' optionally - 'rootPath' can be a relative.
        for initialize_data in project_data_.get("initialize", []):
            name = initialize_data.get("name")

            rootPath = initialize_data.get("rootPath")

            if rootPath is None:
                folders = window.folders()

                if not folders:
                    plugin_logger.error(
                        f"Can't initialize {name}: no 'rootPath', and no open folder"
                    )
                    continue

                rootPath = folders[0]

            rootPath = Path(rootPath)

            project_path = window_project_path(window)

            if not rootPath.is_absolute() and project_path is not None:
                rootPath = (project_path / rootPath).resolve()

            # A relative path can't be made into the root URI.
            if not rootPath.is_absolute():
                plugin_logger.error(
                    f"Can't initialize {name}: 'rootPath' {rootPath} is relative, "
                    "and the window has no project path"
                )
                continue

            window.run_command(
                "pg_smarts_initialize",
                {
                    "server": name,
                    "rootPath": rootPath.as_posix(),
                },
            )


def view_syntax(view: sublime.View) -> str:
    """
    Returns a sublime-syntax for view.

    A syntax might be something like "Packages/Python/Python.sublime-syntax".
    """
    return view.settings().get("syntax")


def applicable_smarts(view: sublime.View, method: str) -> list[PgSmart]:
    """
    Returns Smarts applicable to `view`.
    """
    window = view.window()

    if window is None or view.file_name() is None:
        return []

    # Called on every keystroke, selection change, and hover:
    # read the view's syntax once, not once per Smart.
    syntax = view_syntax(view)

    return [
        smart
        for smart in window_initialized_smarts(window)
        if syntax in smart.config.get("applicable_to", [])
        and smart.client.support_method(method)
    ]


def applicable_smart(view: sublime.View, method: str) -> PgSmart | None:
    """
    Returns the first Smart applicable to `view`, or None.
    """
    if applicable := applicable_smarts(view, method):
        return applicable[0]

    return None


def language_syntax(language: str) -> sublime.Syntax:
    """
    Returns the syntax named `language` (case-insensitive), or Plain Text.

    Used to highlight code fences in hover popups. Lookups are cached:
    scanning every syntax for each code fence of each hover is slow.
    (The cache is cleared when the plugin is reloaded.)
    """
    key = language.casefold()

    if (syntax := _LANGUAGE_SYNTAX.get(key)) is None:
        syntax = next(
            (s for s in sublime.list_syntaxes() if key and s.name.casefold() == key),
            None,
        ) or sublime.find_syntax_by_name("Plain Text")[0]

        _LANGUAGE_SYNTAX[key] = syntax

    return syntax


def text_to_html(s: str) -> str:
    return (
        html.escape(s)
        .replace("\n", "<br/>")
        .replace("\t", "&nbsp;" * 4)
        .replace(" ", "&nbsp;")
    )


def panel(window: sublime.Window, name: str, syntax: str | None = None) -> sublime.View:
    """
    Returns output panel `name` of `window` - created if it doesn't exist.
    """
    if panel_view := window.find_output_panel(name):
        return panel_view

    panel_view = window.create_output_panel(name)

    panel_settings = panel_view.settings()
    panel_settings.set("gutter", False)
    panel_settings.set("auto_indent", False)
    panel_settings.set("translate_tabs_to_spaces", False)
    panel_settings.set("smart_indent", False)
    panel_settings.set("indent_to_bracket", False)
    panel_settings.set("highlight_line", False)
    panel_settings.set("line_numbers", False)
    panel_settings.set("scroll_past_end", False)

    if syntax:
        panel_view.assign_syntax(syntax)

    return panel_view


def show_panel(window: sublime.Window, name: str):
    window.run_command("show_panel", {"panel": f"output.{name}"})


def toggle_panel(window: sublime.Window, name: str, syntax: str | None = None):
    if window.active_panel() == f"output.{name}":
        window.run_command("hide_panel", {"panel": f"output.{name}"})
    else:
        # Create the panel if it doesn't exist.
        panel(window, name, syntax)
        show_panel(window, name)


def clear_panel(window: sublime.Window, name: str):
    panel_view = panel(window, name)
    panel_view.run_command("select_all")
    panel_view.run_command("left_delete")


def output_panel(window: sublime.Window) -> sublime.View:
    return panel(window, kOUTPUT_PANEL_NAME)


def messages_panel(window: sublime.Window) -> sublime.View:
    return panel(window, kOUTPUT_PANEL_MESSAGES_NAME, kMESSAGES_PANEL_SYNTAX)


def panel_log(window: sublime.Window, text: str, show=False):
    panel_view = output_panel(window)
    panel_view.run_command("insert", {"characters": text})

    if show:
        show_panel(window, kOUTPUT_PANEL_NAME)


def panel_log_error(
    window: sublime.Window,
    error: smarts_client.LSPResponseError,
    show=True,
):
    panel_log(
        window,
        f"Error: {error.get('code')} {error.get('message')} {error.get('data')}\n",
        show=show,
    )


def panel_error_logger(
    view: sublime.View,
) -> Callable[[smarts_client.LSPResponseError], None]:
    """
    Returns a request error callback which logs the error in the Output panel
    of the window of `view` - if the view is still in a window.
    """

    def on_error(error: smarts_client.LSPResponseError):
        if window := view.window():
            panel_log_error(window, error)

    return on_error


def messages_panel_insert(window: sublime.Window, label: str, message: Any):
    """
    Append `message` to the Messages panel if the panel is visible.

    Serializing is skipped while the panel is hidden: messages can be large
    (e.g. a full document sync) and are exchanged on every keystroke.
    """
    if window.active_panel() == kOUTPUT_PANEL_MESSAGES_NAME_PREFIXED:
        text = f"// {label}:\n{json.dumps(message, indent=2)}\n"
        messages_panel(window).run_command("append", {"characters": text})


def show_signature_help_popup(
    view: sublime.View,
    smart: PgSmart,
    result: smarts_client.LSPSignatureHelp,
):
    # The result of a signature help request.
    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#signatureHelp

    signatures = result.get("signatures", [])

    if not signatures:
        return

    active_signature_index = result.get("activeSignature")
    if active_signature_index is None:
        active_signature_index = 0

    # Clamp active signature index to valid range
    if active_signature_index < 0 or active_signature_index >= len(signatures):
        active_signature_index = 0

    active_signature = signatures[active_signature_index]

    # Active parameter can be at the top level or inside the active signature
    # Per LSP spec, SignatureInformation.activeParameter is deprecated, but some servers still use it
    active_parameter_index = result.get("activeParameter")
    if active_parameter_index is None:
        active_parameter_index = active_signature.get("activeParameter")

    popup_content = []

    # Add signature label
    signature_label = active_signature.get("label", "")

    # If active parameter is specified and parameters are available, highlight it
    parameters = active_signature.get("parameters", [])

    if active_parameter_index is not None and parameters:
        # Clamp active parameter index to valid range
        if active_parameter_index < 0:
            active_parameter_index = 0
        elif active_parameter_index >= len(parameters):
            active_parameter_index = len(parameters) - 1

        # After clamping, index is guaranteed to be valid
        param_label = parameters[active_parameter_index].get("label")

        # Parameter label can be a string or [start, end] offsets
        if isinstance(param_label, list) and len(param_label) == 2:
            start, end = param_label
            # Highlight the active parameter in the signature
            highlighted_label = (
                text_to_html(signature_label[:start])
                + '<b class="text-foreground">'
                + text_to_html(signature_label[start:end])
                + "</b>"
                + text_to_html(signature_label[end:])
            )
            popup_content.append(highlighted_label)

        elif isinstance(param_label, str):
            # Find the parameter in the signature and highlight it
            param_start = signature_label.find(param_label)
            if param_start >= 0:
                param_end = param_start + len(param_label)
                highlighted_label = (
                    text_to_html(signature_label[:param_start])
                    + '<b class="text-foreground">'
                    + text_to_html(signature_label[param_start:param_end])
                    + "</b>"
                    + text_to_html(signature_label[param_end:])
                )
                popup_content.append(highlighted_label)

            else:
                popup_content.append(text_to_html(signature_label))

        else:
            popup_content.append(text_to_html(signature_label))
    else:
        popup_content.append(text_to_html(signature_label))

    minihtml = f"""
    <style>
        {kMINIHTML_STYLES}
    </style>
    <body>
        <div class='rounded-lg p-3 bg-background-50 text-foreground-07 text-sm'>{"<br />".join(popup_content)}</div>

        <br />

        <span class='text-sm text-pinkish font-bold'>{html.escape(smart.client._name)}</span>
    </body>
    """

    view.show_popup(minihtml, location=-1, max_width=1200)


def diagnostic_severity(diagnostic: smarts_client.LSPDiagnostic) -> int:
    """
    Returns the severity of `diagnostic`.

    To avoid interpretation mismatches when a server is used with different
    clients it is highly recommended that servers always provide a severity value.
    If omitted, it's recommended for the client to interpret it as an Error severity.
    """
    return diagnostic.get("severity") or kDIAGNOSTIC_SEVERITY_ERROR


def severity_name(severity: int) -> str:
    if style := kSEVERITY_STYLE.get(severity):
        return style[0]

    return f"Unknown {severity}"


def severity_scope(severity: int) -> str:
    if style := kSEVERITY_STYLE.get(severity):
        return style[1]

    return "invalid"


def severity_annotation_color(view: sublime.View, severity: int) -> str:
    scope = severity_scope(severity)

    style = view.style_for_scope(scope)

    return style.get("foreground", "#ff0000")


def severity_kind(severity: int) -> tuple[sublime.KindId, str, str]:
    if style := kSEVERITY_STYLE.get(severity):
        return (style[2], style[3], style[3])

    return (sublime.KIND_ID_AMBIGUOUS, "", "")


def point_position(
    view: sublime.View,
    position_encoding: smarts_client.LSPPositionEncoding,
    point: int,
) -> smarts_client.LSPPosition:
    """
    Convert a Sublime point to an LSP Position.

    UTF-32 isn't advertised to servers, so anything but UTF-8 is UTF-16.
    """
    if position_encoding == "utf-8":
        row, col = view.rowcol_utf8(point)
    else:
        row, col = view.rowcol_utf16(point)

    return {
        "line": int(row),
        "character": int(col),
    }


def position_point(
    view: sublime.View,
    position_encoding: smarts_client.LSPPositionEncoding,
    position: smarts_client.LSPPosition,
) -> int:
    """
    Convert an LSP Position to a Sublime point; See `point_position`.
    """
    if position_encoding == "utf-8":
        return view.text_point_utf8(
            position["line"], position["character"], clamp_column=True
        )

    return view.text_point_utf16(
        position["line"], position["character"], clamp_column=True
    )


def region_range(
    view: sublime.View,
    position_encoding: smarts_client.LSPPositionEncoding,
    region: sublime.Region,
) -> smarts_client.LSPRange:
    return {
        "start": point_position(view, position_encoding, region.begin()),
        "end": point_position(view, position_encoding, region.end()),
    }


def range_region(
    view: sublime.View,
    position_encoding: smarts_client.LSPPositionEncoding,
    range: smarts_client.LSPRange,
    inverted=False,
) -> sublime.Region:
    """
    Convert an LSP Range to a Sublime Region.

    Args:
        view: The view to convert positions for.
        position_encoding: LSP position encoding ("utf-8" or "utf-16").
        range: LSP Range with start and end positions.
        inverted: If True, swap start/end so cursor is at the beginning of selection.

    Returns:
        Sublime Region corresponding to the LSP range.
    """
    a = position_point(view, position_encoding, range["start"])
    b = position_point(view, position_encoding, range["end"])

    if inverted:
        return sublime.Region(b, a)

    return sublime.Region(a, b)


def diagnostic_origin(diagnostic: PgSmartsDiagnostic) -> str:
    """
    Returns where `diagnostic` comes from: the server name, and its source
    if it's different - e.g. a server which aggregates linters.
    """
    server = diagnostic["server"]
    source = diagnostic.get("source")

    if source and source.casefold() != server.casefold():
        return f"{server} ({source})"

    return server


def diagnostic_quick_panel_item(data: PgSmartsDiagnostic) -> sublime.QuickPanelItem:
    path = uri_to_path(data["uri"])
    start_line = data["range"]["start"]["line"] + 1
    start_character = data["range"]["start"]["character"] + 1

    origin = diagnostic_origin(data)
    code = data.get("code")

    severity = diagnostic_severity(data)

    return sublime.QuickPanelItem(
        f"{severity_name(severity)}: {data['message']}",
        kind=severity_kind(severity),
        annotation=" · ".join(str(s) for s in (origin, code) if s),
        details=f"{path}:{start_line}:{start_character}",
    )


def location_quick_panel_item(
    window: sublime.Window,
    location: smarts_client.LSPLocation,
) -> sublime.QuickPanelItem:
    start_line = location["range"]["start"]["line"] + 1
    start_character = location["range"]["start"]["character"] + 1

    path = uri_to_path(location["uri"])

    if folders := window.folders():
        path = os.path.relpath(path, folders[0])

    return sublime.QuickPanelItem(
        str(path),
        annotation=f"{start_line}:{start_character}",
    )


def flatten_document_symbols(symbols: list, container: str | None = None) -> list:
    """
    Returns `symbols` and their descendants as a flat list, in document order.

    DocumentSymbols are hierarchical - e.g. the methods of a class are its
    `children`. Each descendant gets a `containerName`, the qualified name of
    its parent, like a SymbolInformation - which is flat already - might have.
    """
    flat = []

    for symbol in symbols:
        if container and not symbol.get("containerName"):
            symbol = {**symbol, "containerName": container}

        flat.append(symbol)

        if children := symbol.get("children"):
            name = symbol["name"]

            flat.extend(
                flatten_document_symbols(
                    children,
                    container=f"{container}.{name}" if container else name,
                )
            )

    return flat


def document_symbol_quick_panel_item(data) -> sublime.QuickPanelItem:
    line = None
    character = None

    if location := data.get("location"):
        line = location["range"]["start"]["line"] + 1
        character = location["range"]["start"]["character"] + 1
    else:
        line = data["selectionRange"]["start"]["line"] + 1
        character = data["selectionRange"]["start"]["character"] + 1

    details = (
        f"{data['containerName']}.{data['name']}"
        if data.get("containerName")
        else f"{data['name']}"
    )

    return sublime.QuickPanelItem(
        details,
        annotation=f"{line}:{character}",
    )


def workspace_symbol_quick_panel_item(data) -> sublime.QuickPanelItem:
    path = uri_to_path(data["location"]["uri"])
    start_line = data["location"]["range"]["start"]["line"] + 1
    start_character = data["location"]["range"]["start"]["character"] + 1

    return sublime.QuickPanelItem(
        data["name"],
        annotation=kSYMBOL_KIND_NAME.get(data["kind"], ""),
        details=f"{path}:{start_line}:{start_character}",
    )


def path_to_uri(path: str) -> str:
    return Path(path).as_uri()


def uri_to_path(uri: str) -> str:
    return unquote(urlparse(uri).path)


def view_file_name_uri(view: sublime.View) -> str:
    if file_name := view.file_name():
        return path_to_uri(file_name)
    else:
        return f"untitled://{view.id()}"


def view_text_document_item(view: sublime.View) -> smarts_client.LSPTextDocumentItem:
    """
    An item to transfer a text document from the client to the server.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentItem
    """
    return {
        "uri": view_file_name_uri(view),
        "languageId": view_languageId(view),
        "version": view.change_count(),
        "text": view.substr(sublime.Region(0, view.size())),
    }


def view_clones(view: sublime.View) -> list[sublime.View]:
    """
    Returns the other views of the buffer of `view` in its window.
    """
    buffer = view.buffer()
    window = view.window()

    if buffer is None or window is None:
        return []

    return [
        clone
        for clone in buffer.views()
        if clone.id() != view.id()
        and (clone_window := clone.window()) is not None
        and clone_window.id() == window.id()
    ]


def document_open(view: sublime.View):
    """
    Notify applicable servers that the document of `view` is open -
    unless they were notified already.
    """
    window = view.window()

    if window is None or view.file_name() is None:
        return

    uri = view_file_name_uri(view)

    # The document was saved as another file: for servers it's another
    # document, and the one they know is no longer open.
    key = (window.id(), view.buffer_id())

    previous_uri = _DOCUMENT_URI.get(key)

    if previous_uri and previous_uri != uri:
        for smart in window_initialized_smarts(window):
            # (A no-op for servers which don't have the document open.)
            smart.client.textDocument_didClose({
                "textDocument": {"uri": previous_uri},
            })

    _DOCUMENT_URI[key] = uri

    for smart in applicable_smarts(view, method="textDocument/didOpen"):
        # Checked first: the text of the document isn't read for nothing.
        if not smart.client.is_document_open(uri):
            smart.client.textDocument_didOpen({
                "textDocument": view_text_document_item(view),
            })


def document_did_save(view: sublime.View):
    """
    Notify applicable servers that the document of `view` was saved.
    """
    text: str | None = None

    for smart in applicable_smarts(view, method="textDocument/didSave"):
        params: smarts_client.LSPDidSaveTextDocumentParams = {
            "textDocument": view_textDocumentIdentifier(view),
        }

        save = smart.client.text_document_sync()["save"]

        # The text is included only if the server asked for it.
        if isinstance(save, dict) and save.get("includeText"):
            if text is None:
                text = view.substr(sublime.Region(0, view.size()))

            params["text"] = text

        smart.client.textDocument_didSave(params)


def document_did_change_full(
    view: sublime.View,
    smart: PgSmart,
):
    """
    Send a full document sync for the current view contents.
    """
    if file_name := view.file_name():
        params: smarts_client.LSPDidChangeTextDocumentParams = {
            "textDocument": {
                "uri": path_to_uri(file_name),
                "version": view.change_count(),
            },
            "contentChanges": [
                cast(
                    smarts_client.LSPTextDocumentContentChangeEvent,
                    {
                        "text": view.substr(sublime.Region(0, view.size())),
                    },
                )
            ],
        }

        smart.client.textDocument_didChange(params)


def document_resync(view: sublime.View):
    """
    Re-synchronize a view with all applicable servers after a reload/revert.

    External file modifications do not produce the incremental edit stream used
    while typing, so servers are sent the full text - which is a valid change
    for servers which sync incrementally too: a change without a range is
    the full content of the document.
    """
    for smart in applicable_smarts(view, method="textDocument/didChange"):
        document_did_change_full(view, smart)


def open_location_jar(
    window: sublime.Window,
    position_encoding: smarts_client.LSPPositionEncoding,
    location: smarts_client.LSPLocation,
    caret: Literal["start", "end"],
    flags: sublime.NewFileFlags,
):
    """
    Open JAR `fname` and call `f` with the path of the temporary file.
    """
    fname = uri_to_path(location["uri"])

    dep_jar, dep_filepath = fname.split("::")

    with ZipFile(dep_jar) as jar, jar.open(dep_filepath) as jar_file:
        tmp_path = os.path.join(tempfile.gettempdir(), dep_filepath)

        # Create all parent directories of the temporary file:
        os.makedirs(os.path.dirname(tmp_path), exist_ok=True)

        with open(tmp_path, "w") as tmp_file:
            tmp_file.write(jar_file.read().decode())

        new_location = {
            "uri": path_to_uri(tmp_file.name),
            "range": location["range"],
        }

        open_location(
            window,
            position_encoding=position_encoding,
            location=cast(smarts_client.LSPLocation, new_location),
            caret=caret,
            flags=flags,
        )


def open_location(
    window: sublime.Window,
    position_encoding: smarts_client.LSPPositionEncoding,
    location: smarts_client.LSPLocation,
    caret: Literal["start", "end"] | None = None,
    flags: sublime.NewFileFlags = sublime.NewFileFlags.NONE,
):
    """
    Open a file at an LSP location and set the selection.

    Used by goto commands (definition, reference, diagnostic) to navigate to locations.

    Args:
        window: The Sublime window to open the file in.
        position_encoding: LSP position encoding for interpreting character offsets.
        location: LSP Location with URI and range.
        caret: Place an empty cursor at the range start or end. Select the range
            when omitted.
        flags: Sublime flags (e.g., sublime.TRANSIENT for preview during quick panel).
    """
    file_path = uri_to_path(location["uri"])

    if ".jar:" in file_path:
        open_location_jar(
            window,
            position_encoding,
            location,
            caret or "start",
            flags,
        )
    elif caret:
        # Use ENCODED_POSITION to trigger proper selection events (e.g., word highlight).
        position = location["range"][caret]
        line = position["line"] + 1  # LSP is 0-based, Sublime is 1-based
        col = position["character"] + 1
        window.open_file(f"{file_path}:{line}:{col}", flags | sublime.ENCODED_POSITION)
    else:
        view = window.open_file(file_path, flags)

        def select_range():
            if view.is_loading():
                sublime.set_timeout(select_range, 10)
            else:
                show_at_center_region = range_region(
                    view,
                    position_encoding,
                    location["range"],
                    inverted=True,
                )

                view.sel().clear()
                view.sel().add(show_at_center_region)
                view.show_at_center(show_at_center_region)

        select_range()


def capture_view(view: sublime.View) -> Callable[[], None]:
    regions = [[region.a, region.b] for region in view.sel()]
    viewport_position = view.viewport_position()
    highlight_regions = view_highlights(view)

    def restore():
        view.run_command(
            "pg_smarts_set_selection",
            {
                "regions": regions,
            },
        )

        view.set_viewport_position(viewport_position, True)

        if highlight_regions:
            set_view_highlights(view, highlight_regions)

        if window := view.window():
            window.focus_view(view)

    return restore


def view_highlights(view: sublime.View) -> list[sublime.Region]:
    """
    Returns regions of references highlighted in `view`, in document order.

    Sublime keeps regions in sync with edits, so they're always valid for the
    current buffer - unlike the LSP ranges they were created from.
    """
    return sorted(view.get_regions(kSMARTS_HIGHLIGHTS), key=lambda r: r.begin())


def set_view_highlights(view: sublime.View, regions: list[sublime.Region]):
    highlight_references = False

    if window := view.window():
        highlight_references = setting(window, "editor.highlight_references", False)

    # Regions are kept even if hidden: Select and Jump use them.
    view.add_regions(
        kSMARTS_HIGHLIGHTS,
        regions,
        scope="region.yellowish",
        icon="",
        flags=sublime.DRAW_NO_FILL if highlight_references else sublime.HIDDEN,
    )


def erase_view_highlights(view: sublime.View):
    view.erase_regions(kSMARTS_HIGHLIGHTS)


def capture_viewport_position(view: sublime.View) -> Callable[[], None]:
    viewport_position = view.viewport_position()

    def restore():
        view.set_viewport_position(viewport_position, True)

    return restore


def apply_workspace_edit(
    window: sublime.Window,
    position_encoding: smarts_client.LSPPositionEncoding,
    workspace_edit: smarts_client.LSPWorkspaceEdit,
):
    """
    Apply a WorkspaceEdit to the workspace.

    Handles both 'changes' and 'documentChanges' formats.

    Must be called on the main thread.
    """
    # documentChanges (preferred) or changes (older format).
    if document_changes := workspace_edit.get("documentChanges"):
        file_edits = [
            (doc_edit["textDocument"]["uri"], doc_edit["edits"])
            for doc_edit in document_changes
        ]
    else:
        file_edits = list((workspace_edit.get("changes") or {}).items())

    for uri, edits in file_edits:
        apply_file_edits(window, position_encoding, uri_to_path(uri), edits)


def apply_file_edits(
    window: sublime.Window,
    position_encoding: smarts_client.LSPPositionEncoding,
    path: str,
    edits: list[smarts_client.LSPTextEdit],
):
    """
    Apply `edits` to the file at `path`, opening it if it's not open.

    Edits are applied once the file is loaded; Waiting is done by
    rescheduling on the main thread - never by blocking a thread.

    Must be called on the main thread.
    """
    view = window.find_open_file(path) or window.open_file(path)

    def apply():
        if not view.is_valid():
            return

        if view.is_loading():
            sublime.set_timeout(apply, 10)
            return

        view.run_command(
            "pg_smarts_apply_edits",
            {
                "position_encoding": position_encoding,
                "edits": edits,
            },
        )

    apply()


def transient_sheet_ids(window: sublime.Window) -> set[int]:
    return {
        sheet.id()
        for group in range(window.num_groups())
        if (sheet := window.transient_sheet_in_group(group))
    }


def capture_transient_sheets(window: sublime.Window) -> Callable[[], None]:
    """
    Returns a function which closes transient sheets opened after this call -
    e.g. previews opened while highlighting quick panel items.
    """
    before = transient_sheet_ids(window)

    def close():
        for group in range(window.num_groups()):
            sheet = window.transient_sheet_in_group(group)

            if sheet and sheet.id() not in before and sheet.is_transient():
                sheet.close()

    return close


def goto_location(
    window: sublime.Window,
    position_encoding: smarts_client.LSPPositionEncoding,
    locations: list[smarts_client.LSPLocation],
    item_builder: Callable[
        [sublime.Window, smarts_client.LSPLocation], sublime.QuickPanelItem
    ],
    flags: sublime.NewFileFlags = sublime.NewFileFlags.NONE,
    on_cancel: Callable[[], None] | None = None,
    quick_panel_caret: Literal["start", "end"] = "start",
):
    if len(locations) == 1:
        open_location(
            window,
            position_encoding=position_encoding,
            location=locations[0],
            caret="start",
            flags=flags,
        )
    else:
        locations = sorted(
            locations,
            key=lambda location: [
                location["uri"],
                location["range"]["start"]["line"],
                location["range"]["start"]["character"],
            ],
        )

        close_previews = capture_transient_sheets(window)

        def on_highlight(index):
            open_location(
                window,
                position_encoding=position_encoding,
                location=locations[index],
                flags=sublime.TRANSIENT,
            )

        def on_select(index):
            if index == -1:
                close_previews()

                if on_cancel:
                    on_cancel()
            else:
                open_location(
                    window,
                    position_encoding=position_encoding,
                    location=locations[index],
                    caret=quick_panel_caret,
                    flags=flags,
                )

        items = [item_builder(window, location) for location in locations]

        window.show_quick_panel(
            items,
            on_select=on_select,
            on_highlight=on_highlight,
        )


def goto_diagnostic(
    window: sublime.Window,
    diagnostics: list[PgSmartsDiagnostic],
    on_cancel: Callable[[], None] | None = None,
):
    if len(diagnostics) == 1:
        open_location(
            window,
            position_encoding=diagnostics[0]["position_encoding"],
            location=diagnostics[0],
            caret="start",
        )
    else:
        diagnostics = sorted(
            diagnostics,
            key=lambda diagnostic: [
                diagnostic_severity(diagnostic),
                diagnostic["uri"],
                diagnostic["range"]["start"]["line"],
                diagnostic["range"]["start"]["character"],
            ],
        )

        close_previews = capture_transient_sheets(window)

        def on_highlight(index):
            open_location(
                window,
                position_encoding=diagnostics[index]["position_encoding"],
                location=diagnostics[index],
                flags=sublime.ENCODED_POSITION | sublime.TRANSIENT,
            )

        def on_select(index):
            if index == -1:
                close_previews()

                if on_cancel:
                    on_cancel()
            else:
                open_location(
                    window,
                    position_encoding=diagnostics[index]["position_encoding"],
                    location=diagnostics[index],
                    caret="start",
                )

        window.show_quick_panel(
            [diagnostic_quick_panel_item(diagnostic) for diagnostic in diagnostics],
            on_select=on_select,
            on_highlight=on_highlight,
        )


# -- LSP


def view_textDocumentIdentifier(
    view: sublime.View,
) -> smarts_client.LSPTextDocumentIdentifier:
    """
    Text documents are identified using a URI. On the protocol level, URIs are passed as strings.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentIdentifier
    """
    return {
        "uri": view_file_name_uri(view),
    }


def view_textDocumentPositionParams(
    view: sublime.View,
    position_encoding: smarts_client.LSPPositionEncoding,
    point: int | None = None,
) -> smarts_client.LSPTextDocumentPositionParams:
    """
    A parameter literal used in requests to pass a text document and a position inside that document.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentPositionParams
    """
    # (A point of 0 - the beginning of the document - is a valid point.)
    position_point: int = view.sel()[0].begin() if point is None else point

    return {
        "textDocument": view_textDocumentIdentifier(view),
        "position": point_position(view, position_encoding, position_point),
    }


def view_completionContext(
    view: sublime.View,
    smart: PgSmart,
    point: int,
) -> smarts_client.LSPCompletionContext:
    trigger_kind = 1  # Invoked
    context: smarts_client.LSPCompletionContext = {
        "triggerKind": trigger_kind,
    }

    if point <= 0:
        return context

    if not (server_capabilities := smart.client.server_capabilities()):
        return context

    completion_provider = server_capabilities.get("completionProvider")

    if not isinstance(completion_provider, dict):
        return context

    trigger_characters = completion_provider.get("triggerCharacters") or []
    trigger_character = view.substr(sublime.Region(point - 1, point))

    if trigger_character in trigger_characters:
        context["triggerKind"] = 2  # TriggerCharacter
        context["triggerCharacter"] = trigger_character

    return context


def completions(
    items: list[smarts_client.LSPCompletionItem],
) -> list[sublime.CompletionItem]:
    completions: list[sublime.CompletionItem] = []

    for item in items:
        # The label of this completion item.
        # The label property is also by default the text that is inserted when selecting this completion.
        label = item.get("label") or ""

        annotation = item.get("detail") or ""

        # A string that should be inserted into a document when selecting this completion.
        # When omitted the label is used as the insert text for this item.
        insert_text = item.get("insertText") or label

        completion_kind = item.get("kind") or 0

        kind = kCOMPLETION_ITEM_KIND.get(completion_kind, sublime.KIND_ID_AMBIGUOUS)

        details = item.get("documentation")

        if isinstance(details, dict):
            details = details.get("value")

        completions.append(
            sublime.CompletionItem(
                trigger=label,
                annotation=annotation,
                completion=insert_text,
                kind=(kind, "", annotation),
                details=details or "",
            )
        )

    return completions


# Language identifier of syntaxes whose scope doesn't tell it; See `view_languageId`.
kSYNTAX_LANGUAGE_ID = {
    "Packages/Clojure/Clojure.sublime-syntax": "clojure",
    "Packages/Clojure/ClojureScript.sublime-syntax": "clojure",
    "Packages/Tutkain/EDN (Tutkain).sublime-syntax": "clojure",
    "Packages/Tutkain/Clojure (Tutkain).sublime-syntax": "clojure",
    "Packages/Tutkain/ClojureScript (Tutkain).sublime-syntax": "clojure",
    "Packages/Tutkain/Clojure Common (Tutkain).sublime-syntax": "clojure",
    "Packages/Clojure Sublimed/Clojure (Sublimed).sublime-syntax": "clojure",
}

# Language identifier of scopes whose name isn't the identifier.
kSCOPE_LANGUAGE_ID = {
    "source.js": "javascript",
    "source.jsx": "javascriptreact",
    "source.ts": "typescript",
    "source.tsx": "typescriptreact",
    "source.c++": "cpp",
    "source.cs": "csharp",
    "source.objc": "objective-c",
    "source.objc++": "objective-cpp",
    "source.shell.bash": "shellscript",
    "text.html.basic": "html",
    "text.html.markdown": "markdown",
    "text.tex.latex": "latex",
}


def scope_languageId(scope: str) -> str:
    """
    Returns the language identifier for the scope of a syntax.

    Most scopes are named after the language - e.g. "source.python" is "python".
    """
    if language_id := kSCOPE_LANGUAGE_ID.get(scope):
        return language_id

    names = scope.split(".")

    return names[1] if len(names) > 1 else ""


def view_languageId(view: sublime.View) -> str:
    """
    Returns the text document's language identifier.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentItem
    """
    if language_id := kSYNTAX_LANGUAGE_ID.get(view_syntax(view)):
        return language_id

    if syntax := view.syntax():
        return scope_languageId(syntax.scope)

    return ""


def handle_window_message(
    window: sublime.Window,
    message: smarts_client.LSPNotificationMessage,
) -> None:
    """
    Log a message of the server - both a message to log and a message to show
    have a type and a text, and are logged in the Output panel.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#window_logMessage
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#window_showMessage
    """

    if params := message["params"]:
        message_type = params["type"]
        message_type = kMESSAGE_TYPE_NAME.get(message_type, message_type)

        message_message = params["message"]

        panel_log(window, f"{message_type} {message_message}\n")


def present_diagnostics(
    view: sublime.View,
    diagnostics: list[PgSmartsDiagnostic],
):
    """
    Render diagnostics in the view (regions, annotations, status bar).
    """
    clear_diagnostics(view)

    diagnostics_status = []

    for k, g in groupby(
        sorted(diagnostics, key=diagnostic_severity), key=diagnostic_severity
    ):
        severity_regions = []
        severity_annotations = []
        severity_diagnostics = list(g)

        diagnostics_status.append(f"{severity_name(k)}: {len(severity_diagnostics)}")

        for d in severity_diagnostics:
            # Regions by Severity
            severity_regions.append(
                range_region(view, d["position_encoding"], d["range"]),
            )

            message = d["message"]

            if origin := diagnostic_origin(d):
                message = f"{message} · {origin}"

            # Annotations (minihtml) by Severity
            severity_annotations.append(
                (
                    "<html>"
                    "<body id='smarts-diagnostic-annotation' "
                    "style='margin:0;padding:0;font-size:1rem;line-height:1rem;'>"
                    "<span>"
                    f"{html.escape(message)}"
                    "</span>"
                    "</body>"
                    "</html>"
                ),
            )

        view.add_regions(
            f"{kDIAGNOSTICS}_SEVERITY_{k}",
            severity_regions,
            scope=severity_scope(k),
            annotations=severity_annotations,
            annotation_color=severity_annotation_color(view, k),
            flags=sublime.DRAW_SQUIGGLY_UNDERLINE
            | sublime.DRAW_NO_FILL
            | sublime.DRAW_NO_OUTLINE,
        )

    view.set_status(kDIAGNOSTICS, ", ".join(diagnostics_status))


def clear_diagnostics(view: sublime.View):
    # Clear annotations/regions for all severity levels and status.
    for severity in kSEVERITY_STYLE:
        view.erase_regions(f"{kDIAGNOSTICS}_SEVERITY_{severity}")

    view.erase_status(kDIAGNOSTICS)


def handle_textDocument_publishDiagnostics(
    window: sublime.Window,
    smart: PgSmart,
    message: smarts_client.LSPNotificationMessage,
):
    """
    Diagnostics notifications are sent from the server to the client to signal results of validation runs.

    Diagnostics are "owned" by the server so it is the server's responsibility to clear them if necessary.

    When a file changes it is the server's responsibility to re-compute diagnostics and push them to the client.
    If the computed set is empty it has to push the empty array to clear former diagnostics.
    Newly pushed diagnostics always replace previously pushed diagnostics.
    There is no merging that happens on the client side.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#publishDiagnosticsParams
    """
    position_encoding = smart.position_encoding()

    params = cast(smarts_client.LSPPublishDiagnosticsParams, message["params"])

    # Including URI to a Diagnostic make it equivalent to a Location - `uri` and `range`.
    # (Anything that works with a Location also works with this Diagnostic.)
    diagnostics = [
        cast(
            PgSmartsDiagnostic,
            {
                "uri": params["uri"],
                "position_encoding": position_encoding,
                "server": smart.config["name"],
                **diagnostic,
            },
        )
        for diagnostic in params["diagnostics"]
    ]

    path = uri_to_path(params["uri"])

    # Diagnostics are stored even if the document is not open:
    # they're presented when the document is loaded, and are available to Goto Diagnostic.
    #
    # Skip redraw work when the server republishes the same diagnostics while the user is typing.
    if not set_server_diagnostics(window.id(), path, smart.uuid, diagnostics):
        return

    sublime.set_timeout(lambda: schedule_present_diagnostics(window, path), 0)


def schedule_present_diagnostics(window: sublime.Window, path: str):
    """
    Present diagnostics for `path`, if it's open, after a cool-down period.

    Must be called on the main thread.
    """
    view = window.find_open_file(path)

    if view is None:
        return

    view_id = view.id()

    # Supersedes any pending presentation for this view.
    token = object()
    _DIAGNOSTICS_PENDING[view_id] = token

    def _present():
        if _DIAGNOSTICS_PENDING.get(view_id) is not token:
            return

        del _DIAGNOSTICS_PENDING[view_id]

        if view.is_valid():
            present_diagnostics(view, view_diagnostics(view))

    # Cool-down period: Suppress presenting diagnostics while actively editing.
    #
    # Without this, diagnostics constantly update while typing, causing
    # distracting visual changes (squiggly lines and annotations moving around).
    # We wait until the user stops typing before showing updated diagnostics.
    last_modified = _LAST_MODIFIED.get(view_id, 0)
    cool_down = 1.0
    delay = max(0.0, cool_down - (time.monotonic() - last_modified))

    sublime.set_timeout(_present, int(delay * 1000))


def handle_notification(
    smart_uuid: str,
    notification: smarts_client.LSPNotificationMessage,
):
    smart = find_smart(smart_uuid)

    if smart is None:
        return

    window = find_window(smart.window)

    if not window:
        return

    message_method = notification.get("method")

    if message_method in ("window/logMessage", "window/showMessage"):
        handle_window_message(window, notification)

    elif message_method == "textDocument/publishDiagnostics":
        handle_textDocument_publishDiagnostics(window, smart, notification)

    # Progress is not presented. (Sent often - don't log it.)
    elif message_method == "$/progress":
        pass

    else:
        panel_log(window, f"Unhandled Notification: {pprint.pformat(notification)}\n\n")


def handle_failure(smart_uuid: str, reason: str):
    """
    Remove a Smart whose server failed - e.g. it crashed, or didn't initialize.

    A failed server is unusable, and nothing it published is valid anymore:
    removing the Smart also removes its diagnostics.

    Must be called on the main thread.
    """
    smart = find_smart(smart_uuid)

    # Removed already - e.g. the user shut it down.
    if smart is None:
        return

    if window := find_window(smart.window):
        panel_log(window, f"Error {smart.config['name']}: {reason}\n", show=True)

    remove_smarts({smart_uuid})


# -- INPUT HANDLERS


class RootPathInputHandler(sublime_plugin.TextInputHandler):
    def __init__(self, initial_text: str):
        self._initial_text = initial_text

    def placeholder(self):
        return "Root Path"

    def name(self):
        return "rootPath"

    def initial_text(self):
        return self._initial_text

    def initial_selection(self):
        end = len(self._initial_text)
        return [(end, end)]

    def validate(self, text: str, event=None):
        return bool(text.strip())


class ServerInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, window: sublime.Window, items):
        self.window = window
        self.items = items

    def placeholder(self):
        return "Server"

    def name(self):
        return "server"

    def list_items(self):
        return self.items

    def next_input(
        self, args
    ) -> sublime_plugin.BackInputHandler | RootPathInputHandler:
        folders = self.window.folders()

        if not folders:
            sublime.error_message("Can't initialize server without an open folder.")

            return sublime_plugin.BackInputHandler()

        return RootPathInputHandler(folders[0])


class SmartsInputHandler(sublime_plugin.ListInputHandler):
    def placeholder(self):
        return "Server"

    def name(self):
        return "smart_uuid"

    def list_items(self):
        items = []

        for smart in window_running_smarts(sublime.active_window()):
            smart_uuid = smart.uuid
            smart_server_name = smart.client._name

            items.append((f"{smart_server_name} {smart_uuid}", smart_uuid))

        return items


# -- COMMANDS


class PgSmartsInitializeCommand(sublime_plugin.WindowCommand):
    def input(self, args):
        return ServerInputHandler(
            self.window,
            sorted(
                [server_config["name"] for server_config in available_servers()],
            ),
        )

    def run(self, server: str, rootPath: str):
        rootPath_path = Path(rootPath)

        rootUri = rootPath_path.as_uri()

        workspaceFolders = [{"name": rootPath_path.name, "uri": rootUri}]

        params = {
            "processId": os.getpid(),
            "clientInfo": {
                "name": "Smarts",
                "version": "0.1.0",
            },
            # The rootPath of the workspace. Is null if no folder is open.
            # Deprecated in favour of rootUri.
            "rootPath": rootPath_path.as_posix(),
            # The rootUri of the workspace. Is null if no folder is open.
            # If both rootPath and rootUri are set rootUri wins.
            # Deprecated in favour of workspaceFolders.
            "rootUri": rootUri,
            # The workspace folders configured in the client when the server starts.
            "workspaceFolders": workspaceFolders,
            # Servers must not send $/logTrace notifications.
            "trace": "off",
            "capabilities": {
                "general": {
                    "positionEncodings": ["utf-16", "utf-8"],
                },
                # Client support for textDocument/didOpen, textDocument/didChange
                # and textDocument/didClose notifications is mandatory in the protocol
                # and clients can not opt out supporting them.
                #
                # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_synchronization
                "textDocument": {
                    "synchronization": {
                        # Whether text document synchronization supports dynamic registration.
                        "dynamicRegistration": False,
                        # Save notifications are sent to the server.
                        "didSave": True,
                        # Open and close notifications are sent to the server.
                        "openClose": True,
                        # Documents are synced by always sending the full content of the document.
                        "change": 1,
                    },
                    # Completion capabilities advertised to language servers.
                    # Keep this aligned with what on_query_completions() actually supports.
                    "completion": {
                        "dynamicRegistration": False,
                        "completionItem": {
                            "documentationFormat": ["markdown", "plaintext"],
                        },
                        "completionItemKind": {
                            "valueSet": list(kCOMPLETION_ITEM_KIND.keys()),
                        },
                    },
                    "hover": {
                        "contentFormat": ["markdown", "plaintext"],
                    },
                    # The signature help request is sent from the client to the server to request signature information at a given cursor position.
                    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_signatureHelp
                    "signatureHelp": {
                        "signatureInformation": {
                            "documentationFormat": ["plaintext", "markdown"],
                            "activeParameterSupport": True,
                        },
                        "contextSupport": True,
                    },
                },
            },
        }

        server_config = None

        for _server_config in available_servers():
            if _server_config["name"] == server:
                server_config = _server_config

        if server_config is None:
            plugin_logger.error(
                f"Server {server} not found; Did you forget to configure Smarts.sublime-settings?"
            )
            return

        smart_uuid = str(uuid.uuid4())

        def _on_receive_notification(message):
            handle_notification(smart_uuid, message)

        client = smarts_client.LanguageServerClient(
            logger=plugin_logger,
            name=server_config["name"],
            server_args=server_config["start"],
            on_send=lambda message: messages_panel_insert(
                self.window, "Send", message
            ),
            on_receive=lambda message: messages_panel_insert(
                self.window, "Receive", message
            ),
            on_notification=_on_receive_notification,
            # Called on a thread of the client; Smarts are removed on the main thread.
            on_failure=lambda reason: sublime.set_timeout(
                lambda: handle_failure(smart_uuid, reason), 0
            ),
        )

        with _SMARTS_LOCK:
            _SMARTS.append(
                PgSmart(
                    uuid=smart_uuid,
                    window=self.window.id(),
                    config=server_config,
                    client=client,
                )
            )

        def callback(response: smarts_client.LSPResponseMessage):
            # A server which fails to initialize is handled by `handle_failure`.
            if response.get("error"):
                return

            # Notify the server about open documents.
            #
            # On the main thread - this is a thread of the client: a document's
            # text and version must be read together, with no edit in between.
            def open_documents():
                for view in self.window.views():
                    document_open(view)

            sublime.set_timeout(open_documents, 0)

        client.initialize(params, callback)


class PgSmartsShutdownCommand(sublime_plugin.WindowCommand):
    def input(self, args):
        if "smart_uuid" not in args:
            return SmartsInputHandler()

    def run(self, smart_uuid):
        if smart := find_smart(smart_uuid):
            smart.client.shutdown()
            remove_smarts({smart_uuid})


class PgSmartsToggleOutputPanelCommand(sublime_plugin.WindowCommand):
    def run(self):
        toggle_panel(self.window, kOUTPUT_PANEL_NAME)


class PgSmartsClearOutputPanelCommand(sublime_plugin.WindowCommand):
    def run(self):
        clear_panel(self.window, kOUTPUT_PANEL_NAME)


class PgSmartsToggleMessagesPanelCommand(sublime_plugin.WindowCommand):
    def run(self):
        toggle_panel(self.window, kOUTPUT_PANEL_MESSAGES_NAME, kMESSAGES_PANEL_SYNTAX)


class PgSmartsClearMessagesPanelCommand(sublime_plugin.WindowCommand):
    def run(self):
        clear_panel(self.window, kOUTPUT_PANEL_MESSAGES_NAME)


class PgSmartsStatusCommand(sublime_plugin.WindowCommand):
    def run(self):
        smarts = window_smarts(self.window)

        if not smarts:
            return

        lines: list[str] = []

        for smart in smarts:
            client = smart.client

            pid = client._server_process.pid if client._server_process else None

            lines.extend([
                f"# {client._name}",
                "",
                f"- **UUID:** {smart.uuid}",
                f"- **Status:** {client.server_status().name}",
                f"- **PID:** {pid}",
                "",
                "## Info",
                "",
            ])

            if server_info := client._server_info:
                lines.extend(["```json", json.dumps(server_info, indent=2), "```"])
            else:
                lines.append("-")

            lines.extend(["", "## Capabilities", ""])

            if server_capabilities := client.server_capabilities():
                lines.extend([
                    "```json",
                    json.dumps(server_capabilities, indent=2),
                    "```",
                ])
            else:
                lines.append("-")

            lines.extend(["", "---", ""])

        content = "\n".join(lines)

        view = self.window.new_file(sublime.SEMI_TRANSIENT | sublime.ADD_TO_SELECTION)
        view.set_name("Smarts Status")
        view.set_scratch(True)
        view.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")
        view.run_command("append", {"characters": content})


class PgSmartsGotoDefinition(sublime_plugin.TextCommand):
    def run(self, _, side_by_side=False):
        smart = applicable_smart(self.view, method="textDocument/definition")

        if smart is None:
            return

        position_encoding = smart.position_encoding()

        open_file_flags = (
            sublime.SEMI_TRANSIENT | sublime.ADD_TO_SELECTION
            if side_by_side
            else sublime.NewFileFlags.NONE
        )

        params = view_textDocumentPositionParams(self.view, position_encoding)

        restore_view = capture_view(self.view)

        def on_result(result: smarts_client.LSPDefinitionResult):
            if not result:
                return

            locations = cast(
                list[smarts_client.LSPLocation],
                [result] if isinstance(result, dict) else result,
            )

            if window := self.view.window():
                goto_location(
                    window,
                    position_encoding=position_encoding,
                    locations=locations,
                    item_builder=location_quick_panel_item,
                    flags=open_file_flags,
                    on_cancel=restore_view,
                )

        on_error = panel_error_logger(self.view)

        smart.client.textDocument_definition(params, on_result, on_error)


class PgSmartsGotoReference(sublime_plugin.TextCommand):
    def run(self, _):
        smart = applicable_smart(self.view, method="textDocument/references")

        if smart is None:
            return

        position_encoding = smart.position_encoding()

        restore_view = capture_view(self.view)

        def on_result(result: smarts_client.LSPReferencesResult):
            if not result:
                return

            if window := self.view.window():
                goto_location(
                    window,
                    position_encoding=position_encoding,
                    locations=result,
                    item_builder=location_quick_panel_item,
                    on_cancel=restore_view,
                    quick_panel_caret="end",
                )

        on_error = panel_error_logger(self.view)

        params = {
            "context": {
                "includeDeclaration": False,
            },
            **view_textDocumentPositionParams(self.view, position_encoding),
        }

        smart.client.textDocument_references(params, on_result, on_error)


class PgSmartsGotoDocumentDiagnostic(sublime_plugin.TextCommand):
    def run(self, _):
        restore_view = capture_view(self.view)

        diagnostics = view_diagnostics(self.view)

        if window := self.view.window():
            goto_diagnostic(
                window,
                diagnostics,
                on_cancel=restore_view,
            )


class PgSmartsGotoDiagnostic(sublime_plugin.WindowCommand):
    def run(self):
        on_cancel = None

        if view := self.window.active_view():
            if view.element() is None:
                on_cancel = capture_view(view)

        diagnostics = window_diagnostics(self.window)

        goto_diagnostic(
            self.window,
            diagnostics,
            on_cancel=on_cancel,
        )


class PgSmartsGotoDocumentSymbol(sublime_plugin.TextCommand):
    def run(self, _):
        smart = applicable_smart(self.view, method="textDocument/documentSymbol")

        if smart is None:
            return

        position_encoding = smart.position_encoding()

        def on_result(result: smarts_client.LSPDocumentSymbolResult):
            # Document Symbols Request
            # DocumentSymbol[] | SymbolInformation[] | null
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentSymbol
            if result:
                symbols = flatten_document_symbols(result)

                restore_view = capture_view(self.view)

                def select_symbol(index, empty_region=False):
                    data = symbols[index]

                    selected_range = None

                    # Represents information about programming constructs like variables, classes, interfaces etc.
                    # @deprecated use DocumentSymbol or WorkspaceSymbol instead.
                    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#symbolInformation
                    if location := data.get("location"):
                        selected_range = location["range"]

                    # The range that should be selected and revealed when this symbol is being
                    # picked, e.g. the name of a function. Must be contained by the `range`.
                    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#documentSymbol
                    else:
                        selected_range = data["selectionRange"]

                    selected_region = range_region(
                        self.view,
                        position_encoding,
                        selected_range,
                        inverted=True,
                    )

                    selection_region = selected_region

                    if empty_region:
                        selection_region = sublime.Region(
                            selected_region.end(),
                            selected_region.end(),
                        )

                    self.view.run_command(
                        "pg_smarts_set_selection",
                        {
                            "regions": [
                                [selection_region.a, selection_region.b],
                            ],
                        },
                    )
                    self.view.show_at_center(selection_region)

                def on_highlight(index):
                    select_symbol(index)

                def on_select(index):
                    global _DOCUMENT_SYMBOL_VIEW_ID
                    _DOCUMENT_SYMBOL_VIEW_ID = None

                    if index == -1:
                        restore_view()

                    else:
                        # The first quick-panel item may be accepted without
                        # triggering on_highlight, so apply its range here too.
                        select_symbol(index, empty_region=True)

                quick_panel_items = [
                    document_symbol_quick_panel_item(data) for data in symbols
                ]

                window = self.view.window()

                if not window:
                    return

                global _DOCUMENT_SYMBOL_VIEW_ID
                _DOCUMENT_SYMBOL_VIEW_ID = self.view.id()

                window.show_quick_panel(
                    quick_panel_items,
                    on_select,
                    on_highlight=on_highlight,
                )

        params = {
            "textDocument": view_textDocumentIdentifier(self.view),
        }

        on_error = panel_error_logger(self.view)

        smart.client.textDocument_documentSymbol(params, on_result, on_error)


class PgSmartsSetSelectionCommand(sublime_plugin.TextCommand):
    def run(self, _, regions):
        self.view.sel().clear()

        for start, end in regions:
            self.view.sel().add(sublime.Region(start, end))


# WIP
class PgSmartsGotoWorkspaceSymbol(sublime_plugin.WindowCommand):
    def run(self):
        def on_result(result: smarts_client.LSPWorkspaceSymbolResult):
            # Workspace Symbols Request
            # SymbolInformation[] | WorkspaceSymbol[] | null
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#workspace_symbol
            if result:
                restore_view = None

                if view := self.window.active_view():
                    restore_view = capture_view(view)

                def on_highlight(index):
                    # FIXME
                    open_location(
                        self.window,
                        position_encoding="utf-16",
                        location=result[index]["location"],
                        flags=sublime.ENCODED_POSITION | sublime.TRANSIENT,
                    )

                def on_select(index):
                    if index == -1:
                        if restore_view:
                            restore_view()
                    else:
                        # FIXME
                        open_location(
                            self.window,
                            position_encoding="utf-16",
                            location=result[index]["location"],
                            caret="start",
                            flags=sublime.ENCODED_POSITION | sublime.TRANSIENT,
                        )

                quick_panel_items = [
                    workspace_symbol_quick_panel_item(data) for data in result
                ]

                self.window.show_quick_panel(
                    quick_panel_items,
                    on_select,
                    on_highlight=on_highlight,
                )

        # This is not good. Some servers do not return any result until the query is not empty.
        params: smarts_client.LSPWorkspaceSymbolParams = {
            # A query string to filter symbols by. Clients may send an empty string here to request all symbols.
            "query": "",
        }

        def on_error(error: smarts_client.LSPResponseError):
            panel_log_error(self.window, error)

        # TODO: Support multiple Smarts
        for smart in window_running_smarts(self.window):
            if smart.client.support_method("workspace/symbol"):
                smart.client.workspace_symbol(params, on_result, on_error)
                break


class PgSmartsSelectCommand(sublime_plugin.TextCommand):
    def run(self, _):
        regions = view_highlights(self.view)

        if not regions:
            return

        self.view.sel().clear()
        self.view.sel().add_all(regions)


class PgSmartsJumpCommand(sublime_plugin.TextCommand):
    def run(self, _, movement):
        regions = view_highlights(self.view)

        if not regions:
            return

        trampoline = self.view.sel()[0]

        jump_loc_index = None

        for index, r in enumerate(regions):
            if r.contains(trampoline.begin()) or r.contains(trampoline.end()):
                if movement == "back":
                    jump_loc_index = max(0, index - 1)
                elif movement == "forward":
                    jump_loc_index = min(index + 1, len(regions) - 1)

                break

        if jump_loc_index is not None:
            jump_region = sublime.Region(regions[jump_loc_index].end())

            self.view.sel().clear()
            self.view.sel().add(jump_region)
            self.view.show(jump_region)


class PgSmartsShowHoverCommand(sublime_plugin.TextCommand):
    def run(self, _, position=None, show="popup"):
        smart = applicable_smart(self.view, method="textDocument/hover")

        if smart is None:
            return

        position_encoding = smart.position_encoding()

        # (A position of 0 - the beginning of the document - is a valid position.)
        point = position if position is not None else self.view.sel()[0].begin()

        def on_result(result: smarts_client.LSPHoverResult):
            if result:
                if show == "transient":
                    word_region = self.view.word(point)
                    word_str = self.view.substr(word_region)
                    self._show_transient(smart, word_str, result)
                else:
                    self._show_popup(smart, result)

        on_error = panel_error_logger(self.view)

        params = view_textDocumentPositionParams(
            self.view,
            position_encoding,
            point,
        )

        smart.client.textDocument_hover(params, on_result, on_error)

    def _show_transient(self, smart: PgSmart, name: str, hover: smarts_client.LSPHover):
        window = self.view.window()

        if not window:
            return

        markdown = self._hover_markdown(hover)
        markdown += f"\n\n---\n*{smart.client._name}*"

        view = window.new_file(sublime.SEMI_TRANSIENT | sublime.ADD_TO_SELECTION)
        view.set_name(name)
        view.set_scratch(True)
        view.assign_syntax("Packages/Markdown/Markdown.sublime-syntax")
        view.run_command("append", {"characters": markdown})

    def _show_popup(self, smart: PgSmart, hover: smarts_client.LSPHover):
        markdown = self._hover_markdown(hover)
        content_html = smarts_markdown.markdown_to_html(
            markdown,
            code_block_renderer=self._code_minihtml,
        )

        styles = """
        .container {
            color: var(--foreground);
            font-size: 0.875rem;
            line-height: 1.3;
        }

        .container h1,
        .container h2,
        .container h3,
        .container h4,
        .container h5,
        .container h6 {
            color: color(var(--foreground) blend(var(--bluish) 60%));
            margin: 0.3rem 0 0.15rem 0;
            font-weight: bold;
        }

        .container h1 { font-size: 1.25rem; color: var(--bluish); }
        .container h2 { font-size: 1.125rem; color: var(--bluish); }
        .container h3 { font-size: 1rem; }
        .container p { margin: 0.1rem 0; }
        .container pre { margin: 0.3rem 0; }
        .container a { color: var(--bluish); text-decoration: none; }
        .container strong { font-weight: bold; }
        .container em { font-style: italic; }
        .container .hr { border-top: 1px solid color(var(--foreground) alpha(0.1)); margin: 0.7rem 0; }
        """

        minihtml = f"""
        <style>
            {kMINIHTML_STYLES}
            {styles}
        </style>
        <body class='container'>
            {content_html}
            <div class='hr'></div>
            <span class='text-pinkish font-bold'>{html.escape(smart.client._name)}</span>
        </body>
        """

        location = -1

        if result_range := hover.get("range"):
            location = position_point(
                self.view,
                smart.position_encoding(),
                result_range["start"],
            )

        self.view.show_popup(
            minihtml,
            location=location,
            max_width=1000,
            max_height=600,
        )

    def _code_minihtml(self, code_block: str) -> str:
        window = self.view.window()

        if not window:
            raise RuntimeError(
                "Smarts hover popups require a window to render code fences."
            )

        language, body = self._split_language_body(code_block)

        panel_name = f"Smarts-{uuid.uuid4().hex}"

        output_view = window.create_output_panel(panel_name, unlisted=True)
        output_view.assign_syntax(language_syntax(language))

        try:
            output_view.run_command("append", {"characters": body})

            return output_view.export_to_html(minihtml=True)
        finally:
            window.destroy_output_panel(panel_name)

    def _split_language_body(self, code_block: str) -> tuple[str, str]:
        match = re.match(r"^```([^\n]*)\n(.*?)\n```$", code_block, re.DOTALL)

        if not match:
            return "", code_block

        return match.group(1).strip(), match.group(2)

    def _hover_markdown(self, hover: smarts_client.LSPHover) -> str:
        contents = hover["contents"]

        """Extract markdown content from LSP hover contents."""
        if isinstance(contents, str):
            return contents

        elif isinstance(contents, dict):
            # MarkupContent { kind: 'markdown' | 'plaintext', value: string }
            return contents.get("value", "")

        elif isinstance(contents, list):
            # Array of MarkedString | MarkupContent
            sections = []
            for item in contents:
                if isinstance(item, str):
                    sections.append(item)
                elif isinstance(item, dict):
                    if "language" in item:
                        # MarkedString with language: { language: string, value: string }
                        lang = item.get("language", "")
                        value = item.get("value", "")
                        sections.append(f"```{lang}\n{value}\n```")
                    else:
                        sections.append(item.get("value", ""))
            return "\n\n".join(sections)

        return ""


class PgSmartsShowSignatureHelpCommand(sublime_plugin.TextCommand):
    def run(self, _, position=None):
        smart = applicable_smart(self.view, method="textDocument/signatureHelp")

        if smart is None:
            return

        if position is None:
            position = self.view.sel()[0].begin()

        position_encoding = smart.position_encoding()

        params = cast(
            smarts_client.LSPSignatureHelpParams,
            {
                **view_textDocumentPositionParams(
                    self.view, position_encoding, position
                ),
                "context": {
                    "triggerKind": 1,  # Invoked manually
                    "isRetrigger": False,
                },
            },
        )

        def on_result(result: smarts_client.LSPSignatureHelpResult):
            if result:
                show_signature_help_popup(self.view, smart, result)

        on_error = panel_error_logger(self.view)

        smart.client.textDocument_signatureHelp(params, on_result, on_error)


def view_formatting_options(view: sublime.View) -> smarts_client.LSPFormattingOptions:
    return {
        "tabSize": view.settings().get("tab_size"),
        "insertSpaces": True,
        "insertFinalNewline": None,
        "trimTrailingWhitespace": None,
        "trimFinalNewlines": None,
    }


def text_edits_application_order(
    edits: list[smarts_client.LSPTextEdit],
) -> list[smarts_client.LSPTextEdit]:
    """
    Returns `edits` in the order to apply them, one by one.

    All ranges refer to the document before any edit, so edits are applied
    from the end of the document to its start: an edit never moves the
    range of the ones still to apply.

    Inserts at the same position are applied last to first: each one is
    inserted before the previous, so their text ends up in the order of `edits`.
    """
    # The sort is stable - also if reversed - hence `edits` is reversed first.
    return sorted(
        reversed(edits),
        key=lambda e: (
            e["range"]["start"]["line"],
            e["range"]["start"]["character"],
        ),
        reverse=True,
    )


def text_edits_applier(
    view: sublime.View,
    position_encoding: smarts_client.LSPPositionEncoding,
) -> Callable[[smarts_client.LSPFormattingResult], None]:
    """
    Returns a formatting result callback which applies the edits to `view`.

    Edits are dropped if the document changed since this function was called -
    i.e. since the request: their ranges refer to a document which no longer
    exists, and applying them would corrupt the text.
    """
    change_count = view.change_count()

    def on_result(result: smarts_client.LSPFormattingResult):
        if not result:
            return

        if view.change_count() != change_count:
            plugin_logger.debug("Document changed; Will drop formatting edits")
            return

        view.run_command(
            "pg_smarts_apply_edits",
            {
                "position_encoding": position_encoding,
                "edits": result,
            },
        )

    return on_result


class PgSmartsFormatDocumentCommand(sublime_plugin.TextCommand):
    def run(self, _):
        smart = applicable_smart(self.view, method="textDocument/formatting")

        if smart is None:
            return

        position_encoding = smart.position_encoding()

        params: smarts_client.LSPDocumentFormattingParams = {
            "textDocument": view_textDocumentIdentifier(self.view),
            "options": view_formatting_options(self.view),
        }

        on_result = text_edits_applier(self.view, position_encoding)
        on_error = panel_error_logger(self.view)

        smart.client.textDocument_formatting(params, on_result, on_error)


class PgSmartsFormatSelectionCommand(sublime_plugin.TextCommand):
    def run(self, _, region=None):
        smart = applicable_smart(self.view, method="textDocument/rangeFormatting")

        if smart is None:
            return

        if region is None:
            regions = [r for r in self.view.sel() if not r.empty()]
        else:
            regions = [sublime.Region(region[0], region[1])]

        # Regions are formatted one at a time, from the last to the first:
        # the edits of a region change the text - so the next region is only
        # requested once they're applied - but never move the regions before it.
        regions = sorted(regions, key=lambda r: r.begin(), reverse=True)

        self._format(smart, regions)

    def _format(self, smart: PgSmart, regions: list[sublime.Region]):
        """
        Format the first of `regions`, then - once its edits are applied - the rest.
        """
        if not regions or not self.view.is_valid():
            return

        view = self.view
        position_encoding = smart.position_encoding()
        change_count = view.change_count()

        params = {
            "textDocument": view_textDocumentIdentifier(view),
            "range": region_range(view, position_encoding, regions[0]),
            "options": view_formatting_options(view),
        }

        def apply(result: smarts_client.LSPFormattingResult):
            # The ranges of the edits, and the remaining regions, refer to
            # a document which no longer exists.
            if view.change_count() != change_count:
                plugin_logger.debug("Document changed; Will drop formatting edits")
                return

            if result:
                view.run_command(
                    "pg_smarts_apply_edits",
                    {
                        "position_encoding": position_encoding,
                        "edits": result,
                    },
                )

            self._format(smart, regions[1:])

        # Called on a thread of the client; Edits are applied on the main thread,
        # where servers are told about the change before the next request.
        def on_result(result: smarts_client.LSPFormattingResult):
            sublime.set_timeout(lambda: apply(result), 0)

        on_error = panel_error_logger(view)

        smart.client.textDocument_rangeFormatting(params, on_result, on_error)


class PgSmartsApplyEditsCommand(sublime_plugin.TextCommand):
    def run(self, edit, edits, position_encoding):
        for e in text_edits_application_order(edits):
            edit_region = range_region(self.view, position_encoding, e["range"])
            edit_new_text = e["newText"]

            self.view.replace(edit, edit_region, edit_new_text)


class PgSmartsRenameCommand(sublime_plugin.TextCommand):
    def run(self, _):
        smart = applicable_smart(self.view, method="textDocument/rename")

        if smart is None:
            return

        position_encoding = smart.position_encoding()

        # Get the word at the current cursor position to use as placeholder
        word_point = self.view.sel()[0].begin()
        word_region = self.view.word(word_point)

        current_name = self.view.substr(word_region)

        def on_done(new_name: str):
            if not new_name or new_name == current_name:
                return

            params = cast(
                smarts_client.LSPRenameParams,
                {
                    **view_textDocumentPositionParams(self.view, position_encoding),
                    "newName": new_name,
                },
            )

            def on_result(result: smarts_client.LSPRenameResult):
                if result and (window := self.view.window()):
                    sublime.set_timeout(
                        lambda: apply_workspace_edit(
                            window,
                            position_encoding,
                            result,
                        )
                    )

            on_error = panel_error_logger(self.view)

            smart.client.textDocument_rename(params, on_result, on_error)

        if window := self.view.window():
            window.show_input_panel("New Name:", current_name, on_done, None, None)


## -- Listeners


class PgSmartsTextListener(sublime_plugin.TextChangeListener):
    # Documents are synchronized on the main thread - not asynchronously - so
    # servers are told about a change, in order, before anything which depends
    # on it: the next change, or a request for the changed document.
    #
    # It also makes every notification consistent: the text, or the changes,
    # and the version are read with no edit in between.

    def on_text_changed(self, changes):
        buffer = self.buffer

        if not buffer:
            return

        view = buffer.primary_view()

        view_file_name = view.file_name()

        if not view_file_name:
            return

        # Same for every server; The full text is read only if a server needs it.
        uri = path_to_uri(view_file_name)
        version = view.change_count()
        full_text: str | None = None

        for smart in applicable_smarts(view, method="textDocument/didChange"):
            language_client = smart.client

            position_encoding = language_client.position_encoding()

            textDocumentSync = language_client.text_document_sync()

            # The document that did change.
            # The version number points to the version
            # after all provided content changes have been applied.
            #
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#versionedTextDocumentIdentifier
            textDocument: smarts_client.LSPVersionedTextDocumentIdentifier = {
                "uri": uri,
                "version": version,
            }

            # The actual content changes.
            # The content changes describe single state changes to the document.
            # So if there are two content changes c1 (at array index 0) and c2 (at array index 1)
            # for a document in state S then c1 moves the document from S to S' and
            # c2 from S' to S''. So c1 is computed on the state S and c2 is computed on the state S'.
            #
            # If only a text is provided it is considered to be the full content of the document.
            #
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentContentChangeEvent
            contentChanges: list[smarts_client.LSPTextDocumentContentChangeEvent] = []

            # Full
            # Documents are synced by always sending the full content of the document.
            if textDocumentSync["change"] == 1:
                if full_text is None:
                    full_text = view.substr(sublime.Region(0, view.size()))

                contentChanges = [
                    cast(
                        smarts_client.LSPTextDocumentContentChangeEvent,
                        {
                            "text": full_text,
                        },
                    )
                ]

            # Incremental
            # Documents are synced by sending the full content on open.
            # After that only incremental updates to the document are sent.
            elif textDocumentSync["change"] == 2:
                contentChanges = []

                for change in changes:
                    contentChanges.append(
                        cast(
                            smarts_client.LSPTextDocumentContentChangeEvent,
                            {
                                "range": {
                                    "start": {
                                        "line": change.a.row,
                                        "character": change.a.col_utf8
                                        if position_encoding == "utf-8"
                                        else change.a.col_utf16,
                                    },
                                    "end": {
                                        "line": change.b.row,
                                        "character": change.b.col_utf8
                                        if position_encoding == "utf-8"
                                        else change.b.col_utf16,
                                    },
                                },
                                "rangeLength": change.len_utf8
                                if position_encoding == "utf-8"
                                else change.len_utf16,
                                "text": change.str,
                            },
                        )
                    )

            params: smarts_client.LSPDidChangeTextDocumentParams = {
                "textDocument": textDocument,
                "contentChanges": contentChanges,
            }

            language_client.textDocument_didChange(params)

    def on_reload(self):
        if self.buffer and (view := self.buffer.primary_view()):
            document_resync(view)

    def on_revert(self):
        if self.buffer and (view := self.buffer.primary_view()):
            document_resync(view)


class PgSmartsViewListener(sublime_plugin.ViewEventListener):
    def on_hover(self, point, hover_zone):
        window = self.view.window()

        if not window:
            return

        if setting(window, "editor.show_hover", False):
            if hover_zone == sublime.HOVER_TEXT:
                self.view.run_command("pg_smarts_show_hover", {"position": point})

    # (Documents are opened on the main thread; See PgSmartsTextListener.)

    def on_load(self):
        document_open(self.view)

        # Present diagnostics published before the document was open.
        if (window := self.view.window()) and (file_name := self.view.file_name()):
            if path_diagnostics(window.id(), file_name):
                sublime.set_timeout(
                    lambda: schedule_present_diagnostics(window, file_name), 0
                )

    def on_post_save(self):
        # A new file is not open until it's saved: only then it has a name -
        # and there's no 'load' for a file which was created in the editor.
        document_open(self.view)

        document_did_save(self.view)

    def on_pre_save(self):
        window = self.view.window()

        if not window:
            return

        if not setting(window, "editor.format_on_save", False):
            return

        smart = applicable_smart(self.view, method="textDocument/formatting")

        if smart is None:
            return

        # A file being saved for the first time isn't open yet - it's opened
        # once it's saved - and servers can't format a document they don't know.
        if not smart.client.is_document_open(view_file_name_uri(self.view)):
            return

        # Formatting must be done before the document is saved: this waits for
        # the edits - and blocks the UI meanwhile, hence the timeout - then
        # applies them. (Edits applied after the save would leave the file
        # unformatted, and the view dirty.)
        timeout = setting(window, "editor.format_on_save_timeout", 1000) / 1000

        edits: list[smarts_client.LSPTextEdit] = []
        done = threading.Event()

        # Called on a thread of the client - the main thread is waiting.
        def on_result(result: smarts_client.LSPFormattingResult):
            edits.extend(result or [])
            done.set()

        # (The error is logged by the client.)
        def on_error(_: smarts_client.LSPResponseError):
            done.set()

        params: smarts_client.LSPDocumentFormattingParams = {
            "textDocument": view_textDocumentIdentifier(self.view),
            "options": view_formatting_options(self.view),
        }

        smart.client.textDocument_formatting(params, on_result, on_error)

        if not done.wait(timeout):
            plugin_logger.warning(
                f"Format on save timed out after {timeout}s; Will save unformatted"
            )
            return

        if edits:
            self.view.run_command(
                "pg_smarts_apply_edits",
                {
                    "position_encoding": smart.position_encoding(),
                    "edits": edits,
                },
            )

    def on_pre_close(self):
        # Close document symbol quick panel if its view is being closed.
        global _DOCUMENT_SYMBOL_VIEW_ID
        if _DOCUMENT_SYMBOL_VIEW_ID == self.view.id():
            _DOCUMENT_SYMBOL_VIEW_ID = None
            if window := self.view.window():
                window.run_command("hide_overlay")

        _LAST_MODIFIED.pop(self.view.id(), None)

        # Views of the same buffer - clones - show the same document:
        # it's closed when its last view in the window is closed.
        # (Servers are per window, and so are their open documents.)
        if view_clones(self.view):
            return

        if window := self.view.window():
            _DOCUMENT_URI.pop((window.id(), self.view.buffer_id()), None)

        for smart in applicable_smarts(self.view, method="textDocument/didClose"):
            smart.client.textDocument_didClose({
                "textDocument": view_textDocumentIdentifier(self.view),
            })

    def highlight(self, serial: int):
        """
        Highlight references of the symbol at the caret.

        `serial` identifies the selection change which scheduled this highlight.
        """
        # Let Sublime Text highlight selection based on `match_selection` setting.
        if self.view.sel().has_non_empty_region():
            erase_view_highlights(self.view)
            return

        smart = applicable_smart(self.view, method="textDocument/documentHighlight")

        if smart is None:
            return

        position_encoding = smart.position_encoding()

        def on_result(result: smarts_client.LSPDocumentHighlightResult):
            # The caret moved while the server was computing highlights:
            # they're for a symbol which is no longer at the caret.
            if serial != self.pg_smarts_highlight_serial:
                return

            if result is None:
                erase_view_highlights(self.view)
                return

            regions = sorted(
                (
                    range_region(self.view, position_encoding, highlight["range"])
                    for highlight in result
                ),
                key=lambda r: r.begin(),
            )

            # Do nothing if result regions are the same as view regions.
            if regions == view_highlights(self.view):
                return

            set_view_highlights(self.view, regions)

        params = view_textDocumentPositionParams(self.view, position_encoding)

        on_error = panel_error_logger(self.view)

        smart.client.textDocument_documentHighlight(params, on_result, on_error)

    def on_modified(self):
        # Erase highlights immediately.
        erase_view_highlights(self.view)

        _LAST_MODIFIED[self.view.id()] = time.monotonic()

    def on_selection_modified(self):
        # Erase highlights as soon as the caret leaves the highlighted symbol,
        # rather than when the next highlight arrives - after a delay.
        #
        # Highlights are kept while the caret moves within one of them:
        # it's still on the same symbol, so the next highlight is the same,
        # and erasing would only make them flicker.
        regions = view_highlights(self.view)

        if not regions:
            return

        sel = self.view.sel()

        on_highlight = (
            len(sel) > 0
            and sel[0].empty()
            and any(region.contains(sel[0].b) for region in regions)
        )

        if not on_highlight:
            erase_view_highlights(self.view)

    def on_selection_modified_async(self):
        window = self.view.window()

        if not window:
            return

        # Debounce: each selection change supersedes the previous one, so only
        # the last one - once the caret rests for `debounce` - sends a request.
        #
        # The delay must outlast the pause between arrow key presses, or moving
        # line by line highlights every line the caret passes through.
        serial = getattr(self, "pg_smarts_highlight_serial", 0) + 1
        self.pg_smarts_highlight_serial = serial

        def _highlight():
            if serial == self.pg_smarts_highlight_serial and self.view.is_valid():
                self.highlight(serial)

        # Cool-down period: Suppress highlights while actively editing.
        #
        # Wait for cool_down seconds after the last modification, so highlights
        # appear once the user stops typing, without another cursor movement.
        last_modified = _LAST_MODIFIED.get(self.view.id(), 0)
        cool_down = 0.5
        debounce = setting(window, "editor.highlight_references_delay", 300) / 1000
        delay = max(debounce, cool_down - (time.monotonic() - last_modified))

        sublime.set_timeout_async(_highlight, int(delay * 1000))

    def on_query_completions(self, prefix, locations):
        window = self.view.window()

        if window is None:
            return

        if not setting(window, "editor.auto_complete", False):
            return

        smart = applicable_smart(self.view, method="textDocument/completion")

        if smart is None:
            return

        position_encoding = smart.position_encoding()
        point = locations[0]
        completion_list = sublime.CompletionList(flags=kCOMPLETION_FLAGS)

        request_serial = getattr(self, "pg_smarts_completion_request_serial", 0) + 1
        self.pg_smarts_completion_request_serial = request_serial

        def on_result(result: smarts_client.LSPCompletionResult):
            if request_serial != self.pg_smarts_completion_request_serial:
                return

            # result: CompletionItem[] | CompletionList | null
            #  If a CompletionItem[] is provided it is interpreted to be complete. So it is the same as { isIncomplete: false, items }
            if result is None:
                result = []

            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#completionList
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#completionItem
            items = result.get("items") if isinstance(result, dict) else result

            completion_list.set_completions(completions(items))

        def on_error(error: smarts_client.LSPResponseError):
            if request_serial != self.pg_smarts_completion_request_serial:
                return

            if window := self.view.window():
                panel_log_error(window, error)

            completion_list.set_completions([])

        params = cast(
            smarts_client.LSPCompletionParams,
            {
                **view_textDocumentPositionParams(
                    self.view,
                    position_encoding,
                    point,
                ),
                "context": view_completionContext(self.view, smart, point),
            },
        )

        smart.client.textDocument_completion(params, on_result, on_error)

        return completion_list


class PgSmartsListener(sublime_plugin.EventListener):
    def on_load_project(self, window):
        plugin_logger.debug("Load project; Shutdown previous Smarts...")

        shutdown_smarts(window)

        plugin_logger.debug("Load project; Initialize Smarts...")

        initialize_project_smarts(window)

    def on_pre_close_window(self, window):
        plugin_logger.debug("Pre-close window; Shutdown Smarts...")

        shutdown_smarts(window)


# -- PLUGIN LIFECYLE


def plugin_loaded():
    plugin_logger.addHandler(console_logging_handler)
    plugin_logger.setLevel(settings().get("logger.plugin.level", "INFO"))
    plugin_logger.debug("Plugin loaded")

    for window in sublime.windows():
        initialize_project_smarts(window)


def plugin_unloaded():
    plugin_logger.debug("Plugin unloaded")

    # Every window: once unloaded, the plugin loses track of its servers,
    # and any server left running would be orphaned.
    for window in sublime.windows():
        shutdown_smarts(window)

    plugin_logger.removeHandler(console_logging_handler)
