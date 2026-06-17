"""
Desktop coding agent with a web-based GUI, powered by the Claude Agent SDK.

Uses your existing Claude Code **subscription login** (no API key). Serena is
integrated as a local stdio MCP server for semantic code operations; `bash`
(built-in, with a denylist) and a custom `git_checkpoint` tool round it out.
Every tool runs with no confirmation prompts -- the only gate is a bash
denylist, enforced through the SDK's permission callback ("I control the
permission layer").

Run:
    python app.py
then open http://localhost:8765 (opens automatically). Choose the project to
work on inside the GUI.

Why the Agent SDK (vs. the raw Anthropic API): the raw Messages API needs a
separate billed API key and only accepts *remote* MCP servers. The Agent SDK
runs on the subscription you already have, manages local stdio MCP servers
(Serena) natively, executes the agent loop, and exposes a permission callback
for the no-prompts behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    get_session_messages,
    list_sessions,
    tool,
)
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"

# bash safety rails: deny these outright (returned to Claude as a denied tool
# call instead of executing). Everything else runs with no prompt.
BASH_DENYLIST = [
    r"rm\s+-rf\s+/(?:\s|$)",
    r"rm\s+-rf\s+~",
    r"rm\s+-rf\s+\$HOME",
    r"rm\s+-rf\s+--no-preserve-root",
    r"mkfs",
    r"dd\s+if=",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;",  # classic fork bomb
]

GIT_TIMEOUT = 120
CLIENT_CONNECT_TIMEOUT = 300  # cold uvx Serena build can be slow

# Built-in + MCP tools we auto-approve without a callback round-trip. Bash is
# deliberately NOT here so every bash command passes through can_use_tool for
# the denylist check.
ALLOWED_TOOLS = ["mcp__serena__*", "mcp__local__*"]


def build_system_prompt(project: Path | None) -> str:
    where = (
        f"the project at {project}"
        if project
        else "no project yet -- ask the user to select one before doing code work"
    )
    return (
        f"You are a coding agent operating on {where}. You have Serena's semantic "
        "code tools (prefer these for code work -- symbol search, references, "
        "structured edits), the built-in bash tool, and a git_checkpoint tool. "
        "Execute tasks directly with tools instead of asking the user to run "
        "things. Be concise in prose; let the tool calls show the work. "
        "When a request is genuinely ambiguous or hinges on a decision only the "
        "user can make, call the ask_user tool to ask a focused multiple-choice "
        "question rather than guessing or doing free-form Q&A in prose. Don't "
        "overuse it for things you can determine yourself from the code."
    )


def _bash_denied(command: str) -> str | None:
    normalized = " ".join(command.split())
    for pattern in BASH_DENYLIST:
        if re.search(pattern, normalized):
            return f"Refused: command matches a denylisted destructive pattern ({pattern!r})."
    return None


# --------------------------------------------------------------------------
# Custom in-process tool: git_checkpoint
# --------------------------------------------------------------------------


def make_git_checkpoint(project: Path):
    """Build a git_checkpoint tool bound to a specific project directory, so each
    chat session commits in its own project."""

    @tool(
        "git_checkpoint",
        "Stage all changes and create a git commit "
        "(git add -A && git commit -m <message> --allow-empty) in this project.",
        {"message": str},
    )
    async def git_checkpoint(args: dict[str, Any]) -> dict[str, Any]:
        message = str(args.get("message", "checkpoint"))
        parts: list[str] = []
        is_error = False
        for cmd in (["git", "add", "-A"], ["git", "commit", "-m", message, "--allow-empty"]):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(project),
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT)
                parts.append(f"$ {' '.join(cmd)}\n{out.decode(errors='replace')}".rstrip())
                if proc.returncode not in (0, None):
                    is_error = True
            except Exception as exc:  # noqa: BLE001
                parts.append(f"$ {' '.join(cmd)}\nFailed: {exc}")
                is_error = True
                break
        return {"content": [{"type": "text", "text": "\n\n".join(parts) or "(no output)"}], "isError": is_error}

    return git_checkpoint


# --------------------------------------------------------------------------
# Custom in-process tool: ask_user (Claude -> GUI question -> answer)
# --------------------------------------------------------------------------

# JSON Schema for ask_user. Mirrors Claude Code's AskUserQuestion shape so the
# model already knows how to drive it: a list of multiple-choice questions, each
# with a short header chip, the question text, options (label + description), and
# whether multiple options may be selected.
ASK_USER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "description": "One or more questions to ask the user.",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question to ask."},
                    "header": {
                        "type": "string",
                        "description": "Very short label/topic for the question (a few words).",
                    },
                    "multiSelect": {
                        "type": "boolean",
                        "description": "If true, the user may pick more than one option.",
                    },
                    "options": {
                        "type": "array",
                        "description": "The choices to offer.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "Short option text."},
                                "description": {
                                    "type": "string",
                                    "description": "Optional longer explanation of the option.",
                                },
                            },
                            "required": ["label"],
                        },
                    },
                },
                "required": ["question", "options"],
            },
        }
    },
    "required": ["questions"],
}


def _format_answers(answers: list[dict[str, Any]] | None) -> str:
    if not answers:
        return "The user did not answer the questions."
    lines: list[str] = []
    for a in answers:
        q = a.get("question") or a.get("header") or "question"
        sel = a.get("answer")
        if isinstance(sel, str):
            sel = [sel]
        sel = [str(x) for x in (sel or [])]
        lines.append(f"Q: {q}\nA: {', '.join(sel) if sel else '(no selection)'}")
    return "\n\n".join(lines)


def make_ask_user(session: "Session"):
    """Build an ask_user tool bound to a chat session, so the question is routed
    to that session's WebSocket and the answer comes back on the same socket."""

    @tool(
        "ask_user",
        "Ask the user one or more multiple-choice questions and wait for their "
        "answer. Use when you need a decision or clarification before proceeding "
        "(e.g. narrowing an ambiguous request). Returns the user's selections, "
        "or notes that they skipped.",
        ASK_USER_SCHEMA,
    )
    async def ask_user(args: dict[str, Any]) -> dict[str, Any]:
        questions = args.get("questions") or []
        ws = session.ws
        if ws is None:
            return {
                "content": [{"type": "text", "text": "No UI is connected to ask the user."}],
                "isError": True,
            }
        qid = uuid4().hex
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        session.pending_questions[qid] = fut
        try:
            await ws.send_text(
                json.dumps({"type": "ask_question", "id": qid, "questions": questions})
            )
        except Exception as exc:  # noqa: BLE001
            session.pending_questions.pop(qid, None)
            return {
                "content": [{"type": "text", "text": f"Failed to deliver question: {exc}"}],
                "isError": True,
            }
        try:
            answers = await fut  # resolved by the 'ask_response' WS handler
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await ws.send_text(json.dumps({"type": "ask_cancel", "id": qid}))
            raise
        finally:
            session.pending_questions.pop(qid, None)
        return {"content": [{"type": "text", "text": _format_answers(answers)}]}

    return ask_user


