# Coding Agent (web GUI)

A personal, single-user coding agent with a browser-based GUI. It runs on the
**Claude Agent SDK** using your existing **Claude Code subscription login —
no API key, no separate billing**. Code operations go through
[Serena](https://github.com/oraios/serena) (a local MCP server); on top of that
the agent has Claude Code's built-in tools, plus a `git_checkpoint` tool. Tools
run with **no confirmation prompts** — the only gate is a bash denylist.

Same idea as Claude Desktop's Code tab, except *you* own the permission layer
(and it's reduced to a denylist).

> ⚠️ This intentionally edits files and runs commands with no confirmation.
> Point it at repos you're willing to let an agent change, and keep them under
> git. Localhost-only, no auth.

---

## Why the Agent SDK (and not the raw API)

The first cut of this app called the raw Anthropic Messages API directly. That
needs a **separate, billed API key**, and the API only accepts *remote* MCP
servers (Serena is local/stdio). Since this machine has a **Claude
subscription** and no API key, the app was rebuilt on the **Claude Agent SDK**,
which:

- authenticates with the `claude` CLI you're already logged into (subscription),
- runs the agent loop and manages **local stdio MCP servers** (Serena) natively,
- and exposes a **permission callback** (`can_use_tool`) so we implement the
  "no prompts except a bash denylist" policy directly.

```
Browser (WebSocket)
   │
   ▼
FastAPI ── per chat session ──► ClaudeSDKClient  (subscription auth)
                                     │
                                     ├─ Serena   (stdio MCP: mcp__serena__*)
                                     ├─ git_checkpoint (in-process SDK tool)
                                     ├─ Bash / Read / Edit / Grep / …  (built-in)
                                     └─ can_use_tool → allow all, deny denylisted bash
   ▲
   └── streamed text + tool-call cards + cost back to the browser
```

---

## Prerequisites

- **Python 3.11+** (developed on 3.13).
- The **`claude` CLI installed and logged in** with a subscription
  (`claude` → `/login`). The app uses that session; check `claude` works first.
- **`uvx`** (from [uv](https://github.com/astral-sh/uv)) on `PATH` — Serena is
  launched via `uvx --from git+…`. First run builds it (slow once, cached after).
- **No `ANTHROPIC_API_KEY` needed.** If one *is* set in your environment, the
  SDK will prefer it (and bill that API account) — unset it to use the
  subscription.

---

## Install

```bash
pip install -r requirements.txt
```

---

## Run

**Easiest:** double-click **`run.bat`**. It opens a console window, starts the
server (your browser opens automatically), and stays open so you can read any
errors. Press **Ctrl+C** in that window to stop it. You can right-click →
*Create shortcut* and put it on your desktop / pin it.

Or from a terminal:

```bash
python app.py
```

Binds to `127.0.0.1` only, prints a startup banner, and opens
`http://localhost:8765`. **Choose the project in the GUI** — click the project
chip in the top bar (or just try to send a message) and enter an absolute path.
You can switch projects from the same chip. **After the first time, the app
remembers your last project** (stored in `.last_project`) and auto-loads it on
the next launch — so you only pick it once. Each chat session launches its own
Serena against the active project on its first message.

Optionally pre-select a project at launch: `python app.py --project-dir C:\repo`.

**CLI args**

| Flag | Default | Notes |
|---|---|---|
| `--project-dir` | *(none)* | Optional initial project. Otherwise pick one in the GUI. |
| `--port` | `8765` | |
| `--model` | *(subscription default)* | Override with e.g. `claude-opus-4-8` if your plan has it. |
| `--serena-context` | `ide` | Serena context. **`ide-assistant` was removed** — `ide` replaces it. |
| `--no-browser` | off | Don't auto-open the browser. |

`Ctrl+C` shuts the server down; each session's `ClaudeSDKClient` (and its
Serena subprocess) is disconnected on shutdown.

### Continuing a previous conversation

The app shares Claude Code's on-disk session store, so it can **resume an
existing conversation** with full context — including sessions started in
Claude Code itself.

- **`continue.bat`** (double-click) resumes the **most recent** session for the
  remembered project.
- Or: `python app.py --resume-last`, or `python app.py --resume <session-id>`.
- `--fork` (default) continues a *copy*, leaving the original session intact;
  `--no-fork` continues the original in place.

When you open a conversation, its **past messages are replayed** into the chat
pane (reconstructed from the saved transcript), and clicking the same recent
again just switches to the already-open chat instead of duplicating it. Caveat:
the **first message reloads the whole prior conversation** as context (slower +
more cost, may compact), and you must be on the **same project** the session ran
in.

### Serena launch command (verified against the installed CLI)

```
uvx --from git+https://github.com/oraios/serena serena start-mcp-server \
    --context ide --project <dir> --transport stdio \
    --enable-web-dashboard False --enable-gui-log-window False
```

The executable is `serena start-mcp-server` (not `serena-mcp-server`), and the
context is `ide` (not the removed `ide-assistant`).

---

## Using it

- **Top bar:** the **project for the current chat** (click to change it), model,
  running cost estimate, Serena status dot. Each chat has its own project, so
  the chip follows whichever chat you're viewing; new chats inherit your last
  project as a default.
- **Sidebar:** New chat / Reset / Tools (lists the local + Serena tools), the
  current in-app **Chats**, and a **Recents** list of *all* your past Claude
  Code / SDK conversations (same list as Claude Code's "Recents"). Click any
  recent to switch to its project and **resume that conversation** with full
  context. (Sessions with no recorded directory will resume against the current
  project.)
- **Main pane:** transcript. Tool calls render as collapsible cards inline —
  blue while running, green on success, red on error; expand for full args +
  result.
- **Composer:** Enter sends, Shift+Enter newline. **Paste or drag-drop an
  image** to attach it. While a turn runs you get a "running…" indicator and an
  **Interrupt** button — and you can **steer**: just send a new message and it
  interrupts the current turn and redirects.

### Tools available to the agent

- **Serena** (`mcp__serena__*`) — semantic code ops (preferred for code).
- **`git_checkpoint`** — `git add -A && git commit -m <msg> --allow-empty`.
- **`Bash`** — built-in shell. Denylisted destructive commands
  (`rm -rf /`, `mkfs`, `dd if=`, fork bombs, …) are refused; everything else
  runs with no prompt.
- **Claude Code's other built-ins** (Read, Edit, Write, Grep, Glob, …) are also
  available; the system prompt prefers Serena for code work. (To lock the agent
  down to Serena + bash + git_checkpoint only, add the built-ins to
  `disallowed_tools` in `build_options()`.)

Sessions are **in-memory only** — they die with the server (SQLite is a
deliberate "later").

---

## Smoke test — run and passing ✅

Verified end-to-end on this machine (read-only, no files modified):

1. Started the server, connected over WebSocket, selected this repo as the
   project.
2. Asked: *"Using Serena, find the symbol `run_turn` in app.py; report its
   location and how many symbols reference it. Do not modify any files."*
3. The agent used `mcp__serena__find_symbol` and
   `mcp__serena__find_referencing_symbols`, streamed its text and tool-call
   events, and correctly answered: `run_turn` at `app.py:296–389`, **1**
   reference (in `ws_endpoint`). Cost ≈ `$0.28`, drawn from the subscription.
   No API key was set.

`smoke_ws.py` is the script (drives the WebSocket directly).

<!-- TODO: add a browser screenshot of a running session with tool-call cards. -->

---

## Gaps vs Claude Desktop

- **No persistence.** Sessions are in-memory; restart loses everything.
- **No permission prompts** by design — only the bash denylist gates anything.
  `Bash` is *not* sandboxed and can act outside the project dir.
- **One Serena subprocess per chat session.** Many simultaneous sessions = many
  `claude` + Serena + language-server processes. Use few sessions / Reset to
  keep resource use down.
- **Cost is an estimate** (the SDK's subscription-equivalent figure), not a bill.
- **Markdown is rendered without HTML sanitization** — fine for a local,
  single-user tool; don't expose the port.
- **No file/image attachments, no diff/approval/undo, no model-switcher UI.**
- **No auth / multi-user / remote access** — binds to `127.0.0.1` only.
- **`ANTHROPIC_API_KEY` takes precedence** if set, which would bill an API
  account instead of the subscription — unset it for subscription use.
