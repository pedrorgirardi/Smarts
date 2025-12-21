import html
import json
import logging
import os
import pprint
import re
import tempfile
import threading
import uuid
from itertools import groupby
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, TypedDict, cast
from urllib.parse import unquote, urlparse
from zipfile import ZipFile

import sublime
import sublime_plugin

from . import smarts_client

# -- Logging

logging_formatter = logging.Formatter(fmt="[{name}] {levelname} {message}", style="{")

# Handler to log on the Console.
console_logging_handler = logging.StreamHandler()
console_logging_handler.setFormatter(logging_formatter)

# Logger used to log 'everything-plugin' - except LSP stuff. (See logger below)
plugin_logger = logging.getLogger(__package__)
plugin_logger.propagate = False

# Logger used by the LSP client.
client_logger = logging.getLogger(f"{__package__}.Client")
client_logger.propagate = False

# ---------------------------------------------------------------------------------------


# -- CONSTANTS

kSETTING_SERVERS = "servers"

kOUTPUT_PANEL_NAME = "Smarts"
kOUTPUT_PANEL_NAME_PREFIXED = f"output.{kOUTPUT_PANEL_NAME}"

kDIAGNOSTICS = "PG_SMARTS_DIAGNOSTICS"
kSMARTS_HIGHLIGHTS = "PG_SMARTS_HIGHLIGHTS"
kSMARTS_COMPLETIONS = "PG_SMARTS_COMPLETIONS"

# https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#diagnosticSeverity
kDIAGNOSTIC_SEVERITY_ERROR = 1
kDIAGNOSTIC_SEVERITY_WARNING = 2
kDIAGNOSTIC_SEVERITY_INFORMATION = 3
kDIAGNOSTIC_SEVERITY_HINT = 4

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
    1: sublime.KIND_ID_KEYWORD,  # Text
    2: sublime.KIND_ID_TYPE,  # Method
    3: sublime.KIND_ID_FUNCTION,  # Function
    4: sublime.KIND_ID_NAMESPACE,  # Constructor
    5: sublime.KIND_ID_TYPE,  # Field
    6: sublime.KIND_ID_TYPE,  # Variable
    7: sublime.KIND_ID_TYPE,  # Class
    8: sublime.KIND_ID_TYPE,  # Interface
    9: sublime.KIND_ID_TYPE,  # Module
    10: sublime.KIND_ID_TYPE,  # Property
    11: sublime.KIND_ID_TYPE,  # Enum
    12: sublime.KIND_ID_TYPE,  # File
    13: sublime.KIND_ID_TYPE,  # Reference
    14: sublime.KIND_ID_TYPE,  # Folder
    15: sublime.KIND_ID_TYPE,  # EnumMember
    16: sublime.KIND_ID_TYPE,  # Constant
    17: sublime.KIND_ID_TYPE,  # Struct
    18: sublime.KIND_ID_TYPE,  # Event
    19: sublime.KIND_ID_TYPE,  # Operator
    20: sublime.KIND_ID_TYPE,  # TypeParameter
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


class PgSmartsServerConfig(TypedDict):
    name: str
    start: List[str]
    applicable_to: List[str]


class PgSmartsInitializeData(TypedDict, total=False):
    name: str
    rootPath: str  # Optional.


class PgSmartsProjectData(TypedDict):
    initialize: List[PgSmartsInitializeData]


class PgSmart(TypedDict):
    uuid: str
    window: int  # Window ID
    config: PgSmartsServerConfig
    client: smarts_client.LanguageServerClient


# ---------------------------------------------------------------------------------------


# -- Global Variables

_SMARTS: List[PgSmart] = []
_SMARTS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------------------


## -- API


def settings() -> sublime.Settings:
    return sublime.load_settings("Smarts.sublime-settings")


def smarts_project_data(
    window: sublime.Window,
) -> Optional[PgSmartsProjectData]:
    if project_data_ := window.project_data():
        return project_data_.get("Smarts")

    return None


def setting(window: sublime.Window, k: str, not_found: Any):
    """
    Get setting k from project's data or Smarts.sublime-settings.

    Returns not_found if setting k is is not set.
    """
    if project_data := smarts_project_data(window):
        try:
            return project_data[k]
        except KeyError:
            return settings().get(k, not_found)

    return settings().get(k, not_found)


def window_project_path(window: sublime.Window) -> Optional[Path]:
    if project_path := window.extract_variables().get("project_path"):
        return Path(project_path)

    return None


def available_servers() -> List[PgSmartsServerConfig]:
    return settings().get(kSETTING_SERVERS, [])


def remove_smarts(uuids: Set[str]):
    plugin_logger.debug(f"Remove Smarts {uuids}")

    global _SMARTS
    with _SMARTS_LOCK:
        _SMARTS = [smart for smart in _SMARTS if smart["uuid"] not in uuids]


def find_smart(uuid: str) -> Optional[PgSmart]:
    with _SMARTS_LOCK:
        for smart in _SMARTS:
            if smart["uuid"] == uuid:
                return smart

    return None


def find_window(id: int) -> Optional[sublime.Window]:
    for window in sublime.windows():
        if window.id() == id:
            return window

    return None


def window_smarts(window: sublime.Window) -> List[PgSmart]:
    """
    Returns Smarts associated with `window`.
    """
    with _SMARTS_LOCK:
        return [smart for smart in _SMARTS if smart["window"] == window.id()]


def window_running_smarts(window: sublime.Window) -> List[PgSmart]:
    """
    Returns Smarts associated with `window` which are not shutdown.
    """
    return [
        smart
        for smart in window_smarts(window)
        if not smart["client"].is_server_shutdown()
    ]


def window_initialized_smarts(window: sublime.Window) -> List[PgSmart]:
    """
    Returns Smarts associated with `window` which are initialized.
    """
    return [
        smart
        for smart in window_running_smarts(window)
        if smart["client"].is_server_initialized()
    ]


