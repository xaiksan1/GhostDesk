#!/usr/bin/env python3
"""ghostdesk — token-efficient CLI for GhostDesk MCP server.

Requires: GhostDesk Docker container running (http://localhost:3000/mcp)
Install:  ln -sf $PWD/scripts/ghostdesk-cli.py ~/.local/bin/ghostdesk
          chmod +x ~/.local/bin/ghostdesk

Env vars:
  GHOSTDESK_URL      MCP endpoint  (default: http://localhost:3000/mcp)
  GHOSTDESK_SHOT_DIR Screenshot output dir  (default: /tmp/ghostdesk-shots)
"""
import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path

import httpx

_URL = os.environ.get("GHOSTDESK_URL", "http://localhost:3000/mcp")
_SHOT_DIR = Path(os.environ.get("GHOSTDESK_SHOT_DIR", "/tmp/ghostdesk-shots"))
_ID = 0
_SESSION_ID: str | None = None


# ──────────────────────────── MCP transport ────────────────────────────


def _rpc(client: httpx.Client, method: str, params: dict, rpc_id: int) -> tuple[dict, str | None]:
    """Send one JSON-RPC request; return (result_dict, session_id_or_None)."""
    global _SESSION_ID
    headers: dict[str, str] = {"Accept": "application/json, text/event-stream"}
    if _SESSION_ID:
        headers["Mcp-Session-Id"] = _SESSION_ID
    payload = {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}
    resp = client.post(_URL, json=payload, headers=headers)
    resp.raise_for_status()
    # Capture session ID from response header (Streamable HTTP protocol)
    sid = resp.headers.get("Mcp-Session-Id")
    ct = resp.headers.get("content-type", "")
    if "event-stream" in ct:
        result = _parse_sse(resp.text)
    else:
        data = resp.json()
        if "error" in data:
            _die(data["error"].get("message", str(data["error"])))
        result = data.get("result", {})
    return result, sid


def _init_session(client: httpx.Client) -> None:
    """Send MCP initialize + initialized, store session ID."""
    global _SESSION_ID
    result, sid = _rpc(client, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {}},
        "clientInfo": {"name": "ghostdesk-cli", "version": "1.0.0"},
    }, rpc_id=0)
    if sid:
        _SESSION_ID = sid
    # Send initialized notification (no response expected)
    headers: dict[str, str] = {"Accept": "application/json, text/event-stream"}
    if _SESSION_ID:
        headers["Mcp-Session-Id"] = _SESSION_ID
    client.post(_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)


def _call(name: str, arguments: dict) -> str:
    global _ID
    _ID += 1
    try:
        with httpx.Client(timeout=120) as client:
            _init_session(client)
            result, _ = _rpc(client, "tools/call", {"name": name, "arguments": arguments}, _ID)
    except httpx.ConnectError:
        _die("GhostDesk unreachable — is Docker container running? (port 3000)")
    except httpx.HTTPStatusError as exc:
        _die(f"HTTP {exc.response.status_code}: {exc.response.text[:300]}")
    return _render(result)


