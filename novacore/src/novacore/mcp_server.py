"""Work order N6: NOVA's actions as an MCP server, for AI apps that speak MCP.

    python -m novacore --config CONFIG --mcp

Transport: MCP stdio. JSON-RPC 2.0 messages, one per line, come in on stdin
and go out on stdout. Nothing else is written to stdout: while the server
runs, a stray print goes to stderr. No socket is opened.

Protocol: the `initialize` handshake, revisions 2024-11-05 to 2025-11-25. A
2026-07-28 client opens with a `server/discover` probe. This server answers it
"method not found", and such a client falls back to the handshake (the
official Python SDK does: mcp 2.3.0, `mcp/client/_probe.py`, read on
2026-11-02).

What a client can do, and nothing more:
- list, and call, one tool per capability the owner listed in `[mcp] tools`
  (config.py);
- each call is model output. It goes to `CapabilityBroker.request` like a
  router proposal: argument rules, a chained outbox (`mcp-events.jsonl`, its
  own file), refusal on a broken log. It is never owner-confirmed;
- `memory.save` and `timer.set` cannot be listed (config.py says why);
- a result is the broker's one-line message. Notes, lessons and history are
  never read here.

One server per state folder: it holds an OS lock on `mcp-events.jsonl.lock`
while it runs. The OS releases the lock if the process dies.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path
import sys
from typing import BinaryIO

from .capabilities import CATALOGUE, CapabilityBroker, Risk
from .config import RuntimeConfig
from .desktop import DesktopBridge, host_platform
from .router import ActionSpec

PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
SERVER_INFO = {"name": "nova", "version": "0.1.0"}  # novacore/pyproject.toml
MAX_LINE_BYTES = 1_000_000

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "These tools act on the owner's computer through NOVA's capability broker. "
    "Each tool is one action the owner allowed for MCP apps; NOVA checks every call "
    "and records it. A result is one line. Nothing here reads the owner's notes."
)


def tool_name(capability: str) -> str:
    """`apps.open` -> `apps_open`: some clients refuse dots in tool names."""
    return capability.replace(".", "_")


def _response(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


class McpServer:
    """Answers one JSON-RPC message at a time. Holds the broker and the listed actions only."""

    def __init__(self, broker: CapabilityBroker, actions: Sequence[ActionSpec], open_world: frozenset[str] = frozenset()) -> None:
        self.broker = broker
        self.actions = {tool_name(action.name): action for action in actions}
        self.open_world = open_world
        self.version: str | None = None  # the negotiated revision, once initialized

    def tool(self, action: ActionSpec) -> dict:
        properties = {}
        for key, spec in action.args.items():
            properties[key] = {**spec.schema(), "description": spec.describe()}
        example = json.dumps(action.example_args, ensure_ascii=False) if action.example_args else "no arguments"
        definition: dict = {
            "name": tool_name(action.name),
            "description": f"{action.description} Example: \"{action.example_words}\" -> {example}.",
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": list(action.args),
                "additionalProperties": False,
            },
        }
        if self.version != "2024-11-05":  # annotations arrived in 2025-03-26
            capability = CATALOGUE[action.name]
            definition["annotations"] = {
                "readOnlyHint": capability.risk == Risk.OBSERVE,
                "destructiveHint": False,
                "openWorldHint": action.name in self.open_world,
            }
        return definition

    def handle(self, message: object) -> dict | None:
        """One message in; the response out, or None for a notification or a response."""
        if isinstance(message, list):
            return _error(None, INVALID_REQUEST, "batches are not supported")
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        if "method" not in message:
            return None  # a response to a request; this server sends none
        method = message["method"]
        if "id" not in message:
            return None  # a notification (initialized, cancelled, ...): nothing to answer
        request_id = message["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return _error(None, INVALID_REQUEST, "id must be a string or an integer")
        if not isinstance(method, str):
            return _error(request_id, INVALID_REQUEST, "method must be a string")
        params = message.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")

        if method == "initialize":
            asked = params.get("protocolVersion")
            if not isinstance(asked, str):
                return _error(request_id, INVALID_PARAMS, "protocolVersion must be a string")
            self.version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1]
            return _response(request_id, {
                "protocolVersion": self.version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            })
        if method == "ping":
            return _response(request_id, {})
        if method in ("tools/list", "tools/call") and self.version is None:
            return _error(request_id, INVALID_REQUEST, "initialize first")
        if method == "tools/list":
            return _response(request_id, {"tools": [self.tool(action) for action in self.actions.values()]})
        if method == "tools/call":
            return self._call(request_id, params)
        return _error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def _call(self, request_id: object, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str):
            return _error(request_id, INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments", {})
        capability = self.actions[name].name if name in self.actions else next(iter(self.actions.values())).name
        if not isinstance(arguments, dict):
            text, failed = "arguments must be a JSON object", True
        else:
            try:
                # Model output: never owner_confirmed. The broker checks the
                # arguments and records the run in mcp-events.jsonl.
                result = self.broker.request(capability, dict(arguments))
            except ValueError as error:  # the outbox does not verify: nothing ran
                text, failed = f"refused: {error}", True
            else:
                failed = result.state != "done"
                text = result.message if result.state in ("done", "failed") else f"{result.state}: {result.message}"
        return _response(request_id, {"content": [{"type": "text", "text": text}], "isError": failed})


def serve(server: McpServer, stdin: BinaryIO, stdout: BinaryIO) -> int:
    """Read messages until stdin closes; write one line per response."""

    def send(response: dict) -> None:
        stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
        stdout.flush()

    for raw in stdin:
        if len(raw) > MAX_LINE_BYTES:
            send(_error(None, INVALID_REQUEST, "message too long"))
            continue
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError):
            send(_error(None, PARSE_ERROR, "Parse error"))
            continue
        try:
            response = server.handle(message)
        except Exception as error:  # never let one message end the session
            request_id = message.get("id") if isinstance(message, dict) else None
            response = _error(request_id, INTERNAL_ERROR, f"internal error: {type(error).__name__}")
        if response is not None:
            send(response)
    return 0


def _lock(path: Path):
    """An exclusive OS lock held while the server runs, or None if another holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # UNVERIFIED: no Windows host
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _to_stderr(message: str) -> None:
    print(f"[NOVA] {message}", file=sys.stderr, flush=True)


