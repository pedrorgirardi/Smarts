import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from queue import Queue

import sublime  # pyright: ignore
import sublime_plugin  # pyright: ignore

logger = logging.getLogger("LSC")

STG_SERVERS = "servers"


def settings():
    return sublime.load_settings("LanguageServerClient.sublime-settings")


# -- LSP


class LanguageServerClient:
    def __init__(self, server_name, server_process_args):
        self.server_name = server_name
        self.server_process_args = server_process_args
        self.server_process = None
        self.server_shutdown = threading.Event()
        self.server_reader = None
        self.server_request_count = 0
        self.server_initialized = False
        self.send_queue = Queue(maxsize=1)
        self.send_worker = None
        self.receive_queue = Queue(maxsize=1)
        self.receive_worker = None

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
            logger.debug(f"> {message}")

            try:
                self.server_request_count += 1

                content = json.dumps(message)

                header = f"Content-Length: {len(content)}\r\n\r\n"

                try:
                    self.server_process.stdin.write(header.encode("ascii"))
                    self.server_process.stdin.write(content.encode("utf-8"))
                    self.server_process.stdin.flush()
                except BrokenPipeError as e:
                    logger.error(f"Can't write to server's stdin: {e}")

            finally:
                self.send_queue.task_done()

        # 'None Task' is complete.
        self.send_queue.task_done()

        logger.debug("Send Worker is done")

    def _handle(self):
        logger.debug("Receive Worker is ready")

        while (message := self.receive_queue.get()) is not None:  # noqa
            self.receive_queue.task_done()

        # 'None Task' is complete.
        self.receive_queue.task_done()

        logger.debug("Receive Worker is done")

    def initialize(self, rootPath):
        # The initialize request is sent as the first request from the client to the server.
        # Until the server has responded to the initialize request with an InitializeResult,
        # the client must not send any additional requests or notifications to the server.
        #
        # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initialize

        logger.debug(f"Initialize {self.server_name} {self.server_process_args}")

        self.server_process = subprocess.Popen(
            self.server_process_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        logger.debug(
            f"{self.server_name} is up and running; PID {self.server_process.pid}"
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

        rootUri = Path(rootPath).as_uri()

        self.server_request_count += 1

        # Enqueue 'initialize' message.
        # Message must contain "method" and "params";
        # Keys "id" and "jsonrpc" are added by the worker.
        self.send_queue.put(
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
                    "capabilities": {},
                },
            },
        )

    def shutdown(self):
        # The shutdown request is sent from the client to the server.
        # It asks the server to shut down,
        # but to not exit (otherwise the response might not be delivered correctly to the client).
        # There is a separate exit notification that asks the server to exit.
        #
        # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown

        self.server_request_count += 1

        self.send_queue.put(
            {
                "jsonrpc": "2.0",
                "id": self.server_request_count,
                "method": "shutdown",
                "params": {},
            }
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


# -- INPUT HANDLERS


class ServerInputHandler(sublime_plugin.ListInputHandler):
    def placeholder(self):
        return "Server"

    def name(self):
        return "server"

    def list_items(self):
        # The returned value may be a list of item,
        # or a 2-element tuple containing a list of items,
        # and an int index of the item to pre-select.
        return sorted(settings().get(STG_SERVERS).keys())


# -- COMMANDS


class LanguageServerClientInitializeCommand(sublime_plugin.WindowCommand):
    def input(self, args):
        if "server" not in args:
            return ServerInputHandler()

    def run(self, server):
        server_config = settings().get(STG_SERVERS).get(server)

        self.window._lsc_client = LanguageServerClient(
            server_name=server,
            server_process_args=server_config["args"],
        )

        rootPath = self.window.folders()[0] if self.window.folders() else None

        self.window._lsc_client.initialize(rootPath)


class LanguageServerClientShutdownCommand(sublime_plugin.WindowCommand):
    def run(self):
        if c := self.window._lsc_client:
            c.shutdown()


class LanguageServerClientExitCommand(sublime_plugin.WindowCommand):
    def run(self):
        if c := self.window._lsc_client:
            threading.Thread(target=c.exit, daemon=True).start()


# -- PLUGIN LIFECYLE


def plugin_loaded():
    logging_level = "DEBUG"

    logging_format = "%(asctime)s %(name)s %(levelname)s %(message)s"

    logging.basicConfig(level=logging_level, format=logging_format)

    logger.debug("loaded plugin")


def plugin_unloaded():
    logger.debug("unloaded plugin")
