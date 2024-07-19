import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from queue import Queue
from urllib.parse import unquote, urlparse

import sublime  # pyright: ignore
import sublime_plugin  # pyright: ignore

# -- Logging

logging_formatter = logging.Formatter(fmt="[{name}] {levelname}: {message}", style="{")

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


def started_server(rootPath, server):
    if started_servers_ := started_servers(rootPath):
        return started_servers_.get(server)


def view_syntax(view):
    return view.settings().get("syntax")

# -- LSP


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


class LanguageServerClient:
    def __init__(self, window, config):
        self.window = window
        self.config = config
        self.server_process = None
        self.server_shutdown = threading.Event()
        self.server_reader = None
        self.server_request_count = 1
        self.server_initialized = False
        self.send_queue = Queue(maxsize=1)
        self.send_worker = None
        self.receive_queue = Queue(maxsize=1)
        self.receive_worker = None
        self.request_callback = {}

    def _read(self):
        logger.debug("Reader is ready")

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

                logger.debug(f"< {content}")

                try:
                    # Enqueue message; Blocks if queue is full.
                    self.receive_queue.put(json.loads(content))
                except json.JSONDecodeError:
                    # The effect of not being able to decode a message,
                    # is that an 'in-flight' request won't have its callback called.
                    logger.error(f"Failed to decode message: {content}")

        logger.debug("Reader is done")

    def _send(self):
        logger.debug("Send Worker is ready")

        while (message := self.send_queue.get()) is not None:
            if request_id := message.get("id"):
                logger.debug(f"> REQUEST {request_id} {message['method']}")
            else:
                logger.debug(f"> NOTIFICATION {message['method']}")

            try:
                content = json.dumps(message)

                header = f"Content-Length: {len(content)}\r\n\r\n"

                try:
                    self.server_process.stdin.write(header.encode("ascii"))
                    self.server_process.stdin.write(content.encode("utf-8"))
                    self.server_process.stdin.flush()
                except BrokenPipeError as e:
                    logger.error(f"Can't write to server's stdin: {e}")

            finally:
                self.server_request_count += 1

                self.send_queue.task_done()

        # 'None Task' is complete.
        self.send_queue.task_done()

        logger.debug("Send Worker is done")

    def _handle(self):
        logger.debug("Receive Worker is ready")

        while (message := self.receive_queue.get()) is not None:  # noqa
            if request_id := message.get("id"):
                if callback := self.request_callback.get(request_id):
                    try:
                        callback(message)
                    except Exception as e:
                        logger.error(f"Request callback error: {e}")
                    finally:
                        del self.request_callback[request_id]
            else:
                if message["method"] == "textDocument/publishDiagnostics":
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

                            severity_name = {
                                1: "Error",
                                2: "Warning",
                                3: "Info",
                                4: "Hint",
                            }

                            for severity, count in severity_count.items():
                                if count > 0:
                                    diagnostics_status.append(
                                        f"{severity_name[severity]}: {count}"
                                    )

                            view.set_status(
                                STATUS_DIAGNOSTICS, ", ".join(diagnostics_status)
                            )

                    except Exception as e:
                        logger.error(e)

            self.receive_queue.task_done()

        # 'None Task' is complete.
        self.receive_queue.task_done()

        logger.debug("Receive Worker is done")

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
        # The initialize request is sent as the first request from the client to the server.
        # Until the server has responded to the initialize request with an InitializeResult,
        # the client must not send any additional requests or notifications to the server.
        #
        # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initialize

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

        logger.debug(
            f"{self.config['name']} is up and running; PID {self.server_process.pid}"
        )

        # Start Receive Worker - responsible for handling received messages.
        self.receive_worker = threading.Thread(
            name="ReceiveWorker",
            target=self._handle,
            daemon=True,
        )
        self.receive_worker.start()

        # Start Send Worker - responsible for sending messages.
        self.send_worker = threading.Thread(
            name="SendWorker",
            target=self._send,
            daemon=True,
        )
        self.send_worker.start()

        # Start Reader - responsible for reading messages from sever's stdout.
        self.server_reader = threading.Thread(
            name="Reader",
            target=self._read,
            daemon=True,
        )
        self.server_reader.start()

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
                if view_syntax(view) in set(self.config["applicable_to"]):
                    self.text_document_did_open(view)

        # Enqueue 'initialize' message.
        # Message must contain "method" and "params";
        # Keys "id" and "jsonrpc" are added by the worker.
        self._request(
            {
                "jsonrpc": "2.0",
                "id": self.server_request_count,
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
        # The shutdown request is sent from the client to the server.
        # It asks the server to shut down,
        # but to not exit (otherwise the response might not be delivered correctly to the client).
        # There is a separate exit notification that asks the server to exit.
        #
        # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown

        self._request(
            {
                "jsonrpc": "2.0",
                "id": self.server_request_count,
                "method": "shutdown",
                "params": {},
            },
            lambda _: self.exit(),
        )

        # TODO
        # Handle shutdown response.
        # Stop reader and workers after shutdown response is received.

    def exit(self):
        # A notification to ask the server to exit its process.
        # The server should exit with success code 0 if the shutdown request has been received before;
        # otherwise with error code 1.
        #
        # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#exit
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

        logger.debug(f"Server terminated with returncode {returncode}")

    def text_document_did_open(self, view):
        # The document open notification is sent from the client to the server
        # to signal newly opened text documents.
        #
        # The document’s content is now managed by the client
        # and the server must not try to read the document’s content using the document’s Uri.
        #
        # Open in this sense means it is managed by the client.
        # It doesn’t necessarily mean that its content is presented in an editor.
        #
        # An open notification must not be sent more than once without a corresponding close notification send before.
        # This means open and close notification must be balanced and the max open count for a particular textDocument is one.
        #
        # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#textDocument_didOpen
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


## -- Listeners


class LanguageServerClientViewListener(sublime_plugin.ViewEventListener):
    @classmethod
    def is_applicable(cls, settings):
        # return settings.get("syntax") in set()

        return True

    def on_load(self):
        pass

    def on_modified(self):
        pass

    def on_close(self):
        pass


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