# --------------------------------------------------------------------------
# Permission callback -- the entire permission layer
# --------------------------------------------------------------------------


async def can_use_tool(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    if tool_name == "Bash" or tool_name.endswith("__bash"):
        denied = _bash_denied(str(tool_input.get("command", "")))
        if denied:
            return PermissionResultDeny(message=denied)
    return PermissionResultAllow()


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@dataclass
class Session:
    sid: str
    project: Path | None = None  # each chat is bound to its own project
    client: ClaudeSDKClient | None = None
    task: asyncio.Task | None = None
    cost: float = 0.0
    interrupting: bool = False
    resume_id: str | None = None  # resume this Claude session on first message
    ws: WebSocket | None = None  # current socket, so ask_user can reach the GUI
    pending_questions: dict[str, asyncio.Future] = field(default_factory=dict)


@dataclass
class Context:
    args: argparse.Namespace
    last_project: Path | None = None  # default for new chats; remembered across runs
    serena_connected: bool = False
    sessions: dict[str, Session] = field(default_factory=dict)
    resume_id: str | None = None  # one-shot: next client resumes this session id


ctx: Context  # populated in main()


# --------------------------------------------------------------------------
# Serena (external stdio MCP server) config
# --------------------------------------------------------------------------


def serena_mcp_config(project: Path) -> dict[str, Any]:
    uvx = shutil.which("uvx") or "uvx"
    return {
        "type": "stdio",
        "command": uvx,
        "args": [
            "--from",
            "git+https://github.com/oraios/serena",
            "serena",
            "start-mcp-server",
            "--context",
            ctx.args.serena_context,
            "--project",
            str(project),
            "--transport",
            "stdio",
            "--enable-web-dashboard",
            "False",
            "--enable-gui-log-window",
            "False",
        ],
        "env": dict(os.environ),
    }


def build_options(
    session: "Session", resume: str | None = None, fork: bool = True
) -> ClaudeAgentOptions:
    project = session.project
    assert project is not None  # callers guarantee a project is set
    local_server = create_sdk_mcp_server(
        "local", "1.0.0", [make_git_checkpoint(project), make_ask_user(session)]
    )
    opts = ClaudeAgentOptions(
        system_prompt=build_system_prompt(project),
        model=ctx.args.model,  # None -> subscription default
        cwd=str(project),
        permission_mode="default",  # routes through can_use_tool
        can_use_tool=can_use_tool,
        allowed_tools=ALLOWED_TOOLS,
        mcp_servers={"serena": serena_mcp_config(project), "local": local_server},
        include_partial_messages=True,
        setting_sources=[],  # don't inherit the user's ~/.claude project settings
    )
    if resume:
        # Continue an existing Claude Code / SDK session (shared on-disk store).
        # fork_session keeps the original session intact and continues a copy.
        opts.resume = resume
        opts.fork_session = fork
    return opts


async def get_or_create_client(session: Session) -> ClaudeSDKClient | None:
    if session.client is not None:
        return session.client
    if session.project is None:
        return None
    resume = session.resume_id or ctx.resume_id
    ctx.resume_id = None  # one-shot
    session.resume_id = None
    client = ClaudeSDKClient(
        options=build_options(session, resume=resume, fork=ctx.args.fork)
    )
    await asyncio.wait_for(client.connect(), timeout=CLIENT_CONNECT_TIMEOUT)
    session.client = client
    return client


async def close_client(session: Session) -> None:
    if session.client is not None:
        with contextlib.suppress(Exception):
            await session.client.disconnect()
        session.client = None


# --------------------------------------------------------------------------
# Project selection
# --------------------------------------------------------------------------


LAST_PROJECT_FILE = Path(__file__).parent / ".last_project"


def _project_store_dir(project: Path) -> Path:
    """Where Claude Code stores this project's session transcripts."""
    base = Path.home() / ".claude" / "projects"
    try:
        from claude_agent_sdk import project_key_for_directory

        return base / project_key_for_directory(str(project))
    except Exception:  # noqa: BLE001
        return base / re.sub(r"[:\\/]", "-", str(project))


def find_latest_session(project: Path) -> str | None:
    """Newest session id (transcript stem) for a project, or None."""
    store = _project_store_dir(project)
    if not store.is_dir():
        return None
    files = sorted(store.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem if files else None


def remember_last_project(path: Path) -> None:
    ctx.last_project = path
    with contextlib.suppress(Exception):
        LAST_PROJECT_FILE.write_text(str(path), encoding="utf-8")


async def set_session_project(session: Session, path: Path) -> tuple[bool, str]:
    """Bind ONE chat to a project. Only that chat's client is rebuilt; other
    chats keep their own projects."""
    path = path.expanduser()
    if not path.is_dir():
        return False, f"Not a directory: {path}"
    path = path.resolve()
    changed = path != session.project
    session.project = path
    remember_last_project(path)  # default for the next new chat
    if changed:
        # This chat's client is bound to the old cwd/Serena project; drop it so
        # the next message rebuilds against the new project.
        if session.task and not session.task.done():
            session.task.cancel()
        await close_client(session)
    return True, f"This chat now works on {path}. Serena activates it on the next message."


# --------------------------------------------------------------------------
# Tool-result helpers
# --------------------------------------------------------------------------


def build_history_items(session_id: str, project: Path) -> list[dict[str, Any]]:
    """Rebuild a clean, frontend-renderable transcript from a saved session, so
    resuming a conversation actually shows its past messages."""
    try:
        msgs = get_session_messages(session_id, directory=str(project))
    except Exception:  # noqa: BLE001
        return []
    items: list[dict[str, Any]] = []
    seen: dict[str, int] = {}  # assistant message id -> index (collapse stream partials)
    tools: dict[str, dict[str, Any]] = {}  # tool_use_id -> tool part (to attach results)
    for sm in msgs:
        msg = sm.message or {}
        if sm.type == "user":
            content = msg.get("content")
            if isinstance(content, str):
                if content.strip():
                    items.append({"role": "user", "text": content})
            elif isinstance(content, list):
                texts: list[str] = []
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    t = b.get("type")
                    if t == "tool_result":
                        part = tools.get(b.get("tool_use_id"))
                        if part is not None:
                            part["result"] = _stringify_tool_content(b.get("content"))
                            part["status"] = "error" if b.get("is_error") else "done"
                    elif t == "text":
                        texts.append(b.get("text", ""))
                    elif t == "image":
                        texts.append("🖼 [image]")
                joined = "\n".join(x for x in texts if x)
                if joined.strip():
                    items.append({"role": "user", "text": joined})
        elif sm.type == "assistant":
            mid = msg.get("id")
            parts: list[dict[str, Any]] = []
            thinking = ""
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    parts.append({"kind": "text", "text": b.get("text", "")})
                elif t == "thinking":
                    thinking += b.get("thinking", "") or ""
                elif t == "tool_use":
                    parts.append(
                        {
                            "kind": "tool",
                            "id": b.get("id"),
                            "name": b.get("name"),
                            "args": b.get("input"),
                            "result": None,
                            "status": "done",
                            "open": False,
                            "output": "",
                        }
                    )
            item = {"role": "assistant", "thinking": thinking, "parts": parts}
            if mid is not None and mid in seen:
                items[seen[mid]] = item  # replace earlier partial with fuller version
            else:
                if mid is not None:
                    seen[mid] = len(items)
                items.append(item)
            for p in parts:
                if p.get("kind") == "tool" and p.get("id"):
                    tools[p["id"]] = p
    return items


def _stringify_tool_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                else:
                    parts.append(json.dumps(block))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


# --------------------------------------------------------------------------
# Agent turn -> WebSocket events (same protocol the frontend already speaks)
# --------------------------------------------------------------------------


async def run_turn(
    ws: WebSocket, session: Session, user_text: str, images: list[dict[str, str]] | None = None
) -> None:
    async def send(payload: dict[str, Any]) -> None:
        await ws.send_text(json.dumps(payload))

    session.ws = ws  # ensure ask_user routes to this turn's socket

    if session.project is None:
        await send({"type": "error", "message": "No project selected for this chat. Pick one first."})
        return

    try:
        client = await get_or_create_client(session)
    except Exception as exc:  # noqa: BLE001
        await send({"type": "error", "message": f"Failed to start agent/Serena: {exc}"})
        return
    if client is None:
        await send({"type": "error", "message": "No project selected. Pick a project first."})
        return

    # First successful connection: report Serena status to the top bar.
    if not ctx.serena_connected:
        ctx.serena_connected = True
        await send(serena_status_payload())

    await send({"type": "assistant_message_start"})
    seen_tool_ids: set[str] = set()
    session.interrupting = False

    try:
        if images:
            blocks: list[dict[str, Any]] = []
            if user_text:
                blocks.append({"type": "text", "text": user_text})
            for img in images:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img.get("media_type", "image/png"),
                            "data": img.get("data", ""),
                        },
                    }
                )

            async def _input():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": blocks},
                    "parent_tool_use_id": None,
                    "session_id": "default",
                }

            await client.query(_input())
        else:
            await client.query(user_text)
        async for msg in client.receive_response():
            if isinstance(msg, StreamEvent):
                ev = msg.event or {}
                etype = ev.get("type")
                if etype == "content_block_start":
                    cb = ev.get("content_block", {})
                    if cb.get("type") == "tool_use":
                        tid = cb.get("id", "")
                        seen_tool_ids.add(tid)
                        await send({"type": "tool_call", "id": tid, "name": cb.get("name", "")})
                elif etype == "content_block_delta":
                    d = ev.get("delta", {})
                    if d.get("type") == "text_delta":
                        await send({"type": "assistant_text_delta", "text": d.get("text", "")})
                    elif d.get("type") == "thinking_delta":
                        await send({"type": "thinking_delta", "text": d.get("thinking", "")})

            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        if block.id not in seen_tool_ids:
                            await send({"type": "tool_call", "id": block.id, "name": block.name})
                            seen_tool_ids.add(block.id)
                        await send({"type": "tool_args", "id": block.id, "args": block.input})
                if getattr(msg, "error", None):
                    await send({"type": "error", "message": str(msg.error)})

            elif isinstance(msg, UserMessage):
                content = msg.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            await send(
                                {
                                    "type": "tool_result",
                                    "id": block.tool_use_id,
                                    "status": "error" if block.is_error else "done",
                                    "result": _stringify_tool_content(block.content),
                                }
                            )

            elif isinstance(msg, ResultMessage):
                if msg.total_cost_usd is not None:
                    session.cost = msg.total_cost_usd
                # A user interrupt also ends the turn with a non-success subtype;
                # that's not an error -- only surface genuine failures.
                if msg.is_error and msg.subtype != "success" and not session.interrupting:
                    await send(
                        {"type": "error", "message": msg.result or f"turn ended: {msg.subtype}"}
                    )

            elif isinstance(msg, SystemMessage):
                pass  # init / status frames -- not surfaced in the UI

        if session.interrupting:
            await send({"type": "assistant_text_delta", "text": "\n\n_(interrupted)_"})
        await send({"type": "turn_complete", "usage": {}, "cost": session.cost})
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await send({"type": "interrupted"})
        raise
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        with contextlib.suppress(Exception):
            await send({"type": "turn_complete", "usage": {}, "cost": session.cost})
    finally:
        session.interrupting = False


