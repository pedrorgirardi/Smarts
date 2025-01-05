import logging
import os
import pprint
import re
import tempfile
import threading
import uuid
from itertools import groupby
from pathlib import Path
from typing import Any, Callable, List, Optional, Set, TypedDict
from urllib.parse import unquote, urlparse
from zipfile import ZipFile

import sublime
import sublime_plugin

from . import smarts_client
from .smarts_typing import (
    SmartsProjectData,
    SmartsServerConfig,
)

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

kMINIHTML_STYLES = """
.m-0 {
    margin: 0px;
}

.p-0 {
    padding: 0px;
}

.font-bold {
    font-weight: bold;
}

.text-foreground-07 {
    color: color(var(--foreground) alpha(0.7));
}
"""


# ---------------------------------------------------------------------------------------


class Smart(TypedDict):
    uuid: str
    window: int  # Window ID
    client: smarts_client.LanguageServerClient


# ---------------------------------------------------------------------------------------


# -- Global Variables

_SMARTS: List[Smart] = []


# ---------------------------------------------------------------------------------------


## -- API


def settings() -> sublime.Settings:
    return sublime.load_settings("Smarts.sublime-settings")


def smarts_project_data(window: sublime.Window) -> Optional[SmartsProjectData]:
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


def available_servers() -> List[SmartsServerConfig]:
    return settings().get(kSETTING_SERVERS, [])


def add_smart(window: sublime.Window, client: smarts_client.LanguageServerClient):
    global _SMARTS
    _SMARTS.append({
        "uuid": str(uuid.uuid4()),
        "window": window.id(),
        "client": client,
    })

    return _SMARTS


def remove_smarts(uuids: Set[str]):
    plugin_logger.debug(f"Remove Smarts {uuids}")

    global _SMARTS
    _SMARTS = [smart for smart in _SMARTS if smart["uuid"] not in uuids]


def find_smart(uuid: str) -> Optional[Smart]:
    for smart in _SMARTS:
        if smart["uuid"] == uuid:
            return smart

    return None


def window_smarts(window: sublime.Window) -> List[Smart]:
    """
    Returns Smarts associated with `window`.
    """
    return [smart for smart in _SMARTS if smart["window"] == window.id()]


def window_running_smarts(window: sublime.Window) -> List[Smart]:
    """
    Returns Smarts associated with `window` which are not shutdown.
    """
    return [
        smart
        for smart in window_smarts(window)
        if not smart["client"]._server_shutdown.is_set()
    ]


def view_smarts(view: sublime.View) -> List[Smart]:
    window = view.window()

    if window is None:
        return []

    smarts = []

    for smart in window_running_smarts(window):
        smart_client = smart["client"]

        if not smart_client._server_initialized:
            continue

        smarts.append(smart)

    return smarts


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


def view_applicable(config: SmartsServerConfig, view: sublime.View) -> bool:
    """
    Returns True if view is applicable.

    View is applicable if a file is associated and its syntax is contained in the `applicable_to` setting.
    """
    applicable_to = set(config.get("applicable_to", []))

    return view.file_name() is not None and view_syntax(view) in applicable_to


def applicable_smarts(view: sublime.View, method: str) -> List[Smart]:
    """
    Returns Smarts applicable to view.
    """
    smarts = []

    for smart in view_smarts(view):
        smart_client = smart["client"]

        if not view_applicable(smart_client._config, view):
            continue

        if server_capabilities := smart_client._server_capabilities:
            if smarts_client.support_method(server_capabilities, method):
                smarts.append(smart)

    return smarts


def applicable_smart(view: sublime.View, method: str) -> Optional[Smart]:
    """
    Returns the first Smart applicable to view, or None.
    """
    if applicable := applicable_smarts(view, method):
        return applicable[0]

    plugin_logger.debug(f"No applicable Smart for '{method}'")

    return None


def text_to_html(s: str) -> str:
    html = re.sub(r"\n", "<br/>", s)
    html = re.sub(r"\t", "&nbsp;&nbsp;&nbsp;&nbsp;", html)
    html = re.sub(r" ", "&nbsp;", html)

    return html


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


def show_hover_popup(view: sublime.View, smart: Smart, result: Any):
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

    minihtml = "<br /><br />".join(popup_content)
    minihtml += f"<br /><br /><span>{smart['client']._config['name']}</span>"

    view.show_popup(minihtml, location=location, max_width=860)


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


def severity_annotation_color(view: sublime.View, severity: int) -> Optional[str]:
    scope = severity_scope(severity)

    style = view.style_for_scope(scope)

    return style.get("foreground")


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


def region_to_range16(view: sublime.View, region: sublime.Region) -> dict:
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


def diagnostic_quick_panel_item(diagnostic_item: dict) -> sublime.QuickPanelItem:
    line = diagnostic_item["range"]["start"]["line"] + 1
    character = diagnostic_item["range"]["start"]["character"] + 1

    return sublime.QuickPanelItem(
        f"{severity_name(diagnostic_item['severity'])}: {diagnostic_item['message']}",
        details=f"{diagnostic_item.get('code', '')}",
        annotation=f"{line}:{character}",
        kind=severity_kind(diagnostic_item["severity"]),
    )


