import json
import logging
import os
import re
import subprocess
import threading
import uuid
from pathlib import Path
from queue import Queue
from urllib.parse import unquote, urlparse

import sublime  # pyright: ignore
import sublime_plugin  # pyright: ignore

# -- Logging

logging_formatter = logging.Formatter(fmt="[{name}] {levelname} {message}", style="{")

logging_handler = logging.StreamHandler()
logging_handler.setFormatter(logging_formatter)

logger = logging.getLogger(__package__)
logger.propagate = False
logger.addHandler(logging_handler)
logger.setLevel("DEBUG")


# -- CONSTANTS

STG_SERVERS = "servers"
STG_DIAGNOSTICS = "pg_lsc_diagnostics"
STATUS_DIAGNOSTICS = "pg_lsc_diagnostics"


# -- Global Variables

_STARTED_SERVERS = {}


## -- API


def settings():
    return sublime.load_settings("LanguageServerClient.sublime-settings")


def window_rootPath(window):
    return window.folders()[0] if window.folders() else None


def available_servers():
    return settings().get(STG_SERVERS, [])


def started_servers(rootPath):
    return _STARTED_SERVERS.get(rootPath)


def started_servers_values(rootPath):
    return _STARTED_SERVERS.get(rootPath, {}).values()


def started_server(rootPath, server):
    if started_servers_ := started_servers(rootPath):
        return started_servers_.get(server)


def view_syntax(view):
    return view.settings().get("syntax")


def view_applicable(config, view):
    applicable_to = set(config.get("applicable_to", []))

    applicable = view_syntax(view) in applicable_to

    if not applicable:
        logger.debug(
            f"Not-applicable View; Syntax '{view_syntax(view)}' not in {applicable_to}"
        )

    return applicable


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
        return sublime.KIND_ID_AMBIGUOUS


def location_start_text_point(view, location):
    return view.text_point(
        location["range"]["start"]["line"],
        location["range"]["start"]["character"],
    )


def location_end_text_point(view, location):
    return view.text_point(
        location["range"]["end"]["line"],
        location["range"]["end"]["character"],
    )


def location_region(view, location) -> sublime.Region:
    return sublime.Region(
        location_start_text_point(view, location),
        location_end_text_point(view, location),
    )


def diagnostic_start_text_point(view, diagnostic):
    return view.text_point(
        diagnostic["range"]["start"]["line"],
        diagnostic["range"]["start"]["character"],
    )


def diagnostic_end_text_point(view, diagnostic):
    return view.text_point(
        diagnostic["range"]["end"]["line"],
        diagnostic["range"]["end"]["character"],
    )


def diagnostic_region(view, diagnostic) -> sublime.Region:
    return sublime.Region(
        diagnostic_start_text_point(view, diagnostic),
        diagnostic_end_text_point(view, diagnostic),
    )


def document_diagnostic_quick_panel_item(diagnostic_item) -> sublime.QuickPanelItem:
    line = diagnostic_item["range"]["start"]["line"] + 1
    character = diagnostic_item["range"]["start"]["character"] + 1

    return sublime.QuickPanelItem(
        f"{diagnostic_item['message']}",
        details=f"{line}:{character}",
        annotation=f"{diagnostic_item['code']}",
        kind=severity_kind(diagnostic_item["severity"]),
    )


def location_quick_panel_item(location):
    start_line = location["range"]["start"]["line"] + 1
    start_character = location["range"]["start"]["character"] + 1

    return sublime.QuickPanelItem(
        f"{start_line}:{start_character}",
        details=location["uri"],
    )


def uri_to_path(uri: str) -> str:
    return unquote(urlparse(uri).path)


def open_location(window, location, flags=sublime.ENCODED_POSITION):
    row = location["range"]["start"]["line"] + 1
    col = location["range"]["start"]["character"] + 1

    window.open_file(f'{uri_to_path(location["uri"])}:{row}:{col}', flags)


# -- LSP