def shutdown_smarts(window: sublime.Window):
    shutdown_uuids = set()

    for smart in window_running_smarts(window):
        smart["client"].shutdown()

        shutdown_uuids.add(smart["uuid"])

    remove_smarts(shutdown_uuids)


def initialize_project_smarts(window: sublime.Window):
    """
    Initialize Language Servers configured in a Sublime Project.
    """
    if project_data_ := smarts_project_data(window):
        # It's expected a list of server (dict) with 'name', and 'rootPath' optionally - 'rootPath' can be a relative.
        for initialize_data in project_data_.get("initialize", []):
            rootPath = initialize_data.get("rootPath")

            if rootPath is not None:
                rootPath = Path(rootPath)

                project_path = window_project_path(window)

                if not rootPath.is_absolute() and project_path is not None:
                    rootPath = (project_path / rootPath).resolve()

            window.run_command(
                "pg_smarts_initialize",
                {
                    "server": initialize_data.get("name"),
                    "rootPath": rootPath.as_posix() if rootPath is not None else None,
                },
            )


def view_syntax(view: sublime.View) -> str:
    """
    Returns a sublime-syntax for view.

    A syntax might be something like "Packages/Python/Python.sublime-syntax".
    """
    return view.settings().get("syntax")


def view_applicable(config: PgSmartsServerConfig, view: sublime.View) -> bool:
    """
    Returns True if view is applicable.

    View is applicable if a file is associated and its syntax is contained in the `applicable_to` setting.
    """
    applicable_to = set(config.get("applicable_to", []))

    return view.file_name() is not None and view_syntax(view) in applicable_to


def applicable_smarts(view: sublime.View, method: str) -> List[PgSmart]:
    """
    Returns Smarts applicable to `view`.
    """
    smarts = []

    if window := view.window():
        for smart in window_initialized_smarts(window):
            if not view_applicable(smart["config"], view):
                continue

            if smart["client"].support_method(method):
                smarts.append(smart)

    return smarts


def applicable_smart(view: sublime.View, method: str) -> Optional[PgSmart]:
    """
    Returns the first Smart applicable to `view`, or None.
    """
    if applicable := applicable_smarts(view, method):
        return applicable[0]

    plugin_logger.debug(f"No applicable Smart for '{method}'")

    return None


def text_to_html(s: str) -> str:
    result = html.escape(s)
    result = re.sub(r"\n", "<br/>", result)
    result = re.sub(r"\t", "&nbsp;&nbsp;&nbsp;&nbsp;", result)
    result = re.sub(r" ", "&nbsp;", result)

    return result


def output_panel(window: sublime.Window) -> sublime.View:
    if panel_view := window.find_output_panel(kOUTPUT_PANEL_NAME):
        return panel_view
    else:
        panel_view = window.create_output_panel(kOUTPUT_PANEL_NAME)
        panel_view.settings().set("gutter", False)
        panel_view.settings().set("auto_indent", False)
        panel_view.settings().set("translate_tabs_to_spaces", False)
        panel_view.settings().set("smart_indent", False)
        panel_view.settings().set("indent_to_bracket", False)
        panel_view.settings().set("highlight_line", False)
        panel_view.settings().set("line_numbers", False)
        panel_view.settings().set("scroll_past_end", False)

        return panel_view


def show_output_panel(window: sublime.Window):
    window.run_command(
        "show_panel",
        {
            "panel": kOUTPUT_PANEL_NAME_PREFIXED,
        },
    )


def hide_output_panel(window: sublime.Window):
    window.run_command(
        "hide_panel",
        {
            "panel": kOUTPUT_PANEL_NAME_PREFIXED,
        },
    )


def toggle_output_panel(window: sublime.Window):
    if window.active_panel() == kOUTPUT_PANEL_NAME_PREFIXED:
        hide_output_panel(window)
    else:
        # Create Output Panel if it doesn't exist.
        output_panel(sublime.active_window())

        show_output_panel(window)


def panel_log(window: sublime.Window, text: str, show=False):
    panel_view = output_panel(window)
    panel_view.run_command("insert", {"characters": text})

    if show:
        show_output_panel(window)


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


