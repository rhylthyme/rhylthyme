"""Minimal Streamable HTTP client for the remote Rhylthyme MCP server.

The server is stateless, so a tool call is one JSON-RPC POST. The reply
comes back either as ``application/json`` or as a short
``text/event-stream``; both are handled. ``initialize`` is sent once per
client so the server sees a well-formed session (and records the client
name) before the first ``tools/call``.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from rhylthyme import __version__ as _pkg_version

DEFAULT_MCP_URL = "https://mcp.rhylthyme.com/mcp"
PROTOCOL_VERSION = "2025-06-18"
VERTICALS = ("kitchen", "lab", "events", "gym")


class McpError(Exception):
    """Transport or protocol failure talking to the MCP server."""


class ToolError(McpError):
    """A tool ran and reported ``isError``. ``text`` is its message."""

    def __init__(self, tool: str, text: str):
        super().__init__(f"{tool}: {text}")
        self.tool = tool
        self.text = text


@dataclass
class ToolResult:
    content: List[Dict[str, Any]]
    structured: Optional[Dict[str, Any]] = None
    is_error: bool = False

    @property
    def text(self) -> str:
        return "\n\n".join(
            c.get("text", "") for c in self.content if c.get("type") == "text"
        )


def base_url() -> str:
    return os.environ.get("RHYLTHYME_MCP_URL", DEFAULT_MCP_URL).rstrip("/")


def endpoint_for(environment: Optional[str], base: Optional[str] = None) -> str:
    """``/kitchen/mcp`` etc. for a vertical, so returned URLs land on the
    matching subdomain; ``/mcp`` otherwise."""
    base = (base or base_url()).rstrip("/")
    env = (environment or "").lower()
    if env in VERTICALS and base.endswith("/mcp"):
        return f"{base[: -len('/mcp')]}/{env}/mcp"
    return base


def parse_rpc_body(body: str) -> Dict[str, Any]:
    """Return the JSON-RPC response from a JSON or SSE body (last one wins)."""
    t = body.strip()
    if t.startswith("{"):
        return json.loads(t)
    messages = []
    for line in t.splitlines():
        if line.startswith("data:"):
            try:
                messages.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                continue
    responses = [
        m for m in messages if isinstance(m, dict) and ("result" in m or "error" in m)
    ]
    if not responses:
        raise McpError(f"No JSON-RPC response in server reply: {t[:200]!r}")
    return responses[-1]


_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n```", re.S)


def extract_program(text: str) -> Optional[Dict[str, Any]]:
    """The last ```json block in a tool's text that looks like a program."""
    for block in reversed(_JSON_BLOCK.findall(text or "")):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("tracks"), list):
            return obj
    return None


@dataclass
class McpClient:
    url: str = field(default_factory=base_url)
    timeout: float = 180.0
    user_agent: str = f"rhylthyme-cli/{_pkg_version}"
    _ids: Any = field(default_factory=lambda: itertools.count(1), repr=False)
    _initialized: bool = field(default=False, repr=False)
    last_headers: Dict[str, str] = field(default_factory=dict, repr=False)

    def send_raw(
        self,
        payload: Dict[str, Any],
        *,
        accept: str = "application/json, text/event-stream",
        timeout: Optional[float] = None,
    ) -> Tuple[int, str, str]:
        """POST one JSON-RPC message; return (status, content type, body).

        HTTP error statuses are returned, not raised, so callers that test
        the server (``mcp-test``) can assert on them.
        """
        headers = {
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "User-Agent": self.user_agent,
        }
        if accept:
            headers["Accept"] = accept
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                self.last_headers = {k.lower(): v for k, v in resp.headers.items()}
                return (
                    resp.status,
                    resp.headers.get("Content-Type", ""),
                    resp.read().decode("utf-8"),
                )
        except urllib.error.HTTPError as e:
            self.last_headers = {k.lower(): v for k, v in e.headers.items()}
            return (
                e.code,
                e.headers.get("Content-Type", ""),
                e.read().decode("utf-8", "replace"),
            )
        except urllib.error.URLError as e:
            raise McpError(f"Could not reach {self.url}: {e.reason}") from e
        except TimeoutError as e:
            raise McpError(f"Timed out waiting for {self.url}") from e

    def _post(
        self, payload: Dict[str, Any], timeout: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        status, _ctype, body = self.send_raw(payload, timeout=timeout)
        if status >= 400:
            raise McpError(f"MCP server returned HTTP {status}: {body[:300]}")
        if "id" not in payload:  # notification
            return None
        msg = parse_rpc_body(body)
        if "error" in msg:
            err = msg["error"] or {}
            raise McpError(f"MCP error {err.get('code')}: {err.get('message')}")
        return msg.get("result") or {}

    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Any JSON-RPC request (tools/list, resources/read, prompts/get, ...)."""
        if not self._initialized and method != "initialize":
            self.initialize()
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        return self._post(payload, timeout=timeout) or {}

    def initialize(self) -> Dict[str, Any]:
        result = self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "rhylthyme-cli", "version": _pkg_version},
                },
            },
            timeout=30,
        )
        self._post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=30
        )
        self._initialized = True
        return result or {}

    def call_tool(
        self,
        name: str,
        arguments: Dict[str, Any],
        timeout: Optional[float] = None,
        raise_on_error: bool = True,
    ) -> ToolResult:
        if not self._initialized:
            self.initialize()
        result = (
            self._post(
                {
                    "jsonrpc": "2.0",
                    "id": next(self._ids),
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                timeout=timeout,
            )
            or {}
        )
        tr = ToolResult(
            content=result.get("content") or [],
            structured=result.get("structuredContent"),
        )
        tr.is_error = bool(result.get("isError"))
        if tr.is_error and raise_on_error:
            raise ToolError(name, tr.text or "tool reported an error")
        return tr
