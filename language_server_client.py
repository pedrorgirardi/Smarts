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
        self.server_writer_lock = threading.Lock()
        self.server_request_count = 0
        self.server_initialized = False
        self.receive_queue = Queue(maxsize=1)
        self.receive_worker = None

    def write(self, header, body):
        with self.server_writer_lock:
            self.server_process.stdin.write(header.encode("ascii"))
            self.server_process.stdin.write(body.encode("utf-8"))
            self.server_process.stdin.flush()

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

    def handle(self):
        logger.debug("Worker is ready")

        def rec():
            logger.debug("Waiting for message...")

            message = self.receive_queue.get()

            if message is None:
                logger.debug("Received None")

            return message

        while (message := rec()) is not None:
            _, body = message

            logger.debug(f"Handle {body}")

            self.receive_queue.task_done()

        # 'None Task' is complete.
        self.receive_queue.task_done()

        logger.debug("Worker is done")

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

        # Start Worker - responsible for handling messages.
        self.receive_worker = threading.Thread(name="Worker", target=self.handle)
        self.receive_worker.start()

        # Start Reader - responsible for reading messages from sever's stdout.
        self.server_reader = threading.Thread(name="Reader", target=self.read)
        self.server_reader.start()

        rootUri = Path(rootPath).as_uri()

        self.server_request_count += 1

        message = {
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
        }

        body = json.dumps(message)

        header = f"Content-Length: {len(body)}\r\n\r\n"

        self.write(header, body)

    def shutdown(self):
        if p := self.server_process:
            self.server_shutdown.set()

            try:
                p.terminate()
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.debug("Timeout")

    def exit(self):
        if p := self.server_process:
            self.server_shutdown.set()

            def _exit():
                logger.debug("Exiting")

                try:
                    o, e = p.communicate(timeout=10)

                    logger.debug(f"Out: {o}, Error: {e}")
                except subprocess.TimeoutExpired:
                    p.kill()

                    o, e = p.communicate()

                    logger.debug(f"Out: {o}, Error: {e}")

                # Enqueue `None` to signal that handler must stop.
                logger.debug("Stop Worker")

                self.receive_queue.put(None)

            threading.Thread(name="ServerExit", target=_exit).start()


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