def _parse_sse(text: str) -> dict:
    """Extract the first JSON-RPC result from an SSE stream."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            msg = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if "error" in msg:
            _die(msg["error"].get("message", str(msg["error"])))
        if "result" in msg:
            return msg["result"]
    return {}


def _render(result: dict) -> str:
    """Convert MCP CallToolResult to compact text."""
    if not isinstance(result, dict):
        return str(result)

    content = result.get("content", [])
    if not content:
        # FastMCP may put the actual data in structuredContent.result
        structured = result.get("structuredContent", {})
        inner = structured.get("result", structured)
        if inner is None or inner == {} or inner == []:
            return ""
        return json.dumps(inner, ensure_ascii=False, indent=2)

    parts: list[str] = []
    for item in content:
        item_type = item.get("type")
        if item_type == "text":
            parts.append(item["text"])
        elif item_type == "image":
            _SHOT_DIR.mkdir(parents=True, exist_ok=True)
            ext = (item.get("mimeType") or "image/webp").split("/")[-1]
            path = _SHOT_DIR / f"shot-{int(time.time() * 1000)}.{ext}"
            path.write_bytes(base64.b64decode(item["data"]))
            parts.append(str(path))

    return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)


# ──────────────────────────── formatting ───────────────────────────────


def _fmt_feedback(raw: str) -> str:
    """Collapse mouse/keyboard feedback dict → one-liner."""
    try:
        d = json.loads(raw)
        parts = [d.get("action", "")]
        if "screen_changed" in d:
            parts.append("changed" if d["screen_changed"] else "no-change")
        if "reaction_time_ms" in d:
            parts.append(f"{d['reaction_time_ms']}ms")
        return " | ".join(p for p in parts if p)
    except (json.JSONDecodeError, AttributeError, TypeError):
        return raw


def _fmt_exec(raw: str, timeout_s: int) -> None:
    """Print exec_shell result: stdout → stdout, stderr → stderr, exit on rc."""
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        print(raw)
        return

    stdout = d.get("stdout", "")
    stderr = d.get("stderr", "")
    timed_out = d.get("timed_out", False)
    rc = d.get("returncode", 0)

    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    if timed_out:
        print(
            f"ghostdesk: warning: command timed out after {timeout_s}s",
            file=sys.stderr,
        )
    sys.exit(0 if rc == 0 else 1)


def _die(msg: str) -> None:
    print(f"ghostdesk: error: {msg}", file=sys.stderr)
    sys.exit(1)


# ──────────────────────────── argument parsing ─────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ghostdesk",
        description="Thin CLI wrapper for GhostDesk MCP desktop server.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""commands:
  shot      Capture screenshot → prints saved file path
  click     Single click at (x, y)
  dclick    Double-click at (x, y)
  move      Move cursor to (x, y)
  drag      Drag from (x1,y1) to (x2,y2)
  scroll    Scroll at (x, y) in direction
  type      Type text at current focus
  press     Press key or chord  e.g. "ctrl+c"
  clip get  Read clipboard
  clip set  Write clipboard
  apps      List installed GUI applications
  ps        List running GUI processes
  launch    Launch an app by exec string (from 'apps')
  status    Get stdout tail of a running process
  exec      Run shell command in container""",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # shot ────────────────────────────────────────────────────────────
    s = sub.add_parser("shot", help="Screenshot → file path")
    s.add_argument("--region", metavar="x,y,w,h", help="Capture sub-region")
    s.add_argument("--format", choices=["webp", "png"], default="webp")
    s.add_argument("--quality", type=int, default=85, metavar="N")
    s.add_argument("--no-stabilize", action="store_true", help="Skip stability wait")

    # click ───────────────────────────────────────────────────────────
    s = sub.add_parser("click", help="Click at x y")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s.add_argument("--button", choices=["left", "right", "middle"], default="left")

    # dclick ──────────────────────────────────────────────────────────
    s = sub.add_parser("dclick", help="Double-click at x y")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)

    # move ────────────────────────────────────────────────────────────
    s = sub.add_parser("move", help="Move cursor to x y")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)

    # drag ────────────────────────────────────────────────────────────
    s = sub.add_parser("drag", help="Drag from x1 y1 to x2 y2")
    s.add_argument("x1", type=int)
    s.add_argument("y1", type=int)
    s.add_argument("x2", type=int)
    s.add_argument("y2", type=int)
    s.add_argument("--button", choices=["left", "right", "middle"], default="left")

    # scroll ──────────────────────────────────────────────────────────
    s = sub.add_parser("scroll", help="Scroll at x y")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s.add_argument("direction", choices=["up", "down", "left", "right"])
    s.add_argument("--amount", type=int, default=3, metavar="N")

    # type ────────────────────────────────────────────────────────────
    s = sub.add_parser("type", help="Type text at current focus")
    s.add_argument("text")

    # press ───────────────────────────────────────────────────────────
    s = sub.add_parser("press", help='Press key or chord e.g. "ctrl+c"')
    s.add_argument("keys")

    # clip ────────────────────────────────────────────────────────────
    s = sub.add_parser("clip", help="Clipboard operations")
    s.add_argument("action", choices=["get", "set"])
    s.add_argument("text", nargs="?", default="", help="Text for 'set'")

    # apps / ps ───────────────────────────────────────────────────────
    sub.add_parser("apps", help="List installed GUI apps")
    sub.add_parser("ps", help="List running GUI processes")

    # launch ──────────────────────────────────────────────────────────
    s = sub.add_parser("launch", help="Launch app by exec string")
    s.add_argument("command")

    # status ──────────────────────────────────────────────────────────
    s = sub.add_parser("status", help="Get process stdout tail")
    s.add_argument("pid", type=int)
    s.add_argument("--lines", type=int, default=50, metavar="N")

    # exec ────────────────────────────────────────────────────────────
    s = sub.add_parser("exec", help="Run shell command in Docker container")
    s.add_argument("command")
    s.add_argument("--timeout", type=int, default=30, metavar="SECS")
    s.add_argument("--workdir", metavar="DIR", default=None)

    return p


# ──────────────────────────── dispatch ─────────────────────────────────


def main() -> None:
    args = _build_parser().parse_args()
    cmd = args.cmd

    if cmd == "shot":
        region = None
        if args.region:
            parts = args.region.split(",")
            if len(parts) != 4:
                _die("--region must be x,y,w,h (four integers)")
            x, y, w, h = map(int, parts)
            region = {"x": x, "y": y, "width": w, "height": h}
        print(_call("screen_shot", {
            "region": region,
            "format": args.format,
            "stabilize": not args.no_stabilize,
            "quality": args.quality,
        }))

    elif cmd == "click":
        print(_fmt_feedback(_call("mouse_click", {
            "x": args.x, "y": args.y, "button": args.button,
        })))

    elif cmd == "dclick":
        print(_fmt_feedback(_call("mouse_double_click", {
            "x": args.x, "y": args.y,
        })))

    elif cmd == "move":
        print(_fmt_feedback(_call("mouse_move", {"x": args.x, "y": args.y})))

    elif cmd == "drag":
        print(_fmt_feedback(_call("mouse_drag", {
            "x1": args.x1, "y1": args.y1,
            "x2": args.x2, "y2": args.y2,
            "button": args.button,
        })))

    elif cmd == "scroll":
        print(_fmt_feedback(_call("mouse_scroll", {
            "x": args.x, "y": args.y,
            "direction": args.direction,
            "amount": args.amount,
        })))

    elif cmd == "type":
        print(_fmt_feedback(_call("key_type", {"text": args.text})))

    elif cmd == "press":
        print(_fmt_feedback(_call("key_press", {"keys": args.keys})))

    elif cmd == "clip":
        if args.action == "get":
            print(_call("clipboard_get", {}))
        else:
            if not args.text:
                _die("'clip set' requires a text argument")
            print(_call("clipboard_set", {"text": args.text}))

    elif cmd == "apps":
        print(_call("app_list", {}))

    elif cmd == "ps":
        print(_call("app_running", {}))

    elif cmd == "launch":
        print(_call("app_launch", {"command": args.command}))

    elif cmd == "status":
        print(_call("app_status", {"pid": args.pid, "lines": args.lines}))

    elif cmd == "exec":
        raw = _call("exec_shell", {
            "command": args.command,
            "timeout": args.timeout,
            "workdir": args.workdir,
        })
        _fmt_exec(raw, args.timeout)


if __name__ == "__main__":
    main()