def document_symbol_quick_panel_item(data: dict) -> sublime.QuickPanelItem:
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


def location_quick_panel_item(location: dict):
    start_line = location["range"]["start"]["line"] + 1
    start_character = location["range"]["start"]["character"] + 1

    return sublime.QuickPanelItem(
        uri_to_path(location["uri"]),
        annotation=f"{start_line}:{start_character}",
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

            open_location(window, new_location, flags)


def open_location(window: sublime.Window, location, flags=sublime.ENCODED_POSITION):
    fname = uri_to_path(location["uri"])

    if ".jar:" in fname:
        open_location_jar(window, location, flags)
    else:
        row = location["range"]["start"]["line"] + 1
        col = location["range"]["start"]["character"] + 1

        window.open_file(f"{fname}:{row}:{col}", flags)


def capture_view(view: sublime.View) -> Callable:
    regions = [region for region in view.sel()]

    viewport_position = view.viewport_position()

    def restore():
        view.sel().clear()

        for region in regions:
            view.sel().add(region)

        view.window().focus_view(view)

        view.set_viewport_position(viewport_position, True)

    return restore


def capture_viewport_position(view: sublime.View) -> Callable:
    viewport_position = view.viewport_position()

    def restore():
        view.set_viewport_position(viewport_position, True)

    return restore


def goto_location(window, locations, on_cancel=None):
    if len(locations) == 1:
        open_location(window, locations[0])
    else:
        locations = sorted(
            locations,
            key=lambda location: [
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

        window.show_quick_panel(
            [location_quick_panel_item(location) for location in locations],
            on_select=on_select,
            on_highlight=on_highlight,
        )


# -- LSP


def view_textDocumentIdentifier(view: sublime.View) -> smarts_client.LSPTextDocumentIdentifier:
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

    line, character = view.rowcol(point or default_point)

    return {
        "textDocument": view_textDocumentIdentifier(view),
        "position": {
            "line": line,
            "character": character,
        },
    }


def syntax_languageId(syntax):
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


def handle_logTrace(window, message):
    """
    A notification to log the trace of the server’s execution.
    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#logTrace
    """

    panel_log(window, f"{pprint.pformat(message)}\n\n")


def handle_window_logMessage(window, message):
    """
    The log message notification is sent from the server to the client
    to ask the client to log a particular message.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#window_logMessage
    """

    message_type = message["params"]["type"]
    message_type = kMESSAGE_TYPE_NAME.get(message_type, message_type)
    message_message = message["params"]["message"]

    panel_log(window, f"{message_message}\n")


def handle_window_showMessage(window, message):
    """
    The show message notification is sent from a server to a client
    to ask the client to display a particular message in the user interface.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#window_showMessage
    """

    message_type = message["params"]["type"]
    message_type = kMESSAGE_TYPE_NAME.get(message_type, message_type)
    message_message = message["params"]["message"]

    panel_log(window, f"{message_message}\n", show=True)


def handle_textDocument_publishDiagnostics(window, message):
    """
    Diagnostics notifications are sent from the server to the client to signal results of validation runs.

    Diagnostics are “owned” by the server so it is the server’s responsibility to clear them if necessary.

    When a file changes it is the server’s responsibility to re-compute diagnostics and push them to the client.
    If the computed set is empty it has to push the empty array to clear former diagnostics.
    Newly pushed diagnostics always replace previously pushed diagnostics.
    There is no merging that happens on the client side.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#publishDiagnosticsParams
    """
    params = message["params"]

    fname = unquote(urlparse(params["uri"]).path)

    if view := window.find_open_file(fname):
        diagnostics = params["diagnostics"]

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
            return diagnostic["severity"]

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


def on_receive_message(
    window: sublime.Window,
    server: str,
    message: smarts_client.LSPMessage,
):
    message_method = message.get("method")

    if message_method == "$/logTrace":
        handle_logTrace(window, message)

    elif message_method == "window/logMessage":
        handle_window_logMessage(window, message)

    elif message_method == "window/showMessage":
        handle_window_showMessage(window, message)

    elif message_method == "textDocument/publishDiagnostics":
        handle_textDocument_publishDiagnostics(window, message)

    else:
        panel_log(window, f"{pprint.pformat(message)}\n\n")


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
            smart_server_name = smart["client"]._config["name"]

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

        client = smarts_client.LanguageServerClient(
            logger=client_logger,
            config=server_config,
            notification_handler=lambda message: on_receive_message(
                self.window, server, message
            ),
        )

        add_smart(self.window, client)

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.window:
                    panel_log_error(window, error)
            else:
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

            status = "Stopped" if client._server_shutdown.is_set() else "Running"

            minihtml += (
                f"<strong>{client._config['name']} ({status})</strong><br /><br />"
            )

            if client._server_initialized:
                minihtml += "<ul class='m-0'>"

                if server_capabilities := client._server_capabilities:
                    for k, v in server_capabilities.items():
                        minihtml += (
                            f"<li><span class='text-foreground-07'>{k}:</span> {v}</li>"
                        )

                minihtml += "</ul><br /><br />"

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

            result = response.get("result")

            if not result:
                return

            restore_view = capture_view(self.view)

            locations = [result] if isinstance(result, dict) else result

            goto_location(self.view.window(), locations, on_cancel=restore_view)

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

            result = response.get("result")

            if not result:
                return

            restore_view = capture_view(self.view)

            goto_location(self.view.window(), result, on_cancel=restore_view)

        params = {
            **view_textDocumentPositionParams(self.view),
            **{
                "context": {
                    "includeDeclaration": False,
                },
            },
        }

        smart["client"].textDocument_references(params, callback)


class PgSmartsGotoDocumentDiagnostic(sublime_plugin.TextCommand):
    def run(self, _):
        restore_viewport_position = capture_viewport_position(self.view)

        diagnostics = sorted(
            self.view.settings().get(kDIAGNOSTICS, []),
            key=lambda diagnostic: [
                diagnostic["range"]["start"]["line"],
                diagnostic["range"]["start"]["character"],
            ],
        )

        def on_highlight(index):
            diagnostic_region = range16_to_region(
                self.view, diagnostics[index]["range"]
            )

            self.view.sel().clear()
            self.view.sel().add(diagnostic_region)

            self.view.show_at_center(diagnostic_region)

        def on_select(index):
            if index == -1:
                restore_viewport_position()

            else:
                region = range16_to_region(self.view, diagnostics[index]["range"])

                self.view.sel().clear()
                self.view.sel().add(region)

                self.view.show_at_center(region)

        quick_panel_items = [
            diagnostic_quick_panel_item(diagnostic) for diagnostic in diagnostics
        ]

        self.view.window().show_quick_panel(
            quick_panel_items,
            on_select,
            on_highlight=on_highlight,
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

            if result := response.get("result"):
                restore_viewport_position = capture_viewport_position(self.view)

                def on_highlight(index):
                    data = result[index]

                    show_at_center_range = None

                    if location := data.get("location"):
                        show_at_center_range = location["range"]
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

                        show_at_center_region = sublime.Region(
                            selected_region.end(),
                            selected_region.end(),
                        )

                        self.view.sel().clear()
                        self.view.sel().add(show_at_center_region)

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
    def run(self, _):
        smart = applicable_smart(self.view, method="textDocument/hover")

        if not smart:
            return

        params = view_textDocumentPositionParams(self.view)

        def callback(response: smarts_client.LSPResponseMessage):
            if error := response.get("error"):
                if window := self.view.window():
                    panel_log_error(window, error)

            if result := response["result"]:
                show_hover_popup(self.view, smart, result)

        smart["client"].textDocument_hover(params, callback)


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

            if textEdits := response.get("result"):
                self.view.run_command(
                    "pg_smarts_apply_edits",
                    {
                        "edits": textEdits,
                    },
                )

        smart["client"].textDocument_formatting(params, callback)


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
    def on_load_async(self):
        for smart in applicable_smarts(self.view, method="textDocument/didOpen"):
            smart["client"].textDocument_didOpen({
                "textDocument": view_text_document_item(self.view),
            })

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

            self.view.add_regions(
                kSMARTS_HIGHLIGHTS,
                regions,
                scope="region.cyanish",
                icon="",
                flags=sublime.DRAW_NO_FILL,
            )

            self.view.settings().set(kSMARTS_HIGHLIGHTS, result)

        params = view_textDocumentPositionParams(self.view)

        smart["client"].textDocument_documentHighlight(params, callback)

    def on_modified(self):
        # Erase highlights immediately.
        self.erase_highlights()

    def on_selection_modified_async(self):
        if highlighter := getattr(self, "pg_smarts_highlighter", None):
            highlighter.cancel()

        window = self.view.window()

        if not window:
            return

        if not setting(window, "editor.highlight_references", False):
            return

        self.pg_smarts_highlighter = threading.Timer(0.3, self.highlight)
        self.pg_smarts_highlighter.start()

    def on_hover(self, point, hover_zone):
        window = self.view.window()

        if not window:
            return

        if not setting(window, "editor.show_hover", False):
            return

        if hover_zone == sublime.HOVER_TEXT:
            smart = applicable_smart(self.view, method="textDocument/hover")

            if not smart:
                return

            params = view_textDocumentPositionParams(self.view, point)

            def callback(response: smarts_client.LSPResponseMessage):
                if error := response.get("error"):
                    panel_log_error(window, error)

                if result := response["result"]:
                    show_hover_popup(self.view, smart, result)

            smart["client"].textDocument_hover(params, callback)


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

    plugin_logger.debug("Plugin loaded; Initialize Smarts...")

    initialize_project_smarts(sublime.active_window())


def plugin_unloaded():
    plugin_logger.debug("Plugin unloaded; Shutdown Smarts...")

    shutdown_smarts(sublime.active_window())

    plugin_logger.removeHandler(console_logging_handler)
    client_logger.removeHandler(console_logging_handler)
