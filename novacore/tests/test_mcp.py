"""N6: NOVA's MCP server (novacore/src/novacore/mcp_server.py).

Oracles. The official MCP Python SDK could not be run where this was written:
installing it was refused as external code. Its source was read instead, and
the rules this file checks are taken from that source, never from the server:
- message shapes: mcp-types 2.3.0, `mcp_types/_v2025_11_25/__init__.py`, which
  is generated from the MCP repository's `schema/2025-11-25.json` (sha256
  4e01628360a2149892eab8f298ceee626d24a58862184eb8ec85d95b8f353e31, per its
  header). RequestId is a string or an integer. InitializeResult needs
  protocolVersion, capabilities and serverInfo, and serverInfo needs a name
  and a version. A Tool needs a name and an inputSchema of type "object".
  CallToolResult needs a content list of {type: "text", text}. Errors the tool
  itself hits go in the result with isError true; "errors in finding the tool"
  are protocol errors.
- connecting: mcp 2.3.0, `mcp/client/_probe.py`. A client first sends
  `server/discover`; on any JSON-RPC error except -32022 it falls back to
  `initialize`. `mcp/client/session.py` `initialize()` refuses a
  protocolVersion outside 2024-11-05 ... 2025-11-25.
What happened is checked on the outbox, read and verified independently
(test_desktop._events), and on what the launched program wrote.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from novacore import mcp_server
from novacore.capabilities import CapabilityBroker
from novacore.config import ConfigError, DesktopConfig, load_config
from novacore.desktop import DesktopBridge
from test_desktop import FakePlatform, _events

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).parent
SDK_HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")  # mcp_types/version.py
CLIENT_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")  # names every common client accepts


def _request(request_id: int, method: str, params: dict | None = None) -> str:
    message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


INITIALIZE = _request(2, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                        "clientInfo": {"name": "spec-client", "version": "1"}})
INITIALIZED = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})


class SpecChecks:
    """Shape checks from the generated 2025-11-25 schema module (see the module docstring)."""

    def assert_result(self: unittest.TestCase, response: dict, request_id: int) -> dict:
        self.assertEqual("2.0", response["jsonrpc"])
        self.assertEqual(request_id, response["id"])
        self.assertNotIn("error", response)
        return response["result"]

    def assert_error(self: unittest.TestCase, response: dict, request_id: object, code: int) -> None:
        self.assertEqual("2.0", response["jsonrpc"])
        self.assertEqual(request_id, response.get("id"))
        self.assertNotIn("result", response)
        self.assertEqual(code, response["error"]["code"])
        self.assertIsInstance(response["error"]["message"], str)

    def assert_tool(self: unittest.TestCase, tool: dict) -> None:
        self.assertRegex(tool["name"], CLIENT_SAFE_NAME)
        schema = tool["inputSchema"]
        self.assertEqual("object", schema["type"])
        self.assertLessEqual(set(schema.get("required", [])), set(schema.get("properties", {})))

    def assert_call_result(self: unittest.TestCase, result: dict) -> str:
        self.assertIsInstance(result["content"], list)
        [block] = result["content"]
        self.assertEqual("text", block["type"])
        self.assertIsInstance(block["text"], str)
        return block["text"]


def _write_config(base: Path, tools: list[str], extra_desktop: str = "") -> Path:
    docs = base / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "physics notes.pdf").write_text("x", encoding="utf-8")
    marker = base / "probe-runs"
    probe = [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('ran\\n')"]
    config = base / "config.toml"
    config.write_text(
        f"[model]\ncommand = {json.dumps([sys.executable, str(HERE / 'fake_runner.py')])}\n"
        "timeout_seconds = 10\nmax_output_chars = 1000\n\n"
        f"[desktop]\nconfirm_all_actions = true\nfolders = {json.dumps([str(docs)])}\n{extra_desktop}\n"
        f"[desktop.apps]\nprobe = {json.dumps(probe)}\n\n"
        f"[mcp]\ntools = {json.dumps(tools)}\n\n"
        "[conversation]\nhistory_turns = 2\nmemory_results = 2\n\n[storage]\nstate_dir = \"state\"\n",
        encoding="utf-8",
    )
    return config


def _serve(config: Path, lines: list[str], timeout: float = 30) -> tuple[list[dict], str, int]:
    done = subprocess.run(
        [sys.executable, "-m", "novacore", "--config", str(config), "--mcp"],
        input="".join(line + "\n" for line in lines).encode("utf-8"),
        capture_output=True, timeout=timeout,
        env={**os.environ, "PYTHONPATH": str(ROOT / "novacore" / "src")},
    )
    stdout = done.stdout.decode("utf-8")
    return [json.loads(line) for line in stdout.splitlines()], done.stderr.decode("utf-8"), done.returncode


@unittest.skipUnless(sys.platform.startswith("linux"), "the session starts a real program; checked on Linux")
class SessionTests(unittest.TestCase, SpecChecks):
    CANARY = "CANARY-MCP-NOTE-77"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.config = _write_config(self.base, ["time.now", "files.find", "apps.open"])
        # A note the server must never read: a canary in the owner's memory log.
        (self.base / "state").mkdir()
        (self.base / "state" / "memories.jsonl").write_text(json.dumps({"text": self.CANARY}) + "\n", encoding="utf-8")

    def test_a_client_session_as_the_official_sdk_runs_it(self) -> None:
        responses, stderr, status = _serve(self.config, [
            _request(1, "server/discover", {"_meta": {}}),   # 2026 probe first (mcp/client/_probe.py)
            INITIALIZE, INITIALIZED,
            _request(3, "tools/list"),
            _request(4, "tools/call", {"name": "files_find", "arguments": {"query": "physics"}}),
            _request(5, "tools/call", {"name": "apps_open", "arguments": {"app": "probe"}}),
            _request(6, "tools/call", {"name": "apps_open", "arguments": {"app": "calculator"}}),
            _request(7, "tools/call", {"name": "memory_save", "arguments": {"text": "x"}}),
            _request(8, "ping"),
        ])
        self.assertEqual(0, status, stderr)
        self.assertEqual(8, len(responses))  # one per request; none for the notification
        discover, init, listing, found, launched, refused, unknown, ping = responses
        self.assert_error(discover, 1, -32601)  # any error makes the SDK fall back to initialize
        result = self.assert_result(init, 2)
        self.assertIn(result["protocolVersion"], SDK_HANDSHAKE_VERSIONS)
        self.assertEqual("2025-11-25", result["protocolVersion"])
        self.assertIsInstance(result["capabilities"], dict)
        self.assertIn("tools", result["capabilities"])
        self.assertTrue(result["serverInfo"]["name"] and result["serverInfo"]["version"])
        tools = self.assert_result(listing, 3)["tools"]
        for tool in tools:
            self.assert_tool(tool)
        self.assertEqual(["time_now", "files_find", "apps_open"], [tool["name"] for tool in tools])
        app_schema = next(tool for tool in tools if tool["name"] == "apps_open")["inputSchema"]
        self.assertEqual(["probe"], app_schema["properties"]["app"]["enum"])
        self.assertEqual(["app"], app_schema["required"])
        found_result = self.assert_result(found, 4)
        self.assertIn("physics notes.pdf", self.assert_call_result(found_result))
        self.assertFalse(found_result["isError"])
        self.assertFalse(self.assert_result(launched, 5)["isError"])
        refused_result = self.assert_result(refused, 6)
        self.assertTrue(refused_result["isError"])  # a tool-side error: in the result, not a protocol error
        self.assertIn("app must be one of: probe", self.assert_call_result(refused_result))
        self.assert_error(unknown, 7, -32602)
        self.assertEqual({}, self.assert_result(ping, 8))

        # The broker logs runs only (novacore/SCOPE.md: "logs every run"); the call it
        # refused on its argument rules never ran, so it is not in the outbox.
        events = _events(self.base / "state" / "mcp-events.jsonl")
        self.assertEqual([("files.find", False, True), ("apps.open", False, True)],
                         [(e["capability"]["name"], e["owner_confirmed"], e["applied"]) for e in events])
        self.assertFalse((self.base / "state" / "events.jsonl").exists())  # NOVA's own outbox untouched
        deadline = time.monotonic() + 10
        while not (self.base / "probe-runs").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(["ran"], (self.base / "probe-runs").read_text().splitlines())
        self.assertNotIn(self.CANARY, "".join(json.dumps(r) for r in responses) + stderr)

    def test_versions_errors_and_robustness(self) -> None:
        responses, _, status = _serve(self.config, [
            _request(1, "tools/list"),                                   # before initialize
            _request(2, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                       "clientInfo": {"name": "old", "version": "1"}}),
            _request(3, "tools/list"),
            "not json at all",
            json.dumps([{"jsonrpc": "2.0", "id": 4, "method": "ping"}]),  # a batch
            json.dumps({"jsonrpc": "2.0", "id": None, "method": "ping"}),  # null id
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": [1, 2]}),
            _request(6, "tools/call", {"name": "time_now", "arguments": "now"}),
            '{"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {"x": NaN}}',  # NaN is not JSON
            _request(8, "no/such/method"),
            _request(9, "ping"),
        ])
        self.assertEqual(0, status)
        self.assertEqual(11, len(responses))
        early, init, listing, garbage, batch, null_id, bad_params, bad_arguments, nan, unknown, ping = responses
        self.assert_error(early, 1, -32600)
        self.assertEqual("2024-11-05", self.assert_result(init, 2)["protocolVersion"])
        self.assertTrue(all("annotations" not in tool for tool in self.assert_result(listing, 3)["tools"]))
        self.assert_error(garbage, None, -32700)
        self.assert_error(batch, None, -32600)
        self.assert_error(null_id, None, -32600)
        self.assert_error(bad_params, 5, -32602)
        self.assertTrue(self.assert_result(bad_arguments, 6)["isError"])
        self.assert_error(nan, None, -32700)
        self.assert_error(unknown, 8, -32601)
        self.assertEqual({}, self.assert_result(ping, 9))

    def test_an_unknown_version_gets_one_the_sdk_accepts(self) -> None:
        responses, _, _ = _serve(self.config, [
            _request(1, "initialize", {"protocolVersion": "2099-01-01", "capabilities": {},
                                       "clientInfo": {"name": "future", "version": "1"}})])
        self.assertIn(responses[0]["result"]["protocolVersion"], SDK_HANDSHAKE_VERSIONS)

    def test_one_server_per_state_folder(self) -> None:
        first = subprocess.Popen(
            [sys.executable, "-m", "novacore", "--config", str(self.config), "--mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONPATH": str(ROOT / "novacore" / "src")},
        )
        try:
            assert first.stderr is not None
            self.assertIn("NOVA MCP server", first.stderr.readline().decode("utf-8"))  # it holds the lock now
            _, stderr, status = _serve(self.config, [INITIALIZE])
            self.assertEqual(2, status)
            self.assertIn("another NOVA MCP server", stderr)
        finally:
            first.communicate(b"", timeout=30)
        _, _, status = _serve(self.config, [INITIALIZE])
        self.assertEqual(0, status)  # the lock went with the first server


class InProcessTests(unittest.TestCase, SpecChecks):
    def _server(self, base: Path) -> mcp_server.McpServer:
        (base / "docs").mkdir(exist_ok=True)
        bridge = DesktopBridge(DesktopConfig(apps={"music": (sys.executable,)}, folders=(base / "docs",),
                                             confirm_all_actions=True), FakePlatform())
        actions = [a for a in bridge.action_specs() if a.name in ("apps.open", "system.lock")]
        return mcp_server.McpServer(CapabilityBroker(base / "mcp-events.jsonl", bridge), actions)

    def test_nothing_but_protocol_reaches_stdout_and_no_socket_opens(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = load_config(_write_config(Path(directory.name), ["time.now"]))
        original = mcp_server.McpServer.handle

        def noisy(server: mcp_server.McpServer, message: object) -> dict | None:
            print("a stray print from somewhere")  # e.g. a notice meant for NOVA's own window
            return original(server, message)

        sockets: list[str] = []

        def audit(event: str, args: tuple) -> None:
            if event.startswith("socket"):
                sockets.append(event)

        sys.addaudithook(audit)  # cannot be removed; it only appends to this test's list
        stdin = io.BytesIO(f"{INITIALIZE}\n{_request(3, 'tools/call', {'name': 'time_now', 'arguments': {}})}\n".encode())
        stdout, stderr = io.BytesIO(), io.StringIO()
        with patch.object(mcp_server.McpServer, "handle", noisy), patch("sys.stderr", stderr):
            self.assertEqual(0, mcp_server.run(config, stdin, stdout))
        lines = stdout.getvalue().decode("ascii").splitlines()
        self.assertEqual(2, len(lines))
        self.assertTrue(all(json.loads(line)["jsonrpc"] == "2.0" for line in lines))
        self.assertIn("It is ", json.loads(lines[1])["result"]["content"][0]["text"])
        self.assertEqual(2, stderr.getvalue().count("a stray print"))
        self.assertEqual([], sockets)

    def test_a_broken_outbox_refuses_the_call_and_nothing_runs(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        server = self._server(base)
        server.handle(json.loads(INITIALIZE))
        server.handle(json.loads(_request(3, "tools/call", {"name": "system_lock", "arguments": {}})))
        outbox = base / "mcp-events.jsonl"
        outbox.write_text(outbox.read_text(encoding="utf-8").replace("Screen", "Scream"), encoding="utf-8")
        platform = server.broker.executor.platform  # type: ignore[union-attr]
        before = list(platform.calls)
        response = server.handle(json.loads(_request(4, "tools/call", {"name": "system_lock", "arguments": {}})))
        self.assertTrue(response["result"]["isError"])
        self.assertIn("refused", response["result"]["content"][0]["text"])
        self.assertEqual(before, platform.calls)


class McpConfigTests(unittest.TestCase):
    def _load(self, tools: object, extra: str = "") -> object:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.toml"
        path.write_text(
            '[model]\ncommand = ["/x"]\ntimeout_seconds = 1\nmax_output_chars = 1\n\n'
            '[conversation]\nhistory_turns = 1\nmemory_results = 1\n\n[storage]\nstate_dir = "s"\n\n'
            f"[mcp]\ntools = {json.dumps(tools)}\n{extra}",
            encoding="utf-8",
        )
        return load_config(path)

    def test_only_listed_desktop_capabilities_and_never_the_ones_that_need_nova(self) -> None:
        self.assertEqual(("time.now", "apps.open"), self._load(["time.now", "apps.open"]).mcp.tools)
        refused = {
            "memory.save": "needs your typed yes",
            "timer.set": "has none",
            "system.reboot": "not a desktop capability",
            "updates.apply": "not a desktop capability",
            "rm -rf": "not a desktop capability",
        }
        for name, message in refused.items():
            with self.subTest(name), self.assertRaisesRegex(ConfigError, message):
                self._load(["time.now", name])
        for tools, message in (([], "non-empty"), (["time.now", "time.now"], "twice"), ("time.now", "non-empty")):
            with self.subTest(tools), self.assertRaisesRegex(ConfigError, message):
                self._load(tools)
        with self.assertRaisesRegex(ConfigError, "unknown key"):
            self._load(["time.now"], "network = true\n")

    def test_a_listed_tool_this_desktop_does_not_offer_stops_the_server(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = _write_config(Path(directory.name), ["time.now", "docs.create"])  # no documents_folder
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            status = mcp_server.run(load_config(config), io.BytesIO(b""), io.BytesIO())
        self.assertEqual(2, status)
        self.assertIn("does not offer: docs.create", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