def view_textDocumentPositionParams(view, point):
    """
    A parameter literal used in requests to pass a text document and a position inside that document.

    https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocumentPositionParams
    """
    line, character = view.rowcol(point)

    return {
        "textDocument": {
            "uri": Path(view.file_name()).as_uri(),
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


class LanguageServerClient:
    def __init__(self, window, config):
        self.window = window
        self.config = config
        self.server_process = None
        self.server_shutdown = threading.Event()
        self.server_initialized = False
        self.send_queue = Queue(maxsize=1)
        self.receive_queue = Queue(maxsize=1)
        self.reader = None
        self.writer = None
        self.handler = None
        self.request_callback = {}
        self.open_documents = set()

    def __str__(self):
        return json.dumps(
            {
                "server_initialized": self.server_initialized,
                "open_documents": self.open_documents,
            }
        )

    def _start_reader(self):
        logger.debug(f"[{self.config['name']}] Reader is ready")

        while not self.server_shutdown.is_set():
            out = self.server_process.stdout

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
                content = out.read(int(content_length)).decode("utf-8").strip()

                logger.debug(f"[{self.config['name']}] < {content}")

                try:
                    # Enqueue message; Blocks if queue is full.
                    self.receive_queue.put(json.loads(content))
                except json.JSONDecodeError:
                    # The effect of not being able to decode a message,
                    # is that an 'in-flight' request won't have its callback called.
                    logger.error(f"Failed to decode message: {content}")

        logger.debug(f"[{self.config['name']}] Reader is done")

    def _start_writer(self):
        logger.debug(f"[{self.config['name']}] Writer is ready")

        while (message := self.send_queue.get()) is not None:
            if request_id := message.get("id"):
                logger.debug(
                    f"[{self.config['name']}] > {message['method']} ({request_id})"
                )
            else:
                logger.debug(f"[{self.config['name']}] > {message['method']}")

            try:
                content = json.dumps(message)

                header = f"Content-Length: {len(content)}\r\n\r\n"

                try:
                    self.server_process.stdin.write(header.encode("ascii"))
                    self.server_process.stdin.write(content.encode("utf-8"))
                    self.server_process.stdin.flush()
                except BrokenPipeError as e:
                    logger.error(
                        f"{self.config['name']} - Can't write to server's stdin: {e}"
                    )

            finally:
                self.send_queue.task_done()

        # 'None Task' is complete.
        self.send_queue.task_done()

        logger.debug(f"[{self.config['name']}] Writer is done")

    def _start_handler(self):
        logger.debug(f"[{self.config['name']}] Handler is ready")

        while (message := self.receive_queue.get()) is not None:  # noqa
            if request_id := message.get("id"):
                if callback := self.request_callback.get(request_id):
                    try:
                        callback(message)
                    except Exception as e:
                        logger.error(
                            f"{self.config['name']} - Request callback error: {e}"
                        )
                    finally:
                        del self.request_callback[request_id]
            else:
                if message["method"] == "window/logMessage":
                    # The log message notification is sent from the server to the client
                    # to ask the client to log a particular message.
                    #
                    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#window_logMessage
                    #
                    # Message Type:
                    #
                    # Error   = 1
                    # Warning = 2
                    # Info    = 3
                    # Log     = 4
                    #
                    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#messageType

                    log_type = message["params"]["type"]

                    log_message = message["params"]["message"]

                    logger.debug(f"{log_type} {log_message}")

                elif message["method"] == "textDocument/publishDiagnostics":
                    try:
                        # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#publishDiagnosticsParams
                        params = message["params"]

                        fname = unquote(urlparse(params["uri"]).path)

                        if view := self.window.find_open_file(fname):
                            diagnostics = params["diagnostics"]

                            view.settings().set(STG_DIAGNOSTICS, diagnostics)

                            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#diagnosticSeverity
                            severity_count = {
                                1: 0,
                                2: 0,
                                3: 0,
                                4: 0,
                            }

                            # Represents a diagnostic, such as a compiler error or warning.
                            # Diagnostic objects are only valid in the scope of a resource.
                            #
                            # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#diagnostic
                            for diagnostic in diagnostics:
                                severity_count[diagnostic["severity"]] += 1

                            diagnostics_status = []

                            for severity, count in severity_count.items():
                                if count > 0:
                                    diagnostics_status.append(
                                        f"{severity_name(severity)}: {count}"
                                    )

                            view.set_status(
                                STATUS_DIAGNOSTICS, ", ".join(diagnostics_status)
                            )

                    except Exception as e:
                        logger.error(e)

            self.receive_queue.task_done()

        # 'None Task' is complete.
        self.receive_queue.task_done()

        logger.debug(f"[{self.config['name']}] Handler is done")

    def _request(self, message, callback=None):
        self.send_queue.put(message)

        # A mapping of request ID to callback.
        #
        # callback will be called once the response for the request is received.
        #
        # callback might not be called if there's an error reading the response,
        # or the server never returns a response.
        self.request_callback[message["id"]] = callback

    def initialize(self):
        """
        The initialize request is sent as the first request from the client to the server.
        Until the server has responded to the initialize request with an InitializeResult,
        the client must not send any additional requests or notifications to the server.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initialize
        """

        if self.server_initialized:
            return

        # The rootPath of the workspace. Is null if no folder is open.
        # Deprecated in favour of rootUri.
        rootPath = self.window.folders()[0] if self.window.folders() else None

        # The rootUri of the workspace. Is null if no folder is open.
        # If both rootPath and rootUri are set rootUri wins.
        # Deprecated in favour of workspaceFolders.
        rootUri = Path(rootPath).as_uri() if rootPath else None

        # The workspace folders configured in the client when the server starts.
        workspaceFolders = (
            [{"name": Path(rootPath).name, "uri": rootUri}] if rootPath else None
        )

        logger.debug(
            f"Initialize {self.config['name']} {self.config['start']}; rootPath='{rootPath}'"
        )

        self.server_process = subprocess.Popen(
            self.config["start"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        logger.info(
            f"{self.config['name']} is up and running; PID {self.server_process.pid}"
        )

        # Thread responsible for handling received messages.
        self.handler = threading.Thread(
            name="Handler",
            target=self._start_handler,
            daemon=True,
        )
        self.handler.start()

        # Thread responsible for sending/writing messages.
        self.writer = threading.Thread(
            name="Writer",
            target=self._start_writer,
            daemon=True,
        )
        self.writer.start()

        # Thread responsible for reading messages.
        self.reader = threading.Thread(
            name="Reader",
            target=self._start_reader,
            daemon=True,
        )
        self.reader.start()

        def initialize_callback(response):
            self.server_initialized = True

            self.send_queue.put(
                {
                    "jsonrpc": "2.0",
                    "method": "initialized",
                    "params": {},
                }
            )

            # Notify the server about current views.
            # (Check if a view's syntax is valid for the server.)
            for view in self.window.views():
                if view_applicable(self.config, view):
                    self.textDocument_didOpen(view)

        # Enqueue 'initialize' message.
        # Message must contain "method" and "params";
        # Keys "id" and "jsonrpc" are added by the worker.
        self._request(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "initialize",
                "params": {
                    "processId": os.getpid(),
                    "clientInfo": {
                        "name": "Sublime Text Language Server Client",
                        "version": "0.1.0",
                    },
                    "rootPath": rootPath,
                    "rootUri": rootUri,
                    "workspaceFolders": workspaceFolders,
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
                },
            },
            initialize_callback,
        )

    def shutdown(self):
        """
        The shutdown request is sent from the client to the server.
        It asks the server to shut down,
        but to not exit (otherwise the response might not be delivered correctly to the client).
        There is a separate exit notification that asks the server to exit.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown
        """

        self._request(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "shutdown",
                "params": {},
            },
            lambda _: self.exit(),
        )

    def exit(self):
        """
        A notification to ask the server to exit its process.
        The server should exit with success code 0 if the shutdown request has been received before;
        otherwise with error code 1.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#exit
        """

        self.send_queue.put(
            {
                "jsonrpc": "2.0",
                "method": "exit",
                "params": {},
            }
        )

        self.server_shutdown.set()

        # Enqueue `None` to signal that workers must stop:
        self.send_queue.put(None)
        self.receive_queue.put(None)

        returncode = None

        try:
            returncode = self.server_process.wait(30)
        except subprocess.TimeoutExpired:
            # Explicitly kill the process if it did not terminate.
            self.server_process.kill()

            returncode = self.server_process.wait()

        logger.debug(
            f"[{self.config['name']}] Server terminated with returncode {returncode}"
        )

    def textDocument_didOpen(self, view):
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
        if view.file_name() in self.open_documents:
            return

        self.send_queue.put(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {
                    "textDocument": {
                        "uri": Path(view.file_name()).as_uri(),
                        "languageId": syntax_languageId(view.settings().get("syntax")),
                        "version": view.change_count(),
                        "text": view.substr(sublime.Region(0, view.size())),
                    },
                },
            }
        )

        self.open_documents.add(view.file_name())

    def textDocument_didClose(self, view):
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

        # A close notification requires a previous open notification to be sent.
        if view.file_name() not in self.open_documents:
            return

        self.send_queue.put(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didClose",
                "params": {
                    "textDocument": {
                        "uri": Path(view.file_name()).as_uri(),
                    },
                },
            }
        )

        self.open_documents.remove(view.file_name())

    def textDocument_hover(self, params, callback):
        """
        The hover request is sent from the client to the server to request
        hover information at a given text document position.

        https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_hover
        """
        self._request(
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
        self._request(
            {
                "jsonrpc": "2.0",
                "id": str(uuid.uuid4()),
                "method": "textDocument/definition",
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


class LanguageServerClientInitializeCommand(sublime_plugin.WindowCommand):
    def input(self, args):
        if "server" not in args:
            available_servers_names = [config["name"] for config in available_servers()]

            return ServerInputHandler(sorted(available_servers_names))

    def run(self, server):
        available_servers_indexed = {
            config["name"]: config for config in available_servers()
        }

        config = available_servers_indexed.get(server)

        client = LanguageServerClient(window=self.window, config=config)
        client.initialize()

        rootPath = window_rootPath(self.window)

        if started_servers_ := started_servers(rootPath):
            started_servers_[server] = {
                "config": config,
                "client": client,
            }
        else:
            global _STARTED_SERVERS
            _STARTED_SERVERS[rootPath] = {
                server: {
                    "config": config,
                    "client": client,
                }
            }


class LanguageServerClientShutdownCommand(sublime_plugin.WindowCommand):
    def input(self, args):
        if "server" not in args:
            rootPath = window_rootPath(self.window)

            return ServerInputHandler(sorted(started_servers(rootPath).keys()))

    def run(self, server):
        rootPath = window_rootPath(self.window)

        if started_server_ := started_server(rootPath, server):
            started_server_["client"].shutdown()

            global _STARTED_SERVERS
            del _STARTED_SERVERS[rootPath][server]


class LanguageServerClientDebugCommand(sublime_plugin.WindowCommand):
    def run(self):
        logger.debug(_STARTED_SERVERS)


class PgSmartsGotoDefinition(sublime_plugin.TextCommand):
    def run(self, _):
        for started_server in started_servers_values(
            window_rootPath(self.view.window())
        ):
            config = started_server["config"]
            client = started_server["client"]

            point = self.view.sel()[0].begin()

            if view_applicable(config, self.view):

                def callback(response):
                    result = response.get("result")

                    if not result:
                        return

                    locations = sorted(
                        [result] if isinstance(result, dict) else result,
                        key=lambda location: [
                            location["range"]["start"]["line"],
                            location["range"]["start"]["character"],
                        ],
                    )

                    if len(locations) == 1:
                        open_location(self.view.window(), locations[0])
                    else:
                        initial_viewport_position = self.view.viewport_position()

                        def on_highlight(index):
                            open_location(
                                self.view.window(),
                                locations[index],
                                flags=sublime.ENCODED_POSITION | sublime.TRANSIENT,
                            )

                        def on_select(index):
                            if index == -1:
                                self.view.set_viewport_position(
                                    initial_viewport_position, True
                                )

                            else:
                                open_location(self.view.window(), locations[index])

                        quick_panel_items = [
                            location_quick_panel_item(location)
                            for location in locations
                        ]

                        self.view.window().show_quick_panel(
                            quick_panel_items,
                            on_select,
                            on_highlight=on_highlight,
                        )

                client.textDocument_definition(
                    view_textDocumentPositionParams(self.view, point),
                    callback,
                )


class PgSmartsGotoDocumentDiagnostic(sublime_plugin.TextCommand):
    def run(self, _):
        initial_viewport_position = self.view.viewport_position()

        diagnostics = sorted(
            self.view.settings().get(STG_DIAGNOSTICS, []),
            key=lambda diagnostic: [
                diagnostic["range"]["start"]["line"],
                diagnostic["range"]["start"]["character"],
            ],
        )

        def on_highlight(index):
            diagnostic = diagnostics[index]

            logger.debug(diagnostic)

            self.view.show_at_center(diagnostic_region(self.view, diagnostic))

        def on_select(index):
            if index == -1:
                self.view.set_viewport_position(initial_viewport_position, True)

            else:
                region = diagnostic_region(self.view, diagnostics[index])

                self.view.show_at_center(region)
                self.view.sel().clear()
                self.view.sel().add(region)

        quick_panel_items = [
            document_diagnostic_quick_panel_item(diagnostic)
            for diagnostic in diagnostics
        ]

        self.view.window().show_quick_panel(
            quick_panel_items,
            on_select,
            on_highlight=on_highlight,
        )


## -- Listeners


class LanguageServerClientViewListener(sublime_plugin.ViewEventListener):
    def on_load_async(self):
        rootPath = window_rootPath(self.view.window())

        if started_servers_ := started_servers(rootPath):
            for started_server in started_servers_.values():
                config = started_server["config"]
                client = started_server["client"]

                if view_applicable(config, self.view):
                    client.textDocument_didOpen(self.view)

    def on_pre_close(self):
        # When the window is closed, there's no window 'attached' to view.
        if not self.view.window():
            return

        rootPath = window_rootPath(self.view.window())

        if started_servers_ := started_servers(rootPath):
            for started_server in started_servers_.values():
                client = started_server["client"]
                client.textDocument_didClose(self.view)

    def on_hover(self, point, hover_zone):
        if hover_zone == sublime.HOVER_TEXT:
            for started_server in started_servers_values(
                window_rootPath(self.view.window())
            ):
                config = started_server["config"]
                client = started_server["client"]

                def show_contents_popup(response):
                    if result := response["result"]:
                        # The result of a hover request.
                        # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#hover

                        result_contents = result["contents"]

                        popup_content = ""

                        if isinstance(result_contents, str):
                            popup_content = re.sub(r"\n", "<br/>", result_contents)
                            popup_content = re.sub(
                                r"\t", "&nbsp;&nbsp;&nbsp;&nbsp;", popup_content
                            )

                        elif isinstance(result_contents, dict):
                            popup_content = re.sub(
                                r"\n", "<br/>", result_contents["value"]
                            )
                            popup_content = re.sub(
                                r"\t", "&nbsp;&nbsp;&nbsp;&nbsp;", popup_content
                            )

                        elif isinstance(result_contents, list):
                            for x in result_contents:
                                if isinstance(x, str):
                                    popup_content += re.sub(r"\n", "<br/>", x)

                                elif isinstance(x, dict):
                                    popup_content += re.sub(r"\n", "<br/>", x["value"])

                        # The popup is shown at the current postion of the caret.
                        location = -1

                        # An optional range is a range inside a text document
                        # that is used to visualize a hover, e.g. by changing the background color.
                        if result_range := result["range"]:
                            location = self.view.text_point(
                                result_range["start"]["line"],
                                result_range["start"]["character"],
                            )

                        self.view.show_popup(
                            popup_content,
                            location=location,
                            max_width=860,
                        )

                if view_applicable(config, self.view):
                    client.textDocument_hover(
                        view_textDocumentPositionParams(self.view, point),
                        show_contents_popup,
                    )


class LanguageServerClientListener(sublime_plugin.EventListener):
    def on_pre_close_window(self, window):
        def shutdown_servers(started_servers):
            for started_server in started_servers.values():
                started_server["client"].shutdown()

        if started_servers_ := started_servers(window_rootPath(window)):
            logger.debug("Shutdown Servers...")

            threading.Thread(
                name="ShutdownServers",
                target=lambda: shutdown_servers(started_servers_),
                daemon=True,
            ).start()


# -- PLUGIN LIFECYLE


def plugin_loaded():
    logger.debug("loaded plugin")


def plugin_unloaded():
    logger.debug("unloaded plugin")

    logger.removeHandler(logging_handler)