# --------------------------------------------------------------------------
# Status helpers
# --------------------------------------------------------------------------


def serena_status_payload() -> dict[str, Any]:
    return {
        "type": "status",
        "project": str(ctx.last_project) if ctx.last_project else None,
        "model": ctx.args.model or "subscription default",
        "serena": "connected" if ctx.serena_connected else "unknown",
        "tool_count": 0,
    }


# --------------------------------------------------------------------------
# FastAPI app
# --------------------------------------------------------------------------


def build_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Pick the initial project: explicit --project-dir wins, else the last
        # project we used (remembered across restarts), else none (pick in GUI).
        initial = ctx.args.project_dir
        if initial is None and LAST_PROJECT_FILE.exists():
            with contextlib.suppress(Exception):
                cand = Path(LAST_PROJECT_FILE.read_text(encoding="utf-8").strip())
                if cand.is_dir():
                    initial = cand
        if initial is not None and initial.is_dir():
            remember_last_project(initial.resolve())
            print(f"[project] default project: {ctx.last_project}", flush=True)

        # Optionally resume the most recent session for the default project.
        if ctx.resume_id is None and ctx.args.resume_last and ctx.last_project is not None:
            ctx.resume_id = find_latest_session(ctx.last_project)
            print(f"[resume] latest session: {ctx.resume_id or '(none found)'}", flush=True)
        elif ctx.resume_id:
            print(f"[resume] session: {ctx.resume_id} (fork={ctx.args.fork})", flush=True)
        print(
            f"\n  Agent running on http://localhost:{ctx.args.port} "
            f"-- no confirmation prompts (bash denylist only).\n"
            f"  Auth:    Claude subscription (via Agent SDK; no API key)\n"
            f"  Project: {ctx.last_project or '(none yet -- choose one in the GUI)'}\n"
            f"  Model:   {ctx.args.model or 'subscription default'}\n"
            f"  Serena:  launched per chat session on first message\n",
            flush=True,
        )
        try:
            yield
        finally:
            for session in list(ctx.sessions.values()):
                if session.task and not session.task.done():
                    session.task.cancel()
                await close_client(session)

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def no_cache(request, call_next):
        # We iterate on the frontend constantly; never let the browser serve a
        # stale app.js/css/html. (Local single-user app -- caching buys nothing.)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/tools")
    async def api_tools() -> JSONResponse:
        native = [
            {"name": "Bash", "description": "Built-in shell tool. Denylisted destructive commands are refused; everything else runs with no prompt."},
            {"name": "git_checkpoint", "description": "git add -A && git commit -m <msg> --allow-empty in the active project."},
            {"name": "ask_user", "description": "Ask the user multiple-choice question(s) in the GUI and wait for their answer."},
        ]
        serena: list[dict[str, str]] = []
        for session in ctx.sessions.values():
            if session.client is not None:
                with contextlib.suppress(Exception):
                    status = await session.client.get_mcp_status()
                    serena = _extract_serena_tools(status)
                    break
        return JSONResponse(
            {
                "serena_connected": ctx.serena_connected,
                "native": native,
                "serena": serena,
                "note": "Claude Code's built-in tools (Read, Edit, Grep, Glob, …) are also available; the system prompt prefers Serena for code work.",
            }
        )

    @app.get("/api/sessions")
    async def api_sessions() -> JSONResponse:
        """Past Claude Code / SDK conversations, newest first (the 'Recents' list)."""
        items: list[dict[str, Any]] = []
        try:
            infos = list_sessions(limit=80)
            with contextlib.suppress(Exception):
                infos = sorted(infos, key=lambda s: s.last_modified, reverse=True)
            for s in infos:
                title = (
                    s.custom_title
                    or s.summary
                    or (s.first_prompt or "")[:60]
                    or s.session_id[:8]
                )
                items.append(
                    {
                        "id": s.session_id,
                        "title": title,
                        "cwd": s.cwd,
                        "dir": Path(s.cwd).name if s.cwd else None,
                        "branch": s.git_branch,
                        "last_modified": str(s.last_modified),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"sessions": [], "error": str(exc)})
        return JSONResponse({"sessions": items})

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        return JSONResponse(
            {
                "project": str(ctx.last_project) if ctx.last_project else None,
                "model": ctx.args.model or "subscription default",
                "serena_connected": ctx.serena_connected,
            }
        )

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text(json.dumps(serena_status_payload()))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")

                sid = msg.get("session_id", "default")
                session = ctx.sessions.setdefault(sid, Session(sid=sid))
                session.ws = ws  # so ask_user can reach the current GUI socket

                if mtype == "ask_response":
                    qid = msg.get("id")
                    fut = session.pending_questions.get(qid)
                    if fut is not None and not fut.done():
                        fut.set_result(None if msg.get("skipped") else (msg.get("answers") or []))
                    continue

                if mtype == "set_project":
                    ok, message = await set_session_project(session, Path(msg.get("path", "")))
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "project",
                                "ok": ok,
                                "session_id": sid,
                                "project": str(session.project) if session.project else None,
                                "message": message,
                            }
                        )
                    )
                    continue

                if mtype == "resume_session":
                    if session.task and not session.task.done():
                        session.task.cancel()
                    await close_client(session)
                    cwd = msg.get("cwd")
                    note = ""
                    if cwd:
                        _, note = await set_session_project(session, Path(cwd))
                    session.resume_id = msg.get("resume_id")
                    session.cost = 0.0
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "project",
                                "ok": session.project is not None,
                                "session_id": sid,
                                "project": str(session.project) if session.project else None,
                                "message": "Resumed. " + note,
                            }
                        )
                    )
                    # Replay the conversation's past messages into the chat pane.
                    if session.resume_id and session.project:
                        items = build_history_items(session.resume_id, session.project)
                        await ws.send_text(
                            json.dumps({"type": "history", "session_id": sid, "items": items})
                        )
                    continue

                if mtype == "user_message":
                    if session.project is None:
                        session.project = ctx.last_project  # new chat inherits the default
                    if session.task and not session.task.done():
                        continue
                    text = msg.get("text", "")
                    images = msg.get("images") or None
                    session.task = asyncio.create_task(run_turn(ws, session, text, images))
                elif mtype == "interrupt":
                    if session.client is not None and session.task and not session.task.done():
                        session.interrupting = True
                        with contextlib.suppress(Exception):
                            await session.client.interrupt()
                elif mtype == "reset":
                    if session.task and not session.task.done():
                        session.task.cancel()
                    await close_client(session)
                    session.cost = 0.0
        except WebSocketDisconnect:
            for session in ctx.sessions.values():
                if session.task and not session.task.done():
                    session.task.cancel()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def _extract_serena_tools(status: Any) -> list[dict[str, str]]:
    """Best-effort parse of get_mcp_status() for Serena's tool names."""
    out: list[dict[str, str]] = []
    servers = getattr(status, "servers", None) or getattr(status, "mcp_servers", None) or []
    for srv in servers:
        name = getattr(srv, "name", None) or (srv.get("name") if isinstance(srv, dict) else None)
        if name != "serena":
            continue
        tools = getattr(srv, "tools", None) or (srv.get("tools") if isinstance(srv, dict) else None) or []
        for t in tools:
            tname = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else str(t))
            tdesc = getattr(t, "description", "") or (t.get("description", "") if isinstance(t, dict) else "")
            out.append({"name": str(tname), "description": str(tdesc)})
    return out


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Desktop coding agent (Claude Agent SDK + Serena).")
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=None,
        help="Optional initial project directory. If omitted, choose one in the GUI.",
    )
    parser.add_argument("--port", type=int, default=8765, help="Server port (default 8765).")
    parser.add_argument(
        "--model",
        default=None,
        help="Model id override (e.g. claude-opus-4-8). Default: subscription default.",
    )
    parser.add_argument(
        "--serena-context",
        default="ide",
        help="Serena context (default 'ide'; the old 'ide-assistant' was removed).",
    )
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser.")
    parser.add_argument(
        "--resume",
        default=None,
        help="Resume an existing Claude Code/SDK session id; the first chat continues that "
        "conversation with full context. Must use the same project the session ran in.",
    )
    parser.add_argument(
        "--resume-last",
        action="store_true",
        help="Resume the most recent session for the active project (continue where you left off).",
    )
    parser.add_argument(
        "--fork",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="When resuming, fork into a new session id so the original stays intact "
        "(default: --fork). Use --no-fork to continue the original in place.",
    )
    args = parser.parse_args()
    if args.project_dir is not None:
        args.project_dir = args.project_dir.expanduser().resolve()
    return args


def main() -> None:
    global ctx
    args = parse_args()
    if args.project_dir is not None and not args.project_dir.is_dir():
        sys.exit(f"ERROR: project dir does not exist: {args.project_dir}")

    ctx = Context(args=args, resume_id=args.resume)

    # asyncio subprocesses (Serena stdio + bash) need the Proactor loop on Windows.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    app = build_app()

    if not args.no_browser:
        url = f"http://localhost:{args.port}"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
