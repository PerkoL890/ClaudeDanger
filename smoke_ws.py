"""Programmatic smoke test: drive the agent over the WebSocket, read-only."""
import asyncio
import json
import sys
from pathlib import Path

import websockets

PROJECT = str(Path(__file__).parent.resolve())
URL = "ws://127.0.0.1:8765/ws"
PROMPT = (
    "Using Serena's semantic tools, find the symbol named `run_turn` in app.py. "
    "Report its file location and how many symbols reference it. "
    "Do NOT modify any files."
)


async def main() -> None:
    async with websockets.connect(URL, max_size=None) as ws:
        print("connected")
        # initial status
        print("status:", await ws.recv())

        await ws.send(json.dumps({"type": "set_project", "path": PROJECT}))
        # wait for project ack
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if msg.get("type") == "project":
                print("project:", msg)
                if not msg.get("ok"):
                    print("PROJECT FAILED"); return
                break

        await ws.send(json.dumps({"type": "user_message", "session_id": "s1", "text": PROMPT}))

        text_chunks: list[str] = []
        tool_calls: dict[str, str] = {}
        tool_results: list[tuple[str, str]] = []
        thinking_len = 0
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=300)
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "assistant_text_delta":
                text_chunks.append(msg["text"])
            elif t == "thinking_delta":
                thinking_len += len(msg.get("text", ""))
            elif t == "tool_call":
                tool_calls[msg["id"]] = msg["name"]
                print(f"  [tool_call] {msg['name']}")
            elif t == "tool_args":
                print(f"  [tool_args] {tool_calls.get(msg['id'],'?')} -> {json.dumps(msg['args'])[:160]}")
            elif t == "tool_result":
                snippet = (msg.get("result") or "")[:160].replace("\n", " ")
                tool_results.append((msg["status"], snippet))
                print(f"  [tool_result:{msg['status']}] {snippet}")
            elif t == "error":
                print("  [ERROR]", msg.get("message"))
            elif t == "turn_complete":
                print(f"turn_complete  cost=${msg.get('cost')}")
                break
            elif t == "interrupted":
                print("interrupted"); break

        print("\n--- SUMMARY ---")
        print("tool calls:", list(tool_calls.values()))
        print("tool results:", [s for s, _ in tool_results])
        print("thinking chars:", thinking_len)
        print("assistant text:\n", "".join(text_chunks).strip()[:1200])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        print("SMOKE FAILED:", type(exc).__name__, exc)
        sys.exit(1)
