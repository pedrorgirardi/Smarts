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

    def read(self):
        logger.debug("Reader is ready")

        while not self.server_shutdown.is_set():
            out = self.server_process.stdout

            # -- HEADER

            headers = {}

            while True:
                line = out.readline().decode("ascii").strip()

                if line == "":
                    break

                k, v = line.split(": ", 1)

                headers[k] = v

            # -- BODY

            body = None

            if content_length := headers.get("Content-Length"):
                body = out.read(int(content_length)).decode("utf-8").strip()

                # Enqueue message (header & body).
                # Blocks if queue is full.
                self.receive_queue.put((headers, body))

        logger.debug("Reader is done")

    def send(self):
        logger.debug("Send Worker is ready")

        while (message := self.send_queue.get()) is not None:
            try:
                self.server_request_count += 1

                body = json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": self.server_request_count,
                        **message,
                    }
                )

                header = f"Content-Length: {len(body)}\r\n\r\n"

                self.server_process.stdin.write(header.encode("ascii"))
                self.server_process.stdin.write(body.encode("utf-8"))
                self.server_process.stdin.flush()

                logger.debug(f"Sent {body}")
            finally:
                self.send_queue.task_done()

        # 'None Task' is complete.
        self.send_queue.task_done()

        logger.debug("Send Worker is done")

    def handle(self):
        logger.debug("Receive Worker is ready")

        while (message := self.receive_queue.get()) is not None:
            _, body = message

            logger.debug(f"Handle {body}")

            self.receive_queue.task_done()

        # 'None Task' is complete.
        self.receive_queue.task_done()

        logger.debug("Receive Worker is done")

    def initialize(self, rootPath):
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
        self.receive_worker = threading.Thread(name="ReceiveWorker", target=self.handle)
        self.receive_worker.start()

        # Start Send Worker - responsible for sending messages.
        self.send_worker = threading.Thread(name="SendWorker", target=self.send)
        self.send_worker.start()

        # Start Reader - responsible for reading messages from sever's stdout.
        self.server_reader = threading.Thread(name="Reader", target=self.read)
        self.server_reader.start()

        rootUri = Path(rootPath).as_uri()

        # Enqueue 'initialize' message.
        # Message must contain "method" and "params";
        # Keys "id" and "jsonrpc" are added by the worker.
        self.send_queue.put(
            {
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
        self.send_queue.put(
            {
                "method": "shutdown",
                "params": {},
            }
        )

    def exit(self):
        self.send_queue.put(
            {
                "method": "exit",
                "params": {},
            }
        )

        self.server_shutdown.set()

        # Enqueue `None` to signal that workers must stop:
        self.send_queue.put(None)
        self.receive_queue.put(None)

        try:
            returncode = self.server_process.wait(15)
            logger.debug(f"Server terminated with returncode {returncode}")
        except subprocess.TimeoutExpired:
            logger.error("Server didn't terminate within timeout")


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


class LanguageServerClientInitializeCommand(sublime_plugin.WindowCommand):
    # The initialize request is sent as the first request from the client to the server.
    # Until the server has responded to the initialize request with an InitializeResult,
    # the client must not send any additional requests or notifications to the server.
    #
    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#initialize

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
    # The shutdown request is sent from the client to the server.
    # It asks the server to shut down,
    # but to not exit (otherwise the response might not be delivered correctly to the client).
    # There is a separate exit notification that asks the server to exit.
    #
    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown

    def run(self):
        if c := self.window._lsc_client:
            c.shutdown()


class LanguageServerClientExitCommand(sublime_plugin.WindowCommand):
    # The shutdown request is sent from the client to the server.
    # It asks the server to shut down,
    # but to not exit (otherwise the response might not be delivered correctly to the client).
    # There is a separate exit notification that asks the server to exit.
    #
    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown

    def run(self):
        if c := self.window._lsc_client:
            c.exit()


def plugin_loaded():
    logging_level = "DEBUG"

    logging_format = "%(asctime)s %(name)s %(levelname)s %(message)s"

    logging.basicConfig(level=logging_level, format=logging_format)

    logger.debug("loaded plugin")


def plugin_unloaded():
    logger.debug("unloaded plugin")
