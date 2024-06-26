import json
import os
import subprocess
import threading
from pathlib import Path

import sublime  # pyright: ignore
import sublime_plugin  # pyright: ignore

SERVERS = {
    "Nightincode": [
        "java",
        "-jar",
        "/Users/pedro/Developer/Nightincode/nightincode.jar",
    ],
}


def lsp_reader(stdout):
    while True:
        length_header = stdout.readline().decode("ascii").strip()

        print(length_header)

        if not length_header.startswith("Content-Length:"):
            continue

        length = int(length_header.split(":")[1].strip())

        # Consume the empty line:
        stdout.readline()

        message = stdout.read(length).decode("utf-8")

        print(message)


class ServerInputHandler(sublime_plugin.ListInputHandler):
    def placeholder(self):
        return "Server"

    def name(self):
        return "server"

    def list_items(self):
        # The returned value may be a list of item,
        # or a 2-element tuple containing a list of items,
        # and an int index of the item to pre-select.
        return sorted(SERVERS.keys())


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
        server_process = subprocess.Popen(
            ["java", "-jar", "/Users/pedro/Developer/Nightincode/nightincode.jar"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        self.window._lsc_server_process = server_process

        print("Started Server")

        lsc_reader_thread = threading.Thread(
            target=lambda: lsp_reader(server_process.stdout)
        )

        self.window._lsc_reader_thread = lsc_reader_thread

        lsc_reader_thread.start()

        print("Started Reader")

        rootPath = self.window.folders()[0] if self.window.folders() else None
        rootUri = Path(self.window.folders()[0]).as_uri() if self.window.folders() else None

        message = {
            "jsonrpc": "2.0",
            "id": 1,
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

        server_process.stdin.write(header.encode("ascii"))
        server_process.stdin.write(body.encode("utf-8"))
        server_process.stdin.flush()

        print("Flush")


class LanguageServerClientShutdownCommand(sublime_plugin.WindowCommand):
    # The shutdown request is sent from the client to the server.
    # It asks the server to shut down,
    # but to not exit (otherwise the response might not be delivered correctly to the client).
    # There is a separate exit notification that asks the server to exit.
    #
    # https://microsoft.github.io/language-server-protocol/specifications/lsp/3.17/specification/#shutdown

    def input(self, args):
        if "server" not in args:
            return ServerInputHandler()

    def run(self, server):
        self.window._lsc_server_process.kill()
