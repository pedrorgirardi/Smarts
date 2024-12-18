import json
import logging
import os
import pprint
import re
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from queue import Queue
from urllib.parse import unquote, urlparse
from zipfile import ZipFile
from itertools import groupby

import sublime
import sublime_plugin

# -- Logging

logging_formatter = logging.Formatter(fmt="[{name}] {levelname} {message}", style="{")

console_logging_handler = logging.StreamHandler()
console_logging_handler.setFormatter(logging_formatter)

plugin_logger = logging.getLogger(__package__)
plugin_logger.propagate = False

client_logger = logging.getLogger(f"{__package__}.Client")
client_logger.propagate = False


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


# -- Global Variables

_STARTED_SERVERS = {}


## -- API


def settings():
    return sublime.load_settings("Smarts.sublime-settings")


def project_data(window):
    if project_data_ := window.project_data():
        return project_data_.get("Smarts")

    return None


def window_project_path(window):
    if project_path := window.extract_variables().get("project_path"):
        return Path(project_path)

    return None


def window_rootPath(window):
    return window.folders()[0] if window.folders() else None


def available_servers():
    return settings().get(kSETTING_SERVERS, [])


def started_servers(rootPath):
    return _STARTED_SERVERS.get(rootPath)


def started_servers_values(rootPath):
    return _STARTED_SERVERS.get(rootPath, {}).values()


def started_server(rootPath, server):
    if started_servers_ := started_servers(rootPath):
        return started_servers_.get(server)


def add_server(rootPath, started_server):
    server_name = started_server["config"]["name"]

    global _STARTED_SERVERS

    if started_servers_ := _STARTED_SERVERS.get(rootPath):
        started_servers_[server_name] = started_server
    else:
        _STARTED_SERVERS[rootPath] = {server_name: started_server}


def initialize_project_servers(window):
    """
    Initialize Language Servers configured in a Sublime Project.
    """
    if project_data_ := project_data(window):
        project_path = window_project_path(window)

        # It's expected a list of server (dict) with 'name', and 'rootPath' optionally.
        for server in project_data_.get("initialize", []):
            rootPath = server.get("rootPath")

            if rootPath is not None:
                rootPath = Path(rootPath)

                if not rootPath.is_absolute() and project_path is not None:
                    rootPath = (project_path / rootPath).resolve()

            window.run_command(
                "pg_smarts_initialize",
                {
                    "server": server.get("name"),
                    "rootPath": rootPath.as_posix() if rootPath is not None else None,
                },
            )


def view_syntax(view) -> str:
    """
    Returns syntax for view.

    A syntax might be something like "Packages/Python/Python.sublime-syntax".
    """
    return view.settings().get("syntax")


def view_applicable(config, view):
    """
    Returns True if view is applicable.

    View is applicable if its syntax is contained in the `applicable_to` setting.
    """
    applicable_to = set(config.get("applicable_to", []))

    return view.file_name() and view_syntax(view) in applicable_to


def applicable_servers(view):
    """
    Returns started servers applicable to view.
    """
    servers = []

    if not view.window():
        return servers

    for started_server in started_servers_values(window_rootPath(view.window())):
        if view_applicable(started_server["config"], view):
            servers.append(started_server)

    return servers


def applicable_server(view):
    """
    Returns the first started server applicable to view, or None.
    """
    if applicable := applicable_servers(view):
        return applicable[0]


def text_to_html(s: str) -> str:
    html = re.sub(r"\n", "<br/>", s)
    html = re.sub(r"\t", "&nbsp;&nbsp;&nbsp;&nbsp;", html)
    html = re.sub(r" ", "&nbsp;", html)

    return html


def output_panel(window) -> sublime.View:
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


def show_output_panel(window):
    window.run_command(
        "show_panel",
        {
            "panel": kOUTPUT_PANEL_NAME_PREFIXED,
        },
    )


def hide_output_panel(window):
    window.run_command(
        "hide_panel",
        {
            "panel": kOUTPUT_PANEL_NAME_PREFIXED,
        },
    )


def toggle_output_panel(window):
    if window.active_panel() == kOUTPUT_PANEL_NAME_PREFIXED:
        hide_output_panel(window)
    else:
        # Create Output Panel if it doesn't exist.
        output_panel(sublime.active_window())

        show_output_panel(window)


