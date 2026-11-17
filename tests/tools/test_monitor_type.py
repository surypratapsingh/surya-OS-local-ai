"""Unit tests for tools/monitor-type.py (stdlib unittest only).

A local TCP server stands in for the QEMU monitor. The server records each
line the tool sends, so the expected command list is the one the test passes
in on the command line, not a value read back from the tool.
"""

import importlib.util
import os
import socket
import sys
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.normpath(os.path.join(HERE, "..", "..", "tools", "monitor-type.py"))

_spec = importlib.util.spec_from_file_location("monitor_type", TOOL)
mt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mt)


class FakeMonitor:
    """Accepts one connection, sends a banner, answers every line with a prompt."""

    def __init__(self):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.lines = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.srv.accept()
        conn.settimeout(10)
        try:
            conn.sendall(b"QEMU monitor\r\n(qemu) ")
            buf = b""
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.lines.append(line.decode())
                    conn.sendall(b"(qemu) ")
        except (socket.timeout, OSError):
            pass
        finally:
            conn.close()
            self.srv.close()

    def join(self):
        self.thread.join(timeout=15)


class MonitorTypeTests(unittest.TestCase):
    def run_tool(self, host, port, keys):
        argv = ["monitor-type.py", host, str(port), keys]
        with mock.patch.object(sys, "argv", argv):
            return mt.main()

    def test_usage_error_returns_2(self):
        with mock.patch.object(sys, "argv", ["monitor-type.py"]):
            self.assertEqual(mt.main(), 2)

    def test_keys_are_sent_in_order_then_quit(self):
        mon = FakeMonitor()
        rc = self.run_tool("127.0.0.1", mon.port, "q 1 ret")
        mon.join()
        self.assertEqual(rc, 0)
        self.assertEqual(
            mon.lines,
            ["sendkey q", "sendkey 1", "sendkey ret", "quit"],
        )

    def test_single_key_is_sent(self):
        mon = FakeMonitor()
        rc = self.run_tool("127.0.0.1", mon.port, "spc")
        mon.join()
        self.assertEqual(rc, 0)
        self.assertEqual(mon.lines, ["sendkey spc", "quit"])


if __name__ == "__main__":
    unittest.main()