def run(config: RuntimeConfig, stdin: BinaryIO | None = None, stdout: BinaryIO | None = None) -> int:
    """`--mcp`: check the configuration, take the lock, and serve until stdin closes."""
    if config.mcp is None or config.desktop is None:
        print("novacore: configuration error: --mcp needs a [desktop] table and an [mcp] table listing the tools",
              file=sys.stderr)
        return 2
    bridge = DesktopBridge(config.desktop, host_platform(), memory=None, notify=_to_stderr)
    offered = {action.name: action for action in bridge.action_specs()}
    missing = [name for name in config.mcp.tools if name not in offered]
    if missing:
        print("novacore: configuration error: [mcp] lists tools this computer's [desktop] table does not offer: "
              + ", ".join(missing), file=sys.stderr)
        return 2
    outbox = config.state_dir / "mcp-events.jsonl"
    lock = _lock(outbox.with_name(outbox.name + ".lock"))
    if lock is None:
        print(f"novacore: another NOVA MCP server is using {config.state_dir}; only one may run at a time",
              file=sys.stderr)
        return 2
    open_world = frozenset({"music.play"}) if config.desktop.spotify_search else frozenset()
    server = McpServer(CapabilityBroker(outbox, bridge), [offered[name] for name in config.mcp.tools], open_world)
    print(f"NOVA MCP server: {len(server.actions)} tool(s): {', '.join(server.actions)}", file=sys.stderr, flush=True)
    protocol_out = stdout if stdout is not None else sys.stdout.buffer
    saved_stdout, sys.stdout = sys.stdout, sys.stderr  # nothing but protocol reaches the real stdout
    try:
        return serve(server, stdin if stdin is not None else sys.stdin.buffer, protocol_out)
    finally:
        sys.stdout = saved_stdout
        lock.close()