def panel_log(window, text, show=False):
    panel_view = output_panel(window)
    panel_view.run_command("insert", {"characters": text})

    if show:
        show_output_panel(window)


def show_hover_popup(view: sublime.View, result):
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

    view.show_popup(minihtml, location=location, max_width=860)


def severity_name(severity):
    if severity == 1:
        return "Error"
    elif severity == 2:
        return "Warning"
    elif severity == 3:
        return "Info"
    elif severity == 4:
        return "Hint"
    else:
        return f"Unknown {severity}"


def severity_scope(severity):
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


def severity_annotation_color(view, severity):
    scope = severity_scope(severity)

    style = view.style_for_scope(scope)

    return style.get("foreground")


def severity_kind(severity):
    if severity == 1:
        return (sublime.KIND_ID_COLOR_REDISH, "E", "E")
    elif severity == 2:
        return (sublime.KIND_ID_COLOR_ORANGISH, "W", "W")
    elif severity == 3:
        return (sublime.KIND_ID_COLOR_BLUISH, "I", "I")
    elif severity == 4:
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


def view_text_document_item(view):
    """
    An item to transfer a text document from the client to the server.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentItem
    """
    return {
        "uri": path_to_uri(view.file_name()),
        "languageId": syntax_languageId(view_syntax(view)),
        "version": view.change_count(),
        "text": view.substr(sublime.Region(0, view.size())),
    }


def open_location_jar(window, location, flags):
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


def open_location(window, location, flags=sublime.ENCODED_POSITION):
    fname = uri_to_path(location["uri"])

    if ".jar:" in fname:
        open_location_jar(window, location, flags)
    else:
        row = location["range"]["start"]["line"] + 1
        col = location["range"]["start"]["character"] + 1

        window.open_file(f"{fname}:{row}:{col}", flags)


def capture_view(view):
    regions = [region for region in view.sel()]

    viewport_position = view.viewport_position()

    def restore():
        view.sel().clear()

        for region in regions:
            view.sel().add(region)

        view.window().focus_view(view)

        view.set_viewport_position(viewport_position, True)

    return restore


def capture_viewport_position(view):
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


def view_textDocumentParams(view):
    return {
        "textDocument": {
            "uri": Path(view.file_name()).as_uri(),
        }
    }