def show_hover_popup(
    view: sublime.View,
    smart: PgSmart,
    result: Any,
):
    # The result of a hover request.
    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#hover

    result_contents = result["contents"]

    popup_content = []

    if isinstance(result_contents, str):
        popup_content.append(text_to_html(result_contents))

    elif isinstance(result_contents, dict):
        popup_content.append(text_to_html(result_contents["value"]))

    elif isinstance(result_contents, list):
        for x in result_contents:
            if isinstance(x, str):
                popup_content.append(text_to_html(x))

            elif isinstance(x, dict):
                popup_content.append(text_to_html(x["value"]))

    # The popup is shown at the current postion of the caret.
    location = -1

    # An optional range is a range inside a text document
    # that is used to visualize a hover, e.g. by changing the background color.
    if result_range := result.get("range"):
        location = view.text_point(
            result_range["start"]["line"],
            result_range["start"]["character"],
        )

    minihtml = f"""
    <style>
        {kMINIHTML_STYLES}
    </style>
    <body>
        <div class='rounded-lg p-3 bg-background-50 text-foreground text-sm'>{"<br />".join(popup_content)}</div>

        <br />

        <span class='text-sm text-pinkish font-bold'>{smart["client"]._name}</span>
    </body>
    """

    view.show_popup(minihtml, location=location, max_width=860)


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

        <span class='text-sm text-pinkish font-bold'>{smart["client"]._name}</span>
    </body>
    """

    view.show_popup(minihtml, location=-1, max_width=1200)


def severity_name(severity: int):
    if severity == kDIAGNOSTIC_SEVERITY_ERROR:
        return "Error"
    elif severity == kDIAGNOSTIC_SEVERITY_WARNING:
        return "Warning"
    elif severity == kDIAGNOSTIC_SEVERITY_INFORMATION:
        return "Info"
    elif severity == kDIAGNOSTIC_SEVERITY_HINT:
        return "Hint"
    else:
        return f"Unknown {severity}"


def severity_scope(severity: int):
    if severity == kDIAGNOSTIC_SEVERITY_ERROR:
        return "region.redish"
    elif severity == kDIAGNOSTIC_SEVERITY_WARNING:
        return "region.orangish"
    elif severity == kDIAGNOSTIC_SEVERITY_INFORMATION:
        return "region.bluish"
    elif severity == kDIAGNOSTIC_SEVERITY_HINT:
        return "region.purplish"
    else:
        return "invalid"


def severity_annotation_color(view: sublime.View, severity: int) -> str:
    scope = severity_scope(severity)

    style = view.style_for_scope(scope)

    return style.get("foreground", "#ff0000")


def severity_kind(severity: int):
    if severity == kDIAGNOSTIC_SEVERITY_ERROR:
        return (sublime.KIND_ID_COLOR_REDISH, "E", "E")
    elif severity == kDIAGNOSTIC_SEVERITY_WARNING:
        return (sublime.KIND_ID_COLOR_ORANGISH, "W", "W")
    elif severity == kDIAGNOSTIC_SEVERITY_INFORMATION:
        return (sublime.KIND_ID_COLOR_BLUISH, "I", "I")
    elif severity == kDIAGNOSTIC_SEVERITY_HINT:
        return (sublime.KIND_ID_COLOR_PURPLISH, "H", "H")
    else:
        return (sublime.KIND_ID_AMBIGUOUS, "", "")


def range16_to_region(view: sublime.View, range16) -> sublime.Region:
    return sublime.Region(
        view.text_point_utf16(
            range16["start"]["line"],
            range16["start"]["character"],
            clamp_column=True,
        ),
        view.text_point_utf16(
            range16["end"]["line"],
            range16["end"]["character"],
            clamp_column=True,
        ),
    )


def region_to_range16(
    view: sublime.View,
    region: sublime.Region,
) -> smarts_client.LSPRange:
    begin_row, begin_col = view.rowcol_utf16(region.begin())
    end_row, end_col = view.rowcol_utf16(region.end())

    return {
        "start": {
            "line": int(begin_row),
            "character": int(begin_col),
        },
        "end": {
            "line": int(end_row),
            "character": int(end_col),
        },
    }


def diagnostic_quick_panel_item(data) -> sublime.QuickPanelItem:
    path = uri_to_path(data["uri"])
    start_line = data["range"]["start"]["line"] + 1
    start_character = data["range"]["start"]["character"] + 1

    return sublime.QuickPanelItem(
        f"{severity_name(data['severity'])}: {data['message']}",
        kind=severity_kind(data["severity"]),
        annotation=data.get("code", ""),
        details=f"{path}:{start_line}:{start_character}",
    )


def location_quick_panel_item(
    location: smarts_client.LSPLocation,
) -> sublime.QuickPanelItem:
    start_line = location["range"]["start"]["line"] + 1
    start_character = location["range"]["start"]["character"] + 1

    return sublime.QuickPanelItem(
        uri_to_path(location["uri"]),
        annotation=f"{start_line}:{start_character}",
    )


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

    symbol_kind = {
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

    return sublime.QuickPanelItem(
        data["name"],
        annotation=symbol_kind.get(data["kind"], ""),
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
        "languageId": syntax_languageId(view_syntax(view)),
        "version": view.change_count(),
        "text": view.substr(sublime.Region(0, view.size())),
    }


def open_location_jar(window: sublime.Window, location, flags):
    """
    Open JAR `fname` and call `f` with the path of the temporary file.
    """
    fname = uri_to_path(location["uri"])

    dep_jar, dep_filepath = fname.split("::")

    with ZipFile(dep_jar) as jar:
        with jar.open(dep_filepath) as jar_file:
            tmp_path = os.path.join(tempfile.gettempdir(), dep_filepath)

            # Create all parent directories of the temporary file:
            os.makedirs(os.path.dirname(tmp_path), exist_ok=True)

            with open(tmp_path, "w") as tmp_file:
                tmp_file.write(jar_file.read().decode())

            new_location = {
                "uri": path_to_uri(tmp_file.name),
                "range": location["range"],
            }

            open_location(window, cast(smarts_client.LSPLocation, new_location), flags)


def open_location(
    window: sublime.Window,
    location: smarts_client.LSPLocation,
    flags=sublime.ENCODED_POSITION,
):
    fname = uri_to_path(location["uri"])

    if ".jar:" in fname:
        open_location_jar(window, location, flags)
    else:
        row = location["range"]["start"]["line"] + 1
        col = location["range"]["start"]["character"] + 1

        window.open_file(f"{fname}:{row}:{col}", flags)


def capture_view(view: sublime.View) -> Callable[[], None]:
    regions = [region for region in view.sel()]

    viewport_position = view.viewport_position()

    def restore():
        view.sel().clear()

        for region in regions:
            view.sel().add(region)

        view.set_viewport_position(viewport_position, True)

        if window := view.window():
            window.focus_view(view)

    return restore


def capture_viewport_position(view: sublime.View) -> Callable[[], None]:
    viewport_position = view.viewport_position()

    def restore():
        view.set_viewport_position(viewport_position, True)

    return restore


def goto_location(
    window: sublime.Window,
    locations: List[smarts_client.LSPLocation],
    item_builder: Callable[[smarts_client.LSPLocation], sublime.QuickPanelItem],
    on_cancel: Optional[Callable[[], None]] = None,
):
    if len(locations) == 1:
        open_location(window, locations[0])
    else:
        locations = sorted(
            locations,
            key=lambda location: [
                location["uri"],
                location["range"]["start"]["line"],
                location["range"]["start"]["character"],
            ],
        )

        def on_highlight(index):
            open_location(
                window,
                locations[index],
                flags=sublime.ENCODED_POSITION | sublime.TRANSIENT,
            )

        def on_select(index):
            if index == -1:
                if on_cancel:
                    on_cancel()
            else:
                open_location(window, locations[index])

        items = [item_builder(location) for location in locations]

        window.show_quick_panel(
            items,
            on_select=on_select,
            on_highlight=on_highlight,
        )


def goto_diagnostic(
    window: sublime.Window,
    diagnostics: List[PgSmartsDiagnostic],
    on_cancel: Optional[Callable[[], None]] = None,
):
    if len(diagnostics) == 1:
        open_location(window, diagnostics[0])
    else:
        diagnostics = sorted(
            diagnostics,
            key=lambda diagnostic: [
                diagnostic["severity"],
                diagnostic["uri"],
                diagnostic["range"]["start"]["line"],
                diagnostic["range"]["start"]["character"],
            ],
        )

        def on_highlight(index):
            open_location(
                window,
                diagnostics[index],
                flags=sublime.ENCODED_POSITION | sublime.TRANSIENT,
            )

        def on_select(index):
            if index == -1:
                if on_cancel:
                    on_cancel()
            else:
                open_location(window, diagnostics[index])

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
    point=None,
) -> smarts_client.LSPTextDocumentPositionParams:
    """
    A parameter literal used in requests to pass a text document and a position inside that document.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentPositionParams
    """
    default_point = view.sel()[0].begin()

    line, character = view.rowcol_utf16(point or default_point)

    return {
        "textDocument": view_textDocumentIdentifier(view),
        "position": {
            "line": line,
            "character": character,
        },
    }


def syntax_languageId(syntax: str):
    """
    Args:
        syntax:

    Returns:
        the text document's language identifier.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentItem
    """
    if syntax == "Packages/Python/Python.sublime-syntax":
        return "python"
    elif (
        syntax == "Packages/Clojure/Clojure.sublime-syntax"
        or "Packages/Clojure/ClojureScript.sublime-syntax"
        or "Packages/Tutkain/EDN (Tutkain).sublime-syntax"
        or "Packages/Tutkain/Clojure (Tutkain).sublime-syntax"
        or "Packages/Tutkain/ClojureScript (Tutkain).sublime-syntax"
        or "Packages/Tutkain/Clojure Common (Tutkain).sublime-syntax"
        or "Packages/Clojure Sublimed/Clojure (Sublimed).sublime-syntax"
    ):
        return "clojure"
    elif syntax == "Packages/Go/Go.sublime-syntax":
        return "go"
    else:
        return ""


def handle_logTrace(
    window: sublime.Window,
    message: smarts_client.LSPNotificationMessage,
):
    """
    A notification to log the trace of the server’s execution.
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#logTrace
    """

    panel_log(window, f"{pprint.pformat(message)}\n\n")


def handle_window_logMessage(
    window: sublime.Window,
    message: smarts_client.LSPNotificationMessage,
):
    """
    The log message notification is sent from the server to the client
    to ask the client to log a particular message.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#window_logMessage
    """

    message_type = message["params"]["type"]
    message_type = kMESSAGE_TYPE_NAME.get(message_type, message_type)
    message_message = message["params"]["message"]

    panel_log(window, f"{message_message}\n")


def handle_window_showMessage(
    window: sublime.Window,
    message: smarts_client.LSPNotificationMessage,
):
    """
    The show message notification is sent from a server to a client
    to ask the client to display a particular message in the user interface.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#window_showMessage
    """

    message_type = message["params"]["type"]
    message_type = kMESSAGE_TYPE_NAME.get(message_type, message_type)
    message_message = message["params"]["message"]

    panel_log(window, f"{message_message}\n", show=True)


def handle_textDocument_publishDiagnostics(
    window: sublime.Window,
    smart: PgSmart,
    message: smarts_client.LSPNotificationMessage,
):
    """
    Diagnostics notifications are sent from the server to the client to signal results of validation runs.

    Diagnostics are “owned” by the server so it is the server’s responsibility to clear them if necessary.

    When a file changes it is the server’s responsibility to re-compute diagnostics and push them to the client.
    If the computed set is empty it has to push the empty array to clear former diagnostics.
    Newly pushed diagnostics always replace previously pushed diagnostics.
    There is no merging that happens on the client side.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#publishDiagnosticsParams
    """

    params = cast(smarts_client.LSPPublishDiagnosticsParams, message["params"])

    # Including URI to a Diagnostic make it equivalent to a Location - `uri` and `range`.
    # (Anything that works with a Location also works with this Diagnostic.)
    diagnostics: List[PgSmartsDiagnostic] = [
        {
            "uri": params["uri"],
            **diagnostic,
        }
        for diagnostic in params["diagnostics"]
    ]

    # URI to Diagnostics.
    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#diagnostic
    uri_diagnostics = window.settings().get(kDIAGNOSTICS, {})
    uri_diagnostics[params["uri"]] = diagnostics

    window.settings().set(kDIAGNOSTICS, uri_diagnostics)

    fname = unquote(urlparse(params["uri"]).path)

    if view := window.find_open_file(fname):
        # Persists document diagnostics.
        view.settings().set(kDIAGNOSTICS, diagnostics)

        diagnostics_status = []

        # Clear annotations for all severity levels.
        for s in [
            kDIAGNOSTIC_SEVERITY_ERROR,
            kDIAGNOSTIC_SEVERITY_WARNING,
            kDIAGNOSTIC_SEVERITY_INFORMATION,
            kDIAGNOSTIC_SEVERITY_HINT,
        ]:
            view.erase_regions(f"{kDIAGNOSTICS}_SEVERITY_{s}")

        def severity_key(diagnostic):
            # The diagnostic's severity.
            # To avoid interpretation mismatches when a server is used with different clients it is highly recommended
            # that servers always provide a severity value.
            # If omitted, it’s recommended for the client to interpret it as an Error severity.
            return diagnostic.get("severity", 1)

        for k, g in groupby(sorted(diagnostics, key=severity_key), key=severity_key):
            severity_regions = []
            severity_annotations = []
            severity_diagnostics = list(g)

            diagnostics_status.append(
                f"{severity_name(k)}: {len(severity_diagnostics)}"
            )

            for d in severity_diagnostics:
                # Regions by Severity
                severity_regions.append(
                    range16_to_region(view, d["range"]),
                )

                # Annotations (minihtml) by Severity
                severity_annotations.append(
                    f'<span style="font-size:0.8em">{d["message"]}</span>',
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


def handle_notification(
    smart_uuid: str,
    notification: smarts_client.LSPNotificationMessage,
):
    smart = find_smart(smart_uuid)

    if not smart:
        return

    window = find_window(smart["window"])

    if not window:
        return

    message_method = notification.get("method")

    if message_method == "$/logTrace":
        handle_logTrace(window, notification)

    elif message_method == "window/logMessage":
        handle_window_logMessage(window, notification)

    elif message_method == "window/showMessage":
        handle_window_showMessage(window, notification)

    elif message_method == "textDocument/publishDiagnostics":
        handle_textDocument_publishDiagnostics(window, smart, notification)

    else:
        panel_log(window, f"Unhandled Notification: {pprint.pformat(notification)}\n\n")


# -- INPUT HANDLERS


class ServerInputHandler(sublime_plugin.ListInputHandler):
    def __init__(self, items):
        self.items = items

    def placeholder(self):
        return "Server"

    def name(self):
        return "server"

    def list_items(self):
        return self.items


class SmartsInputHandler(sublime_plugin.ListInputHandler):
    def placeholder(self):
        return "Server"

    def name(self):
        return "smart_uuid"

    def list_items(self):
        items = []

        for smart in window_running_smarts(sublime.active_window()):
            smart_uuid = smart["uuid"]
            smart_server_name = smart["client"]._name

            items.append((f"{smart_server_name} {smart_uuid}", smart_uuid))

        return items


# -- COMMANDS


class PgSmartsInitializeCommand(sublime_plugin.WindowCommand):
    def input(self, args):
        if "server" not in args:
            return ServerInputHandler(
                sorted(
                    [server_config["name"] for server_config in available_servers()],
                )
            )

    def run(self, server: str, rootPath=None):
        if rootPath is None:
            rootPath = self.window.folders()[0] if self.window.folders() else None

            if rootPath is None:
                plugin_logger.error("Can't initialize server without a rootPath")
                return

        rootPath = Path(rootPath)

        rootUri = rootPath.as_uri()

        workspaceFolders = [{"name": rootPath.name, "uri": rootUri}]

        params = {
            "processId": os.getpid(),
            "clientInfo": {
                "name": "Smarts",
                "version": "0.1.0",
            },
            # The rootPath of the workspace. Is null if no folder is open.
            # Deprecated in favour of rootUri.
            "rootPath": rootPath.as_posix(),
            # The rootUri of the workspace. Is null if no folder is open.
            # If both rootPath and rootUri are set rootUri wins.
            # Deprecated in favour of workspaceFolders.
            "rootUri": rootUri,
            # The workspace folders configured in the client when the server starts.
            "workspaceFolders": workspaceFolders,
            "trace": "verbose",
            "capabilities": {
                # Client support for textDocument/didOpen, textDocument/didChange
                # and textDocument/didClose notifications is mandatory in the protocol
                # and clients can not opt out supporting them.
                #
                # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_synchronization
                "textDocument": {
                    "synchronization": {
                        # Whether text document synchronization supports dynamic registration.
                        "dynamicRegistration": False,
                        # Documents are synced by always sending the full content of the document.
                        "change": 1,
                    },
                    "hover": {
                        "contentFormat": ["plaintext"],
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
                }
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
            logger=client_logger,
            name=server_config["name"],
            server_args=server_config["start"],
            on_logTrace=_on_receive_notification,
            on_window_logMessage=_on_receive_notification,
            on_window_showMessage=_on_receive_notification,
            on_textDocument_publishDiagnostics=_on_receive_notification,
        )

        global _SMARTS
        with _SMARTS_LOCK:
            _SMARTS.append({
                "uuid": smart_uuid,
                "window": self.window.id(),
                "config": server_config,
                "client": client,
            })

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.window:
                    panel_log_error(window, error)
                return

            # Notify the server about 'open documents'.
            # (Check if a view's syntax is valid for the server.)
            for view in self.window.views():
                if view_applicable(server_config, view):
                    params: smarts_client.LSPDidOpenTextDocumentParams = {
                        "textDocument": view_text_document_item(view),
                    }

                    client.textDocument_didOpen(params)

        client.initialize(params, callback)


class PgSmartsShutdownCommand(sublime_plugin.WindowCommand):
    def input(self, args):
        if "smart_uuid" not in args:
            return SmartsInputHandler()

    def run(self, smart_uuid):
        if smart := find_smart(smart_uuid):
            smart["client"].shutdown()
            remove_smarts({smart_uuid})


class PgSmartsToggleOutputPanelCommand(sublime_plugin.WindowCommand):
    def run(self):
        toggle_output_panel(self.window)


class PgSmartsClearOutputPanelCommand(sublime_plugin.WindowCommand):
    def run(self):
        panel_view = output_panel(self.window)
        panel_view.run_command("select_all")
        panel_view.run_command("left_delete")


class PgSmartsStatusCommand(sublime_plugin.WindowCommand):
    def run(self):
        minihtml = ""

        for smart in window_smarts(self.window):
            client = smart["client"]

            minihtml += f"<span class='text-foreground font-bold'>{client._name}<br /><br /></span>"

            # -- UUID
            minihtml += f"<span class='text-foreground-07 text-sm'>UUID: {smart['uuid']}</span><br />"

            # -- Status
            minihtml += f"<span class='text-foreground-07 text-sm'>Status: {client.server_status().name}</span><br />"

            # -- PID
            minihtml += f"<span class='text-foreground-07 text-sm'>PID: {client._server_process.pid if client._server_process else None}</span><br /><br />"

            # -- Info
            minihtml += "<span class='text-sm font-bold'>Info:</span><br /><br />"

            if server_info := client._server_info:
                minihtml += f"<code class='text-sm' style='display: block; white-space: pre;'>{html.escape(json.dumps(server_info, indent=2))}</code>"
            else:
                minihtml += "-"

            minihtml += "<br /><br />"

            # -- Capabilities
            minihtml += (
                "<span class='text-sm font-bold'>Capabilities:</span><br /><br />"
            )

            if server_capabilities := client._server_capabilities:
                minihtml += f"<code class='text-sm' style='display: block; white-space: pre;'>{html.escape(json.dumps(server_capabilities, indent=2))}</code>"
            else:
                minihtml += "-"

            minihtml += "<br />---<br /><br />"

        if not minihtml:
            return

        sheet = self.window.new_html_sheet(
            "Smarts Status",
            f"""
            <style>
                {kMINIHTML_STYLES}
            </style>
            <body>
                {minihtml}
            </body>
            """,
            sublime.SEMI_TRANSIENT | sublime.ADD_TO_SELECTION,
        )

        self.window.focus_sheet(sheet)


class PgSmartsGotoDefinition(sublime_plugin.TextCommand):
    def run(self, _):
        smart = applicable_smart(self.view, method="textDocument/definition")

        if not smart:
            return

        params = view_textDocumentPositionParams(self.view)

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)
                return

            result = response.get("result")

            if not result:
                return

            restore_view = capture_view(self.view)

            locations = [result] if isinstance(result, dict) else result

            if window := self.view.window():
                goto_location(
                    window,
                    cast(List[smarts_client.LSPLocation], locations),
                    location_quick_panel_item,
                    on_cancel=restore_view,
                )

        smart["client"].textDocument_definition(params, callback)


class PgSmartsGotoReference(sublime_plugin.TextCommand):
    def run(self, _):
        smart = applicable_smart(self.view, method="textDocument/references")

        if not smart:
            return

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)
                return

            result = response.get("result")

            if not result:
                return

            restore_view = capture_view(self.view)

            if window := self.view.window():
                goto_location(
                    window,
                    result,
                    location_quick_panel_item,
                    on_cancel=restore_view,
                )

        params = {
            "context": {
                "includeDeclaration": False,
            },
            **view_textDocumentPositionParams(self.view),
        }

        smart["client"].textDocument_references(params, callback)


class PgSmartsGotoDocumentDiagnostic(sublime_plugin.TextCommand):
    def run(self, _):
        restore_view = capture_view(self.view)

        diagnostics = self.view.settings().get(kDIAGNOSTICS, [])

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

        diagnostics = [
            diagnostic
            for diagnostics in self.window.settings().get(kDIAGNOSTICS, {}).values()
            for diagnostic in diagnostics
        ]

        goto_diagnostic(
            self.window,
            diagnostics,
            on_cancel=on_cancel,
        )


class PgSmartsGotoDocumentSymbol(sublime_plugin.TextCommand):
    def run(self, _):
        smart = applicable_smart(self.view, method="textDocument/documentSymbol")

        if not smart:
            return

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)
                return

            # Document Symbols Request
            # DocumentSymbol[] | SymbolInformation[] | null
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentSymbol
            if result := response.get("result"):
                restore_viewport_position = capture_viewport_position(self.view)

                def on_highlight(index):
                    data = result[index]

                    show_at_center_range = None

                    # Represents information about programming constructs like variables, classes, interfaces etc.
                    # @deprecated use DocumentSymbol or WorkspaceSymbol instead.
                    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#symbolInformation
                    if location := data.get("location"):
                        show_at_center_range = location["range"]

                    # The range that should be selected and revealed when this symbol is being
                    # picked, e.g. the name of a function. Must be contained by the `range`.
                    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#documentSymbol
                    else:
                        show_at_center_range = data["selectionRange"]

                    show_at_center_region = range16_to_region(
                        self.view,
                        show_at_center_range,
                    )

                    self.view.sel().clear()
                    self.view.sel().add(show_at_center_region)

                    self.view.show_at_center(show_at_center_region)

                def on_select(index):
                    if index == -1:
                        restore_viewport_position()

                    else:
                        data = result[index]

                        selected_range = None

                        if location := data.get("location"):
                            selected_range = location["range"]
                        else:
                            selected_range = data["selectionRange"]

                        selected_region = range16_to_region(
                            self.view,
                            selected_range,
                        )

                        self.view.sel().clear()
                        self.view.sel().add(selected_region)

                        show_at_center_region = sublime.Region(
                            selected_region.end(),
                            selected_region.end(),
                        )

                        self.view.show_at_center(show_at_center_region)

                quick_panel_items = [
                    document_symbol_quick_panel_item(data) for data in result
                ]

                self.view.window().show_quick_panel(
                    quick_panel_items,
                    on_select,
                    on_highlight=on_highlight,
                )

        params = {
            "textDocument": view_textDocumentIdentifier(self.view),
        }

        smart["client"].textDocument_documentSymbol(params, callback)


# WIP
class PgSmartsGotoWorkspaceSymbol(sublime_plugin.WindowCommand):
    def run(self):
        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                panel_log_error(self.window, error)
                return

            # Workspace Symbols Request
            # SymbolInformation[] | WorkspaceSymbol[] | null
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#workspace_symbol
            if result := response.get("result"):
                restore_view = None

                if view := self.window.active_view():
                    restore_view = capture_view(view)

                def on_highlight(index):
                    open_location(
                        self.window,
                        result[index]["location"],
                        flags=sublime.ENCODED_POSITION | sublime.TRANSIENT,
                    )

                def on_select(index):
                    if index == -1:
                        if restore_view:
                            restore_view()
                    else:
                        open_location(
                            self.window,
                            result[index]["location"],
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

        # TODO: Support multiple Smarts
        for smart in window_running_smarts(self.window):
            if smart["client"].support_method("workspace/symbol"):
                smart["client"].workspace_symbol(params, callback)
                break


class PgSmartsSelectRanges(sublime_plugin.TextCommand):
    def run(self, _, ranges):
        self.view.sel().clear()

        for r in ranges:
            self.view.sel().add(range16_to_region(self.view, r))

        self.view.show(self.view.sel())


class PgSmartsSelectCommand(sublime_plugin.TextCommand):
    def run(self, _):
        locations = self.view.settings().get(kSMARTS_HIGHLIGHTS)

        if not locations:
            return

        self.view.sel().clear()

        for loc in locations:
            self.view.sel().add(range16_to_region(self.view, loc["range"]))

        self.view.show(self.view.sel())


class PgSmartsJumpCommand(sublime_plugin.TextCommand):
    def run(self, _, movement):
        locations = self.view.settings().get(kSMARTS_HIGHLIGHTS)

        if not locations:
            return

        locations = sorted(
            locations,
            key=lambda location: [
                location["range"]["start"]["line"],
                location["range"]["start"]["character"],
            ],
        )

        trampoline = self.view.sel()[0]

        jump_loc_index = None

        for index, loc in enumerate(locations):
            r = range16_to_region(self.view, loc["range"])

            if r.contains(trampoline.begin()) or r.contains(trampoline.end()):
                if movement == "back":
                    jump_loc_index = max([0, index - 1])
                elif movement == "forward":
                    jump_loc_index = min([index + 1, len(locations) - 1])

                break

        if jump_loc_index is not None:
            jump_region = range16_to_region(
                self.view, locations[jump_loc_index]["range"]
            )

            self.view.sel().clear()
            self.view.sel().add(jump_region)

            self.view.show(jump_region)


class PgSmartsShowHoverCommand(sublime_plugin.TextCommand):
    def run(self, _, position=None):
        smart = applicable_smart(self.view, method="textDocument/hover")

        if not smart:
            return

        position = position or self.view.sel()[0].begin()

        params = view_textDocumentPositionParams(self.view, position)

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)
                return

            if result := response.get("result"):
                show_hover_popup(self.view, smart, result)

        smart["client"].textDocument_hover(params, callback)


class PgSmartsShowSignatureHelpCommand(sublime_plugin.TextCommand):
    def run(self, _, position=None):
        smart = applicable_smart(self.view, method="textDocument/signatureHelp")

        if not smart:
            return

        position = position or self.view.sel()[0].begin()

        params: smarts_client.LSPSignatureHelpParams = {
            **view_textDocumentPositionParams(self.view, position),
            "context": {
                "triggerKind": 1,  # Invoked manually
                "isRetrigger": False,
            },
        }

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)
                return

            if result := response.get("result"):
                show_signature_help_popup(self.view, smart, result)

        smart["client"].textDocument_signatureHelp(params, callback)


class PgSmartsFormatDocumentCommand(sublime_plugin.TextCommand):
    def run(self, _):
        smart = applicable_smart(self.view, method="textDocument/formatting")

        if not smart:
            return

        params: smarts_client.LSPDocumentFormattingParams = {
            "textDocument": view_textDocumentIdentifier(self.view),
            "options": {
                "tabSize": self.view.settings().get("tab_size"),
                "insertSpaces": True,
                "insertFinalNewline": None,
                "trimTrailingWhitespace": None,
                "trimFinalNewlines": None,
            },
        }

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)
                return

            if textEdits := response.get("result"):
                self.view.run_command(
                    "pg_smarts_apply_edits",
                    {
                        "edits": textEdits,
                    },
                )

        smart["client"].textDocument_formatting(params, callback)


class PgSmartsFormatSelectionCommand(sublime_plugin.TextCommand):
    def run(self, _, region=None):
        smart = applicable_smart(self.view, method="textDocument/rangeFormatting")

        if not smart:
            return

        if region is None:
            for r in self.view.sel():
                if not r.empty():
                    self.view.run_command(
                        "pg_smarts_format_selection",
                        {
                            "region": [r.a, r.b],
                        },
                    )
            return

        region = sublime.Region(region[0], region[1])

        params = {
            "textDocument": view_textDocumentIdentifier(self.view),
            "range": region_to_range16(self.view, region),
            "options": {
                "tabSize": self.view.settings().get("tab_size"),
                "insertSpaces": True,
                "insertFinalNewline": None,
                "trimTrailingWhitespace": None,
                "trimFinalNewlines": None,
            },
        }

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)
                return

            if edits := response.get("result"):
                self.view.run_command(
                    "pg_smarts_apply_edits",
                    {
                        "edits": edits,
                    },
                )

        smart["client"].textDocument_rangeFormatting(params, callback)


class PgSmartsApplyEditsCommand(sublime_plugin.TextCommand):
    def run(self, edit, edits):
        for e in edits:
            edit_region = range16_to_region(self.view, e["range"])
            edit_new_text = e["newText"]

            self.view.replace(edit, edit_region, edit_new_text)


## -- Listeners


class PgSmartsTextListener(sublime_plugin.TextChangeListener):
    def on_text_changed_async(self, changes):
        view = self.buffer.primary_view()

        view_file_name = view.file_name()

        if not view_file_name:
            return

        for smart in applicable_smarts(view, method="textDocument/didChange"):
            language_client = smart["client"]

            textDocumentSync = smarts_client.textDocumentSyncOptions(
                language_client._server_capabilities.get("textDocumentSync")
                if language_client._server_capabilities
                else None
            )

            # The document that did change.
            # The version number points to the version
            # after all provided content changes have been applied.
            #
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#versionedTextDocumentIdentifier
            textDocument: smarts_client.LSPVersionedTextDocumentIdentifier = {
                "uri": path_to_uri(view_file_name),
                "version": view.change_count(),
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
            contentChanges: List[smarts_client.LSPTextDocumentContentChangeEvent] = []

            # Full
            # Documents are synced by always sending the full content of the document.
            if textDocumentSync["change"] == 1:
                contentChanges = [
                    {
                        "text": view.substr(sublime.Region(0, view.size())),
                    }
                ]

            # Incremental
            # Documents are synced by sending the full content on open.
            # After that only incremental updates to the document are sent.
            elif textDocumentSync["change"] == 2:
                contentChanges = []

                for change in changes:
                    contentChanges.append({
                        "range": {
                            "start": {
                                "line": change.a.row,
                                "character": change.a.col_utf16,
                            },
                            "end": {
                                "line": change.b.row,
                                "character": change.b.col_utf16,
                            },
                        },
                        "rangeLength": change.len_utf16,
                        "text": change.str,
                    })

            params: smarts_client.LSPDidChangeTextDocumentParams = {
                "textDocument": textDocument,
                "contentChanges": contentChanges,
            }

            language_client.textDocument_didChange(params)


class PgSmartsViewListener(sublime_plugin.ViewEventListener):
    def on_hover(self, point, hover_zone):
        window = self.view.window()

        if not window:
            return

        if setting(window, "editor.show_hover", False):
            if hover_zone == sublime.HOVER_TEXT:
                self.view.run_command("pg_smarts_show_hover", {"position": point})

    def on_load_async(self):
        for smart in applicable_smarts(self.view, method="textDocument/didOpen"):
            smart["client"].textDocument_didOpen({
                "textDocument": view_text_document_item(self.view),
            })

    def on_pre_save_async(self):
        window = self.view.window()

        if not window:
            return

        if setting(window, "editor.format_on_save", False):
            self.view.run_command("pg_smarts_format_document")

    def on_pre_close(self):
        for smart in applicable_smarts(self.view, method="textDocument/didClose"):
            smart["client"].textDocument_didClose({
                "textDocument": view_textDocumentIdentifier(self.view),
            })

    def erase_highlights(self):
        self.view.erase_regions(kSMARTS_HIGHLIGHTS)

        self.view.settings().erase(kSMARTS_HIGHLIGHTS)

    def highlight(self):
        smart = applicable_smart(self.view, method="textDocument/documentHighlight")

        if not smart:
            return

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)
                return

            result = response.get("result")

            if not result:
                self.erase_highlights()
                return

            regions = [
                range16_to_region(self.view, location["range"]) for location in result
            ]

            # Do nothing if result regions are the same as view regions.
            if regions_ := self.view.get_regions(kSMARTS_HIGHLIGHTS):
                if regions == regions_:
                    return

            highlight_references = False

            if window := self.view.window():
                highlight_references = setting(
                    window,
                    "editor.highlight_references",
                    False,
                )

            self.view.add_regions(
                kSMARTS_HIGHLIGHTS,
                regions,
                scope="comment",
                icon="",
                flags=sublime.DRAW_NO_FILL if highlight_references else sublime.HIDDEN,
            )

            self.view.settings().set(kSMARTS_HIGHLIGHTS, result)

        params = view_textDocumentPositionParams(self.view)

        smart["client"].textDocument_documentHighlight(params, callback)

    def on_modified(self):
        # Erase highlights immediately.
        self.erase_highlights()

    def on_selection_modified_async(self):
        window = self.view.window()

        if not window:
            return

        highlighter = getattr(self, "pg_smarts_highlighter", None)

        if highlighter and highlighter.is_alive():
            highlighter.cancel()

            self.pg_smarts_highlighter = threading.Timer(0.1, self.highlight)
            self.pg_smarts_highlighter.start()
        else:
            self.highlight()
            self.pg_smarts_highlighter = threading.Timer(0.1, self.highlight)
            self.pg_smarts_highlighter.start()

    def on_query_completions(self, prefix, locations):
        if window := self.view.window():
            if not setting(window, "editor.auto_complete", False):
                return None

        cached_completion_items = self.view.settings().get(kSMARTS_COMPLETIONS, None)

        if cached_completion_items is not None:
            self.view.settings().erase(kSMARTS_COMPLETIONS)

            completions: List[sublime.CompletionItem] = []

            for item in cached_completion_items:
                item = cast(smarts_client.LSPCompletionItem, item)

                # The label of this completion item.
                # The label property is also by default the text that is inserted when selecting this completion.
                label = item.get("label") or ""

                annotation = item.get("detail") or ""

                #  A string that should be inserted into a document when selecting this completion.
                # When omitted the label is used as the insert text for this item.
                insert_text = item.get("insertText") or label

                completion_kind = item.get("kind") or 0

                kind = kCOMPLETION_ITEM_KIND.get(
                    completion_kind, sublime.KIND_ID_AMBIGUOUS
                )

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

        smart = applicable_smart(self.view, method="textDocument/completion")

        if not smart:
            return None

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)
                return

            # result: CompletionItem[] | CompletionList | null
            #  If a CompletionItem[] is provided it is interpreted to be complete. So it is the same as { isIncomplete: false, items }
            result = response.get("result", [])

            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#completionList
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#completionItem
            items = result.get("items") if isinstance(result, dict) else result

            # Store completions in view settings and trigger auto_complete.
            self.view.settings().set(kSMARTS_COMPLETIONS, items)

            sublime.set_timeout(lambda: self.view.run_command("auto_complete"), 0)

        params = view_textDocumentPositionParams(self.view, locations[0])

        smart["client"].textDocument_completion(params, callback)

        # Return empty list immediately; completions will be shown when response arrives.
        return []


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

    client_logger.addHandler(console_logging_handler)
    client_logger.setLevel(settings().get("logger.client.level", "INFO"))

    plugin_logger.debug("Plugin loaded")

    initialize_project_smarts(sublime.active_window())


def plugin_unloaded():
    plugin_logger.debug("Plugin unloaded")

    shutdown_smarts(sublime.active_window())

    plugin_logger.removeHandler(console_logging_handler)
    client_logger.removeHandler(console_logging_handler)