def view_textDocumentPositionParams(view, point=None):
    """
    A parameter literal used in requests to pass a text document and a position inside that document.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentPositionParams
    """
    default_point = view.sel()[0].begin()

    line, character = view.rowcol(point or default_point)

    return {
        "textDocument": {
            "uri": path_to_uri(view.file_name()),
        },
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


def on_send_message(window, server, message):
    # panel_log(window, f'{server} {message.get("method")}\n')
    pass


def on_receive_message(window, server, message):
    message_method = message.get("method")

    if message_method == "$/logTrace":
        handle_logTrace(window, message)

    elif message_method == "window/logMessage":
        handle_window_logMessage(window, message)

    elif message_method == "window/showMessage":
        handle_window_showMessage(window, message)

    elif message_method == "textDocument/publishDiagnostics":
        handle_textDocument_publishDiagnostics(window, message)

    # Log unhanled methods
    elif message_method:
        panel_log(window, f"{pprint.pformat(message)}\n\n")


# -- CLIENT


class LanguageServerClient:
    def __init__(
        self,
        logger,
        server_name,
        server_start,
        on_send=None,
        on_receive=None,
    ):
        self._logger = logger
        self._server_name = server_name
        self._server_start = server_start
        self._server_process = None
        self._server_shutdown = threading.Event()
        self._server_initialized = False
        self._server_info = None
        self._server_capabilities = None
        self._on_send = on_send
        self._on_receive = on_receive
        self._send_queue = Queue(maxsize=1)
        self._receive_queue = Queue(maxsize=1)
        self._reader = None
        self._writer = None
        self._handler = None
        self._request_callback = {}
        self._open_documents = set()

    def capabilities_textDocumentSync(self):
        """
        Defines how text documents are synced.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentSyncOptions
        """
        if capabilities := self._server_capabilities:
            # If omitted it defaults to `TextDocumentSyncKind.None`.
            textDocumentSync = capabilities.get(
                "textDocumentSync",
                {
                    "change": 0,
                },
            )

            # Is either a detailed structure defining each notification
            # or for backwards compatibility the TextDocumentSyncKind number.
            if not isinstance(textDocumentSync, dict):
                textDocumentSync = {
                    "change": textDocumentSync,
                }

            return textDocumentSync

    def _read(self, out, n):
        remaining = n

        chunks = []

        while remaining > 0:
            chunk = out.read(remaining)

            # End of file or stream
            if not chunk:
                break

            chunks.append(chunk)

            remaining -= len(chunk)

        return b"".join(chunks)

    def _start_reader(self):
        self._logger.debug(f"[{self._server_name}] Reader started 🟢")

        while not self._server_shutdown.is_set():
            out = self._server_process.stdout

            # The base protocol consists of a header and a content part (comparable to HTTP).
            # The header and content part are separated by a ‘\r\n’.
            #
            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#baseProtocol

            # -- HEADER

            headers = {}

            while True:
                line = out.readline().decode("ascii").strip()

                if line == "":
                    break

                k, v = line.split(": ", 1)

                headers[k] = v

            # -- CONTENT

            if content_length := headers.get("Content-Length"):
                content = self._read(out, int(content_length)).decode("utf-8").strip()

                try:
                    message = json.loads(content)

                    # Enqueue message; Blocks if queue is full.
                    self._receive_queue.put(message)

                except json.JSONDecodeError:
                    # The effect of not being able to decode a message,
                    # is that an 'in-flight' request won't have its callback called.
                    self._logger.error(f"Failed to decode message: {content}")

        self._logger.debug(f"[{self._server_name}] Reader stopped 🔴")

    def _start_writer(self):
        self._logger.debug(f"[{self._server_name}] Writer started 🟢")

        while (message := self._send_queue.get()) is not None:
            try:
                content = json.dumps(message)

                header = f"Content-Length: {len(content)}\r\n\r\n"

                try:
                    encoded = header.encode("ascii") + content.encode("utf-8")
                    self._server_process.stdin.write(encoded)
                    self._server_process.stdin.flush()
                except BrokenPipeError as e:
                    self._logger.error(
                        f"{self._server_name} - Can't write to server's stdin: {e}"
                    )

                if self._on_send:
                    try:
                        self._on_send(message)
                    except Exception:
                        self._logger.exception("Error handling sent message")

            finally:
                self._send_queue.task_done()

        # 'None Task' is complete.
        self._send_queue.task_done()

        self._logger.debug(f"[{self._server_name}] Writer stopped 🔴")

    def _start_handler(self):
        self._logger.debug(f"[{self._server_name}] Handler started 🟢")

        while (message := self._receive_queue.get()) is not None:  # noqa
            if self._on_receive:
                try:
                    self._on_receive(message)
                except Exception:
                    self._logger.exception("Error handling received message")

            if request_id := message.get("id"):
                if callback := self._request_callback.get(request_id):
                    try:
                        callback(message)
                    except Exception:
                        self._logger.exception(
                            f"{self._server_name} - Request callback error"
                        )
                    finally:
                        del self._request_callback[request_id]

            self._receive_queue.task_done()

        # 'None Task' is complete.
        self._receive_queue.task_done()

        self._logger.debug(f"[{self._server_name}] Handler stopped 🔴")

    def _put(self, message, callback=None, on_put=None):
        # Drop message if server is not ready - unless it's an initization message.
        if not self._server_initialized and not message["method"] == "initialize":
            self._logger.debug(
                f"Server {self._server_name} is not initialized; Will drop {message['method']}"
            )

            return

        self._send_queue.put(message)

        if on_put:
            on_put()

        if message_id := message.get("id"):
            # A mapping of request ID to callback.
            #
            # callback will be called once the response for the request is received.
            #
            # callback might not be called if there's an error reading the response,
            # or the server never returns a response.
            self._request_callback[message_id] = callback

    def initialize(self, params, callback):
        """
        The initialize request is sent as the first request from the client to the server.
        Until the server has responded to the initialize request with an InitializeResult,
        the client must not send any additional requests or notifications to the server.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initialize
        """

        if self._server_initialized:
            return

        self._logger.debug(f"Initialize {self._server_name} {self._server_start}")

        self._server_process = subprocess.Popen(
            self._server_start,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        self._logger.info(
            f"{self._server_name} is up and running; PID {self._server_process.pid}"
        )

        # Thread responsible for handling received messages.
        self._handler = threading.Thread(
            name="Handler",
            target=self._start_handler,
            daemon=True,
        )
        self._handler.start()

        # Thread responsible for sending/writing messages.
        self._writer = threading.Thread(
            name="Writer",
            target=self._start_writer,
            daemon=True,
        )
        self._writer.start()

        # Thread responsible for reading messages.
        self._reader = threading.Thread(
            name="Reader",
            target=self._start_reader,
            daemon=True,
        )
        self._reader.start()

        def _callback(response):
            self._server_initialized = True
            self._server_capabilities = response.get("result").get("capabilities")
            self._server_info = response.get("result").get(
                "serverInfo",
                {
                    "name": "-",
                    "version": "-",
                },
            )

            self._put({
                "jsonrpc": "2.0",
                "method": "initialized",
                "params": {},
            })

            callback(response)

        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "initialize",
                "params": params,
            },
            _callback,
        )

    def shutdown(self, callback=None):
        """
        The shutdown request is sent from the client to the server.
        It asks the server to shut down,
        but to not exit (otherwise the response might not be delivered correctly to the client).
        There is a separate exit notification that asks the server to exit.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown
        """

        self._logger.info(f"Shutdown {self._server_name}")

        def _callback(message):
            self.exit()

            if callback:
                callback(message)

        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "shutdown",
                "params": {},
            },
            _callback,
        )

    def exit(self):
        """
        A notification to ask the server to exit its process.
        The server should exit with success code 0 if the shutdown request has been received before;
        otherwise with error code 1.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#exit
        """
        self._logger.info(f"Exit {self._server_name}")

        self._put({
            "jsonrpc": "2.0",
            "method": "exit",
            "params": {},
        })

        self._server_shutdown.set()

        # Enqueue `None` to signal that workers must stop:
        self._send_queue.put(None)
        self._receive_queue.put(None)

        returncode = None

        try:
            returncode = self._server_process.wait(30)
        except subprocess.TimeoutExpired:
            # Explicitly kill the process if it did not terminate.
            self._server_process.kill()

            returncode = self._server_process.wait()

        self._logger.info(
            f"{self._server_name} terminated with returncode {returncode}"
        )

    def textDocument_didOpen(self, params):
        """
        The document open notification is sent from the client to the server
        to signal newly opened text documents.

        The document’s content is now managed by the client
        and the server must not try to read the document’s content using the document’s Uri.

        Open in this sense means it is managed by the client.
        It doesn’t necessarily mean that its content is presented in an editor.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didOpen
        """

        # An open notification must not be sent more than once without a corresponding close notification send before.
        # This means open and close notification must be balanced and the max open count for a particular textDocument is one.
        textDocument_uri = params["textDocument"]["uri"]

        if textDocument_uri in self._open_documents:
            return

        self._put(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": params,
            },
            on_put=lambda: self._open_documents.add(textDocument_uri),
        )

    def textDocument_didClose(self, params):
        """
        The document close notification is sent from the client to the server
        when the document got closed in the client.

        The document’s master now exists where
        the document’s Uri points to (e.g. if the document’s Uri is a file Uri the master now exists on disk).

        As with the open notification the close notification
        is about managing the document’s content.
        Receiving a close notification doesn’t mean that the document was open in an editor before.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didClose
        """

        textDocument_uri = params["textDocument"]["uri"]

        # A close notification requires a previous open notification to be sent.
        if textDocument_uri not in self._open_documents:
            return

        self._put(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didClose",
                "params": params,
            },
            on_put=lambda: self._open_documents.remove(textDocument_uri),
        )

    def textDocument_didChange(self, params):
        """
        The document change notification is sent from the client to the server to signal changes to a text document.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didChange
        """

        # Before a client can change a text document it must claim
        # ownership of its content using the textDocument/didOpen notification.
        if params["textDocument"]["uri"] not in self._open_documents:
            return

        self._put({
            "jsonrpc": "2.0",
            "method": "textDocument/didChange",
            "params": params,
        })

    def textDocument_hover(self, params, callback):
        """
        The hover request is sent from the client to the server to request
        hover information at a given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_hover
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/hover",
                "params": params,
            },
            callback,
        )

    def textDocument_definition(self, params, callback):
        """
        The go to definition request is sent from the client to the server
        to resolve the definition location of a symbol at a given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_definition
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/definition",
                "params": params,
            },
            callback,
        )

    def textDocument_references(self, params, callback):
        """
        The references request is sent from the client to the server
        to resolve project-wide references for the symbol denoted by the given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_references
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/references",
                "params": params,
            },
            callback,
        )

    def textDocument_documentHighlight(self, params, callback):
        """
        The document highlight request is sent from the client to
        the server to resolve document highlights for a given text document position.

        For programming languages this usually highlights all references to the symbol scoped to this file.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentHighlight
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/documentHighlight",
                "params": params,
            },
            callback,
        )

    def textDocument_documentSymbol(self, params, callback):
        """
        The document symbol request is sent from the client to the server.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_documentSymbol
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/documentSymbol",
                "params": params,
            },
            callback,
        )

    def textDocument_formatting(self, params, callback):
        """
        The document formatting request is sent from the client to the server to format a whole document.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_formatting
        """
        self._put(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/formatting",
                "params": params,
            },
            callback,
        )


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


# -- COMMANDS


class PgSmartsInitializeCommand(sublime_plugin.WindowCommand):
    def input(self, args):
        if "server" not in args:
            available_servers_names = [config["name"] for config in available_servers()]

            return ServerInputHandler(sorted(available_servers_names))

    def run(self, server, rootPath=None):
        available_servers_indexed = {
            config["name"]: config for config in available_servers()
        }

        config = available_servers_indexed.get(server)

        client = LanguageServerClient(
            logger=client_logger,
            server_name=server,
            server_start=config["start"],
            on_send=lambda message: on_send_message(self.window, server, message),
            on_receive=lambda message: on_receive_message(self.window, server, message),
        )

        if rootPath is None:
            rootPath = self.window.folders()[0] if self.window.folders() else None

        rootPath = Path(rootPath) if rootPath is not None else None

        rootUri = rootPath.as_uri() if rootPath else None

        workspaceFolders = (
            [{"name": rootPath.name, "uri": rootUri}] if rootPath else None
        )

        params = {
            "processId": os.getpid(),
            "clientInfo": {
                "name": "Smarts",
                "version": "0.1.0",
            },
            # The rootPath of the workspace. Is null if no folder is open.
            # Deprecated in favour of rootUri.
            "rootPath": rootPath.as_posix() if rootPath is not None else None,
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

        def callback(response):
            # Notify the server about current views.
            # (Check if a view's syntax is valid for the server.)
            for view in self.window.views():
                if view.file_name() and view_applicable(config, view):
                    client.textDocument_didOpen({
                        "textDocument": view_text_document_item(view),
                    })

        client.initialize(params, callback)

        rootPath = window_rootPath(self.window)

        add_server(
            rootPath,
            {
                "config": config,
                "client": client,
            },
        )


class PgSmartsShutdownCommand(sublime_plugin.WindowCommand):
    def input(self, args):
        if "server" not in args:
            rootPath = window_rootPath(self.window)

            started_servers_ = started_servers(rootPath)

            return ServerInputHandler(
                sorted(started_servers_.keys()) if started_servers_ else []
            )

    def run(self, server):
        rootPath = window_rootPath(self.window)

        if started_server_ := started_server(rootPath, server):
            started_server_["client"].shutdown()

            global _STARTED_SERVERS
            del _STARTED_SERVERS[rootPath][server]


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

        label_class = "text-foreground-07"

        for rootPath, started_servers in _STARTED_SERVERS.items():
            minihtml += f"<span class='font-bold {label_class}'>Root path:</span> <span>{rootPath}</span><br /><br />"

            for started_server in started_servers.values():
                client: LanguageServerClient = started_server["client"]

                textDocumentSync = client.capabilities_textDocumentSync()

                # Open and close notifications are sent to the server.
                # If omitted open close notifications should not be sent.
                textDocumentSync_openClose = textDocumentSync.get("openClose", "-")

                change: int = textDocumentSync.get("change", 0)

                # Change notifications are sent to the server.
                textDocumentSync_change = {
                    0: "0 - None",
                    1: "1 - Full",
                    2: "2 - Incremental",
                }.get(
                    change,
                    change,
                )

                documentSymbolProvider = client._server_capabilities.get(
                    "documentSymbolProvider", "-"
                )
                documentHighlightProvider = client._server_capabilities.get(
                    "documentHighlightProvider", "-"
                )

                # Server name & version
                minihtml += f"<strong>{client._server_info['name']}, version {client._server_info['version']}</strong><br /><br />"

                minihtml += "<ul class='m-0'>"

                minihtml += f"<li><span class='{label_class}'>openClose:</span> {textDocumentSync_openClose}</li>"
                minihtml += f"<li><span class='{label_class}'>change:</span> {textDocumentSync_change}</li>"
                minihtml += f"<li><span class='{label_class}'>documentSymbolProvider:</span> {documentSymbolProvider}</li>"
                minihtml += f"<li><span class='{label_class}'>documentHighlightProvider:</span> {documentHighlightProvider}</li>"

                minihtml += "</ul><br /><br />"

        if not minihtml:
            return

        sheet = self.window.new_html_sheet(
            "Servers",
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
        for started_server in started_servers_values(
            window_rootPath(self.view.window())
        ):
            config = started_server["config"]
            client = started_server["client"]

            if view_applicable(config, self.view):

                def callback(response):
                    if error := response.get("error"):
                        panel_log(
                            self.view.window(),
                            f"Error: {error.get('code')} {error.get('message')}\n",
                            show=True,
                        )

                    result = response.get("result")

                    if not result:
                        return

                    restore_view = capture_view(self.view)

                    locations = [result] if isinstance(result, dict) else result

                    goto_location(self.view.window(), locations, on_cancel=restore_view)

                client.textDocument_definition(
                    view_textDocumentPositionParams(self.view),
                    callback,
                )


class PgSmartsGotoReference(sublime_plugin.TextCommand):
    def run(self, _):
        for started_server in started_servers_values(
            window_rootPath(self.view.window())
        ):
            config = started_server["config"]
            client = started_server["client"]

            if view_applicable(config, self.view):

                def callback(response):
                    if error := response.get("error"):
                        panel_log(
                            self.view.window(),
                            f"Error: {error.get('code')} {error.get('message')}\n",
                            show=True,
                        )

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

                client.textDocument_references(params, callback)


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
        applicable_server_ = applicable_server(self.view)

        if not applicable_server_:
            return

        def callback(response):
            if error := response.get("error"):
                panel_log(
                    self.view.window(),
                    f"Error: {error.get('code')} {error.get('message')}\n",
                    show=True,
                )

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

        params = view_textDocumentParams(self.view)

        applicable_server_["client"].textDocument_documentSymbol(params, callback)


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
        if applicable_server_ := applicable_server(self.view):
            params = view_textDocumentPositionParams(self.view)

            def callback(response):
                if error := response.get("error"):
                    panel_log(
                        self.view.window(),
                        f"Error: {error.get('code')} {error.get('message')}\n",
                        show=True,
                    )

                if result := response["result"]:
                    show_hover_popup(self.view, result)

            applicable_server_["client"].textDocument_hover(params, callback)


class PgSmartsFormatDocumentCommand(sublime_plugin.TextCommand):
    def run(self, _):
        if not self.view.file_name():
            return

        if applicable_server_ := applicable_server(self.view):
            settings = self.view.settings()

            params = {
                "textDocument": {
                    "uri": path_to_uri(self.view.file_name()),
                },
                "options": {
                    "tabSize": settings.get("tab_size"),
                    "insertSpaces": True,
                },
            }

            def callback(response):
                if error := response.get("error"):
                    panel_log(
                        self.view.window(),
                        f"Error: {error.get('code')} {error.get('message')}\n",
                        show=True,
                    )

                if textEdits := response.get("result"):
                    self.view.run_command(
                        "pg_smarts_apply_edits",
                        {
                            "edits": textEdits,
                        },
                    )

            applicable_server_["client"].textDocument_formatting(params, callback)


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

        if not view.file_name():
            return

        language_client = None

        if applicable_server_ := applicable_server(view):
            language_client = applicable_server_["client"]

        if not language_client:
            return

        textDocumentSync = language_client.capabilities_textDocumentSync()

        if not textDocumentSync:
            return

        # The document that did change.
        # The version number points to the version
        # after all provided content changes have been applied.
        #
        # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#versionedTextDocumentIdentifier
        textDocument = {
            "uri": path_to_uri(view.file_name()),
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
        contentChanges = None

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

        language_client.textDocument_didChange({
            "textDocument": textDocument,
            "contentChanges": contentChanges,
        })


class PgSmartsViewListener(sublime_plugin.ViewEventListener):
    def on_load_async(self):
        if not self.view.file_name():
            return

        rootPath = window_rootPath(self.view.window())

        if started_servers_ := started_servers(rootPath):
            for started_server in started_servers_.values():
                config = started_server["config"]
                client = started_server["client"]

                if view_applicable(config, self.view):
                    client.textDocument_didOpen({
                        "textDocument": view_text_document_item(self.view),
                    })

    def on_pre_close(self):
        if not self.view.file_name():
            return

        # When the window is closed, there's no window 'attached' to view.
        if not self.view.window():
            return

        rootPath = window_rootPath(self.view.window())

        if started_servers_ := started_servers(rootPath):
            for started_server in started_servers_.values():
                config = started_server["config"]
                client = started_server["client"]

                if view_applicable(config, self.view):
                    client.textDocument_didClose(
                        {
                            "textDocument": {
                                "uri": path_to_uri(self.view.file_name()),
                            },
                        },
                    )

    def erase_highlights(self):
        self.view.erase_regions(kSMARTS_HIGHLIGHTS)

        self.view.settings().erase(kSMARTS_HIGHLIGHTS)

    def highlight(self):
        applicable_server_ = applicable_server(self.view)

        if not applicable_server_:
            return

        def callback(response):
            if error := response.get("error"):
                panel_log(
                    self.view.window(),
                    f"Error: {error.get('code')} {error.get('message')}\n",
                    show=True,
                )

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

        applicable_server_["client"].textDocument_documentHighlight(params, callback)

    def on_modified(self):
        # Erase highlights immediately.
        self.erase_highlights()

    def on_selection_modified_async(self):
        if highlighter := getattr(self, "pg_smarts_highlighter", None):
            highlighter.cancel()

        if not settings().get("editor.highlight_references"):
            return

        self.pg_smarts_highlighter = threading.Timer(0.3, self.highlight)
        self.pg_smarts_highlighter.start()

    def on_hover(self, point, hover_zone):
        if not settings().get("editor.show_hover"):
            return

        if hover_zone == sublime.HOVER_TEXT:
            for started_server in started_servers_values(
                window_rootPath(self.view.window())
            ):
                config = started_server["config"]
                client = started_server["client"]

                if view_applicable(config, self.view):
                    params = view_textDocumentPositionParams(self.view, point)

                    def callback(response):
                        if error := response.get("error"):
                            panel_log(
                                self.view.window(),
                                f"Error: {error.get('code')} {error.get('message')}\n",
                                show=True,
                            )

                        if result := response["result"]:
                            show_hover_popup(self.view, result)

                    client.textDocument_hover(params, callback)


class PgSmartsListener(sublime_plugin.EventListener):
    def shutdown_servers(self, window):
        def shutdown(rootPath, started_servers):
            for started_server in started_servers.values():
                started_server["client"].shutdown()

            global _STARTED_SERVERS
            del _STARTED_SERVERS[rootPath]

        rootPath = window_rootPath(window)

        if started_servers_ := started_servers(rootPath):
            threading.Thread(
                name="PreCloseShutdown",
                target=lambda: shutdown(rootPath, started_servers_),
                daemon=True,
            ).start()

    def on_pre_close_window(self, window):
        self.shutdown_servers(window)

    def on_load_project(self, window):
        initialize_project_servers(window)

    def on_pre_close_project(self, window):
        self.shutdown_servers(window)


# -- PLUGIN LIFECYLE


def plugin_loaded():
    plugin_logger.addHandler(console_logging_handler)
    plugin_logger.setLevel(settings().get("logger.plugin.level", "INFO"))

    client_logger.addHandler(console_logging_handler)
    client_logger.setLevel(settings().get("logger.client.level", "INFO"))

    plugin_logger.debug("loaded plugin")

    initialize_project_servers(sublime.active_window())


def plugin_unloaded():
    if _STARTED_SERVERS:

        def shutdown_servers():
            for rootPath, servers in _STARTED_SERVERS.items():
                for server_name, started_server in servers.items():
                    started_server["client"].shutdown()

        threading.Thread(
            name="Unloaded",
            target=lambda: shutdown_servers(),
            daemon=True,
        ).start()

    plugin_logger.debug("unloaded plugin")

    plugin_logger.removeHandler(console_logging_handler)
    client_logger.removeHandler(console_logging_handler)
