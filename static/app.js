// Frontend for the desktop coding agent.
// Single WebSocket to the backend; one in-memory conversation per session id.

"use strict";

marked.setOptions({ gfm: true, breaks: true });

// --- State ---------------------------------------------------------------

/** @type {Map<string, Session>} */
const sessions = new Map();
let currentId = null;
let runningId = null; // session whose turn is currently in flight
let ws = null;
let wsReady = false;
let defaultProject = null; // seeds new chats; each chat carries its own project
let pendingImages = []; // images attached to the next message: {media_type, data}

function currentProject() {
  const s = current();
  return s ? s.project || null : null;
}

/**
 * @typedef {Object} Session
 * @property {string} id
 * @property {string} title
 * @property {Array} items   // user/assistant items (source of truth)
 * @property {number} cost
 * @property {boolean} running
 */

const $ = (sel) => document.querySelector(sel);
const transcript = $("#transcript");

function newSessionId() {
  return (crypto.randomUUID && crypto.randomUUID()) || "s" + Date.now() + Math.random();
}

function createSession(makeCurrent = true) {
  const id = newSessionId();
  sessions.set(id, {
    id,
    title: "New chat",
    items: [],
    cost: 0,
    running: false,
    project: defaultProject,
    resumedFrom: null,
    pending: null,
  });
  if (makeCurrent) currentId = id;
  renderSidebar();
  if (makeCurrent) renderTranscript();
  return id;
}

function current() {
  return sessions.get(currentId);
}

function lastAssistant(sess) {
  const it = sess.items[sess.items.length - 1];
  return it && it.role === "assistant" ? it : null;
}

// --- Data mutations (called from WS events) ------------------------------

function addUserItem(sess, text, images) {
  sess.items.push({ role: "user", text, images: images && images.length ? images : null });
  const t = (text || (images && images.length ? "[image]" : "")).trim();
  if (sess.title === "New chat" && t) {
    sess.title = t.slice(0, 40);
    renderSidebar();
  }
}

function startAssistant(sess) {
  sess.items.push({ role: "assistant", thinking: "", parts: [] });
}

function appendText(sess, text) {
  const a = lastAssistant(sess);
  if (!a) return;
  const last = a.parts[a.parts.length - 1];
  if (last && last.kind === "text") last.text += text;
  else a.parts.push({ kind: "text", text });
}

function appendThinking(sess, text) {
  const a = lastAssistant(sess);
  if (a) a.thinking += text;
}

function addToolCall(sess, id, name) {
  // ask_user is shown as an interactive question card (see addAsk), not a raw
  // tool card; its args/result events are ignored on the frontend.
  if (name === "mcp__local__ask_user") return;
  const a = lastAssistant(sess);
  if (a)
    a.parts.push({
      kind: "tool",
      id,
      name,
      args: null,
      output: "",
      result: null,
      status: "running",
      open: false,
    });
}

function findTool(sess, id) {
  const a = lastAssistant(sess);
  if (!a) return null;
  for (const p of a.parts) if (p.kind === "tool" && p.id === id) return p;
  return null;
}

function addAsk(sess, id, questions) {
  const a = lastAssistant(sess);
  if (!a) return;
  a.parts.push({
    kind: "ask",
    id,
    questions: questions || [],
    draft: {}, // qIndex -> { selected: string[], other: string }
    answered: false,
    skipped: false,
    cancelled: false,
    summary: null, // array of {header, question, answer:[]}
  });
}

function findAsk(sess, id) {
  for (let i = sess.items.length - 1; i >= 0; i--) {
    const it = sess.items[i];
    if (it.role !== "assistant") continue;
    for (const p of it.parts) if (p.kind === "ask" && p.id === id) return p;
  }
  return null;
}

// --- Rendering -----------------------------------------------------------

let renderQueued = false;
function scheduleRender() {
  if (renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderTranscript();
  });
}

function nearBottom() {
  return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
}

function renderTranscript() {
  const sess = current();
  const stick = nearBottom();
  transcript.innerHTML = "";

  if (!sess || sess.items.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Send a message to start. Tools run with no confirmation prompts.";
    transcript.appendChild(empty);
    return;
  }

  for (const item of sess.items) {
    if (item.role === "user") {
      transcript.appendChild(renderUser(item));
    } else if (item.role === "note") {
      transcript.appendChild(renderNote(item));
    } else {
      transcript.appendChild(renderAssistant(item));
    }
  }

  if (stick) transcript.scrollTop = transcript.scrollHeight;
}

function renderUser(item) {
  const wrap = document.createElement("div");
  wrap.className = "msg user";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (item.text) {
    const tx = document.createElement("div");
    tx.textContent = item.text;
    bubble.appendChild(tx);
  }
  if (item.images && item.images.length) {
    const box = document.createElement("div");
    box.className = "imgs";
    for (const im of item.images) {
      const img = document.createElement("img");
      img.src = `data:${im.media_type};base64,${im.data}`;
      box.appendChild(img);
    }
    bubble.appendChild(box);
  }
  wrap.appendChild(bubble);
  return wrap;
}

function renderNote(item) {
  const wrap = document.createElement("div");
  wrap.className = "msg note";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = item.text;
  wrap.appendChild(bubble);
  return wrap;
}

function renderAssistant(item) {
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  const bubble = document.createElement("div");
  bubble.className = "bubble";

  if (item.thinking && item.thinking.trim()) {
    const th = document.createElement("div");
    th.className = "thinking";
    th.textContent = item.thinking;
    bubble.appendChild(th);
  }

  for (const part of item.parts) {
    if (part.kind === "text") {
      if (!part.text) continue;
      const md = document.createElement("div");
      md.className = "md";
      md.innerHTML = marked.parse(part.text);
      md.querySelectorAll("pre code").forEach((el) => {
        try {
          hljs.highlightElement(el);
        } catch (_) {}
      });
      bubble.appendChild(md);
    } else if (part.kind === "tool") {
      bubble.appendChild(renderToolCard(part));
    } else if (part.kind === "ask") {
      bubble.appendChild(renderAskCard(part));
    }
  }

  if (bubble.childElementCount === 0) {
    const ph = document.createElement("div");
    ph.className = "thinking";
    ph.textContent = "…";
    bubble.appendChild(ph);
  }

  wrap.appendChild(bubble);
  return wrap;
}

function argsSummary(args) {
  if (args == null) return "";
  try {
    const s = JSON.stringify(args);
    return s.length > 90 ? s.slice(0, 90) + "…" : s;
  } catch (_) {
    return "";
  }
}

function renderToolCard(part) {
  const card = document.createElement("div");
  card.className = "tool-card " + part.status + (part.open ? " open" : "");

  const head = document.createElement("div");
  head.className = "tool-head";
  head.innerHTML =
    `<span class="tool-caret">▸</span>` +
    `<span>🔧</span>` +
    `<span class="tool-name"></span>` +
    `<span class="tool-args-inline"></span>` +
    `<span class="tool-status"></span>`;
  head.querySelector(".tool-name").textContent = part.name;
  head.querySelector(".tool-args-inline").textContent = argsSummary(part.args);
  head.querySelector(".tool-status").textContent =
    part.status === "running" ? "running…" : part.status === "error" ? "error" : "done";
  head.addEventListener("click", () => {
    part.open = !part.open;
    scheduleRender();
  });
  card.appendChild(head);

  const detail = document.createElement("div");
  detail.className = "tool-detail";
  if (part.args != null) {
    detail.appendChild(labeled("arguments", JSON.stringify(part.args, null, 2)));
  }
  const out = (part.output || "") + (part.result != null ? part.result : "");
  if (out.trim()) {
    detail.appendChild(labeled("result", part.result != null ? part.result : part.output));
  } else if (part.status === "running" && part.output) {
    detail.appendChild(labeled("output (streaming)", part.output));
  }
  card.appendChild(detail);
  return card;
}

// --- Ask-the-user question card ------------------------------------------

function renderAskCard(part) {
  const card = document.createElement("div");
  card.className = "ask-card" + (part.answered ? " answered" : "");

  if (part.answered) {
    // Compact read-only summary after the user has responded.
    const head = document.createElement("div");
    head.className = "ask-head";
    head.textContent = part.cancelled
      ? "Question dismissed"
      : part.skipped
        ? "You skipped these questions"
        : "Your answer";
    card.appendChild(head);
    if (!part.cancelled && !part.skipped && part.summary) {
      for (const a of part.summary) {
        const row = document.createElement("div");
        row.className = "ask-summary-row";
        const q = document.createElement("div");
        q.className = "ask-summary-q";
        q.textContent = a.header || a.question || "";
        const ans = document.createElement("div");
        ans.className = "ask-summary-a";
        ans.textContent = (a.answer && a.answer.length ? a.answer.join(", ") : "(no selection)");
        row.appendChild(q);
        row.appendChild(ans);
        card.appendChild(row);
      }
    }
    return card;
  }

  const title = document.createElement("div");
  title.className = "ask-head";
  title.textContent = "Claude is asking you:";
  card.appendChild(title);

  part.questions.forEach((q, qi) => {
    const draft = part.draft[qi] || (part.draft[qi] = { selected: [], other: "" });
    const block = document.createElement("div");
    block.className = "ask-q";

    if (q.header) {
      const chip = document.createElement("span");
      chip.className = "ask-chip";
      chip.textContent = q.header;
      block.appendChild(chip);
    }
    const qtext = document.createElement("div");
    qtext.className = "ask-qtext";
    qtext.textContent = q.question || "";
    block.appendChild(qtext);

    const multi = !!q.multiSelect;
    const inputType = multi ? "checkbox" : "radio";
    const groupName = `ask-${part.id}-${qi}`;

    const makeOption = (value, labelText, descText, isOther) => {
      const opt = document.createElement("label");
      opt.className = "ask-opt";
      const inp = document.createElement("input");
      inp.type = inputType;
      inp.name = groupName;
      inp.value = value;
      inp.checked = draft.selected.includes(value);
      inp.addEventListener("change", () => {
        if (multi) {
          if (inp.checked) {
            if (!draft.selected.includes(value)) draft.selected.push(value);
          } else {
            draft.selected = draft.selected.filter((v) => v !== value);
          }
        } else {
          draft.selected = inp.checked ? [value] : [];
        }
      });
      const txt = document.createElement("div");
      txt.className = "ask-opt-text";
      const lab = document.createElement("div");
      lab.className = "ask-opt-label";
      lab.textContent = labelText;
      txt.appendChild(lab);
      if (descText) {
        const d = document.createElement("div");
        d.className = "ask-opt-desc";
        d.textContent = descText;
        txt.appendChild(d);
      }
      opt.appendChild(inp);
      opt.appendChild(txt);
      if (isOther) {
        const other = document.createElement("input");
        other.type = "text";
        other.className = "ask-other-input";
        other.placeholder = "type your own answer…";
        other.value = draft.other || "";
        other.addEventListener("input", () => {
          draft.other = other.value;
        });
        // Selecting the text field implies the Other option is chosen.
        other.addEventListener("focus", () => {
          inp.checked = true;
          inp.dispatchEvent(new Event("change"));
        });
        txt.appendChild(other);
      }
      return opt;
    };

    (q.options || []).forEach((o) => {
      block.appendChild(makeOption(o.label, o.label, o.description, false));
    });
    block.appendChild(makeOption("__other__", "Other", "", true));

    card.appendChild(block);
  });

  const actions = document.createElement("div");
  actions.className = "ask-actions";
  const submit = document.createElement("button");
  submit.className = "ask-submit";
  submit.textContent = "Submit";
  submit.addEventListener("click", () => answerAsk(part, false));
  const skip = document.createElement("button");
  skip.className = "ask-skip";
  skip.textContent = "Skip";
  skip.addEventListener("click", () => answerAsk(part, true));
  actions.appendChild(submit);
  actions.appendChild(skip);
  card.appendChild(actions);

  return card;
}

function answerAsk(part, skipped) {
  if (part.answered || !wsReady) return;
  const sess = current();
  if (!sess) return;

  let answers = null;
  if (!skipped) {
    answers = part.questions.map((q, qi) => {
      const d = part.draft[qi] || { selected: [], other: "" };
      const labels = d.selected.filter((v) => v !== "__other__");
      if (d.selected.includes("__other__") && (d.other || "").trim()) {
        labels.push(d.other.trim());
      }
      return { header: q.header, question: q.question, answer: labels };
    });
  }

  ws.send(
    JSON.stringify({
      type: "ask_response",
      session_id: sess.id,
      id: part.id,
      answers: answers || [],
      skipped: !!skipped,
    })
  );

  part.answered = true;
  part.skipped = !!skipped;
  part.summary = answers;
  scheduleRender();
}

function labeled(label, text) {
  const frag = document.createDocumentFragment();
  const l = document.createElement("div");
  l.className = "label";
  l.textContent = label;
  const pre = document.createElement("pre");
  pre.textContent = text;
  frag.appendChild(l);
  frag.appendChild(pre);
  return frag;
}

function renderSidebar() {
  const list = $("#session-list");
  list.innerHTML = "";
  for (const sess of sessions.values()) {
    const li = document.createElement("li");
    li.textContent = sess.title || "New chat";
    if (sess.id === currentId) li.classList.add("active");
    li.addEventListener("click", () => {
      currentId = sess.id;
      renderSidebar();
      renderTranscript();
      updateRunIndicator();
    });
    list.appendChild(li);
  }
}

// --- Run indicator / cost ------------------------------------------------

function updateRunIndicator() {
  const sess = current();
  const running = sess && sess.running;
  const proj = currentProject();
  $("#run-indicator").classList.toggle("hidden", !running);
  // Send stays enabled while running so you can steer (interrupt + redirect).
  $("#send").disabled = !proj;
  const input = $("#input");
  input.disabled = !proj;
  input.placeholder = proj
    ? "Ask the agent to do something…  (Enter to send, Shift+Enter for newline)"
    : "Select a project for this chat (top bar) to start.";
  $("#cost").textContent = "$" + (sess ? sess.cost : 0).toFixed(4);
  setChip();
}

// --- WebSocket -----------------------------------------------------------

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => {
    wsReady = true;
  };
  ws.onclose = () => {
    wsReady = false;
    setSerena("failed", "disconnected");
    setTimeout(connect, 2000);
  };
  ws.onmessage = (ev) => {
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (_) {
      return;
    }
    handle(msg);
  };
}

function setSerena(state, label) {
  const dot = $("#serena-dot");
  dot.className = "dot " + (state === "connected" ? "dot-ok" : state === "failed" ? "dot-bad" : "dot-unknown");
  $("#serena-label").textContent = label || "serena";
}

function setChip() {
  const p = currentProject();
  const btn = $("#project-dir");
  btn.textContent = p || "no project — click to choose";
  btn.title = p ? p + "  (click to change this chat's project)" : "click to choose a project";
}

function openProjectModal() {
  const input = $("#project-input");
  input.value = currentProject() || defaultProject || "";
  $("#project-msg").textContent = "";
  $("#project-modal").classList.remove("hidden");
  input.focus();
}

function submitProject() {
  const path = $("#project-input").value.trim();
  if (!path || !wsReady) return;
  let sess = current();
  if (!sess) {
    createSession();
    sess = current();
  }
  $("#project-msg").textContent = "Activating… (first project can take a moment)";
  ws.send(JSON.stringify({ type: "set_project", session_id: sess.id, path }));
}

function handle(msg) {
  // Status is global, not tied to the running session.
  if (msg.type === "status") {
    $("#model-name").textContent = msg.model;
    setSerena(msg.serena, msg.serena === "connected" ? `serena · ${msg.tool_count} tools` : "serena failed");
    if (msg.project) {
      defaultProject = msg.project;
      const s = current();
      if (s && !s.project) s.project = msg.project; // seed the first chat
    }
    updateRunIndicator();
    if (!msg.project && !currentProject()) openProjectModal();
    return;
  }
  if (msg.type === "project") {
    const msgEl = $("#project-msg");
    const target = (msg.session_id && sessions.get(msg.session_id)) || current();
    if (msg.ok) {
      if (target) target.project = msg.project;
      if (msg.project) defaultProject = msg.project;
      msgEl.textContent = msg.message || "";
      $("#project-modal").classList.add("hidden");
      updateRunIndicator();
    } else {
      msgEl.textContent = msg.message || "Failed to set project.";
    }
    return;
  }
  if (msg.type === "history") {
    const s = sessions.get(msg.session_id);
    if (s) {
      s.items = msg.items || [];
      if (s.id === currentId) renderTranscript();
    }
    return;
  }

  // All turn events apply to the session that is currently running.
  const sess = sessions.get(runningId);
  if (!sess) return;

  switch (msg.type) {
    case "assistant_message_start":
      startAssistant(sess);
      break;
    case "assistant_text_delta":
      appendText(sess, msg.text);
      break;
    case "thinking_delta":
      appendThinking(sess, msg.text);
      break;
    case "tool_call":
      addToolCall(sess, msg.id, msg.name);
      break;
    case "tool_args": {
      const t = findTool(sess, msg.id);
      if (t) t.args = msg.args;
      break;
    }
    case "tool_output_delta": {
      const t = findTool(sess, msg.id);
      if (t) t.output += msg.text;
      break;
    }
    case "tool_result": {
      const t = findTool(sess, msg.id);
      if (t) {
        t.status = msg.status;
        t.result = msg.result;
      }
      break;
    }
    case "ask_question":
      addAsk(sess, msg.id, msg.questions);
      break;
    case "ask_cancel": {
      const p = findAsk(sess, msg.id);
      if (p && !p.answered) {
        p.answered = true;
        p.cancelled = true;
      }
      break;
    }
    case "turn_complete":
      sess.running = false;
      sess.cost = msg.cost || 0;
      runningId = null;
      loadRecents(); // a new on-disk session may have appeared
      flushPending(sess); // steering: send the queued message, if any
      break;
    case "interrupted": {
      sess.running = false;
      appendText(sess, "\n\n_(interrupted)_");
      runningId = null;
      flushPending(sess);
      break;
    }
    case "error": {
      sess.running = false;
      const a = lastAssistant(sess) || (startAssistant(sess), lastAssistant(sess));
      appendText(sess, `\n\n**⚠ Error:** ${msg.message}`);
      runningId = null;
      flushPending(sess);
      break;
    }
  }

  if (sess.id === currentId) {
    scheduleRender();
    updateRunIndicator();
  } else {
    renderSidebar();
  }
}

// --- Sending -------------------------------------------------------------

function send() {
  const ta = $("#input");
  const text = ta.value.trim();
  const imgs = pendingImages.slice();
  if ((!text && imgs.length === 0) || !wsReady) return;
  if (!currentProject()) {
    openProjectModal();
    return;
  }
  let sess = current();
  if (!sess) {
    createSession();
    sess = current();
  }

  if (sess.running) {
    // Steer: interrupt the current turn, then send this once it stops.
    sess.pending = { text, images: imgs };
    ws.send(JSON.stringify({ type: "interrupt", session_id: sess.id }));
  } else {
    dispatchUserMessage(sess, text, imgs);
  }
  ta.value = "";
  clearAttachments();
  autosize();
}

function dispatchUserMessage(sess, text, images) {
  addUserItem(sess, text, images);
  sess.running = true;
  runningId = sess.id;
  ws.send(JSON.stringify({ type: "user_message", session_id: sess.id, text, images }));
  renderTranscript();
  updateRunIndicator();
}

function flushPending(sess) {
  if (sess && sess.pending) {
    const p = sess.pending;
    sess.pending = null;
    dispatchUserMessage(sess, p.text, p.images);
  }
}

function interrupt() {
  const sess = current();
  if (sess && sess.running && wsReady) {
    ws.send(JSON.stringify({ type: "interrupt", session_id: sess.id }));
  }
}

function resetChat() {
  const sess = current();
  if (!sess) return;
  if (sess.running && wsReady) ws.send(JSON.stringify({ type: "interrupt", session_id: sess.id }));
  if (wsReady) ws.send(JSON.stringify({ type: "reset", session_id: sess.id }));
  sess.items = [];
  sess.cost = 0;
  sess.running = false;
  if (runningId === sess.id) runningId = null;
  renderTranscript();
  updateRunIndicator();
}

// --- Tools modal ---------------------------------------------------------

async function showTools() {
  const body = $("#tools-body");
  body.innerHTML = "<div class='muted'>Loading…</div>";
  $("#tools-modal").classList.remove("hidden");
  try {
    const res = await fetch("/api/tools");
    const data = await res.json();
    body.innerHTML = "";
    body.appendChild(toolGroup("Native tools", data.native));
    const serenaTitle = data.serena_connected
      ? `Serena tools (${data.serena.length})`
      : "Serena tools — NOT CONNECTED";
    body.appendChild(toolGroup(serenaTitle, data.serena));
  } catch (e) {
    body.innerHTML = `<div class='muted'>Failed to load tools: ${e}</div>`;
  }
}

function toolGroup(title, tools) {
  const frag = document.createDocumentFragment();
  const h = document.createElement("div");
  h.className = "tool-group-title";
  h.textContent = title;
  frag.appendChild(h);
  if (!tools || tools.length === 0) {
    const e = document.createElement("div");
    e.className = "muted";
    e.textContent = "(none)";
    frag.appendChild(e);
    return frag;
  }
  for (const t of tools) {
    const row = document.createElement("div");
    row.className = "tool-row";
    row.innerHTML = `<div class="n"></div><div class="d"></div>`;
    row.querySelector(".n").textContent = t.name;
    row.querySelector(".d").textContent = t.description || "";
    frag.appendChild(row);
  }
  return frag;
}

// --- Recents (past Claude conversations) ---------------------------------

async function loadRecents() {
  const list = $("#recents-list");
  const prevScroll = list.scrollTop;
  try {
    const res = await fetch("/api/sessions");
    const data = await res.json();
    const items = data.sessions || [];
    list.innerHTML = "";
    if (items.length === 0) {
      list.innerHTML = '<li class="recents-empty muted">no past sessions</li>';
      return;
    }
    for (const item of items) {
      const li = document.createElement("li");
      li.title = (item.cwd || "") + (item.branch ? `  (${item.branch})` : "");
      const t = document.createElement("div");
      t.className = "recent-title";
      t.textContent = item.title;
      li.appendChild(t);
      if (item.dir) {
        const sub = document.createElement("div");
        sub.className = "recent-sub";
        sub.textContent = item.dir + (item.branch ? ` · ${item.branch}` : "");
        li.appendChild(sub);
      }
      li.addEventListener("click", () => resumeRecent(item));
      list.appendChild(li);
    }
    list.scrollTop = prevScroll;
  } catch (e) {
    list.innerHTML = `<li class="recents-empty muted">failed to load: ${e}</li>`;
  }
}

function resumeRecent(item) {
  if (!wsReady) return;
  // Dedup: if this conversation is already open as a chat, just switch to it.
  for (const s of sessions.values()) {
    if (s.resumedFrom === item.id) {
      currentId = s.id;
      renderSidebar();
      renderTranscript();
      updateRunIndicator();
      return;
    }
  }
  const id = createSession();
  const sess = sessions.get(id);
  sess.title = item.title;
  sess.resumedFrom = item.id;
  if (item.cwd) sess.project = item.cwd;
  sess.items.push({ role: "note", text: `Resuming "${item.title}"…` });
  renderSidebar();
  renderTranscript();
  updateRunIndicator();
  ws.send(JSON.stringify({ type: "resume_session", session_id: id, resume_id: item.id, cwd: item.cwd }));
}

// --- Image attachments ---------------------------------------------------

function addImageFile(file) {
  if (!file || !file.type || !file.type.startsWith("image/")) return;
  const reader = new FileReader();
  reader.onload = () => {
    const url = String(reader.result || "");
    const comma = url.indexOf(",");
    if (comma < 0) return;
    pendingImages.push({ media_type: file.type, data: url.slice(comma + 1) });
    renderAttachments();
  };
  reader.readAsDataURL(file);
}

function onPaste(e) {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  let found = false;
  for (const it of items) {
    if (it.kind === "file" && it.type.startsWith("image/")) {
      addImageFile(it.getAsFile());
      found = true;
    }
  }
  if (found) e.preventDefault();
}

function onDrop(e) {
  e.preventDefault();
  $("#composer").classList.remove("dragover");
  const files = (e.dataTransfer && e.dataTransfer.files) || [];
  for (const f of files) addImageFile(f);
}

function renderAttachments() {
  const box = $("#attachments");
  box.innerHTML = "";
  box.classList.toggle("hidden", pendingImages.length === 0);
  pendingImages.forEach((im, i) => {
    const thumb = document.createElement("div");
    thumb.className = "thumb";
    const img = document.createElement("img");
    img.src = `data:${im.media_type};base64,${im.data}`;
    const x = document.createElement("button");
    x.textContent = "✕";
    x.addEventListener("click", () => {
      pendingImages.splice(i, 1);
      renderAttachments();
    });
    thumb.appendChild(img);
    thumb.appendChild(x);
    box.appendChild(thumb);
  });
}

function clearAttachments() {
  pendingImages = [];
  renderAttachments();
}

// --- Input box -----------------------------------------------------------

function autosize() {
  const ta = $("#input");
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 200) + "px";
}

// --- Wire up -------------------------------------------------------------

function init() {
  $("#send").addEventListener("click", send);
  $("#interrupt").addEventListener("click", interrupt);
  $("#new-chat").addEventListener("click", () => {
    createSession();
    updateRunIndicator();
    $("#input").focus();
  });
  $("#reset-chat").addEventListener("click", resetChat);
  $("#show-tools").addEventListener("click", showTools);
  $("#close-tools").addEventListener("click", () => $("#tools-modal").classList.add("hidden"));
  $("#refresh-recents").addEventListener("click", loadRecents);

  $("#project-dir").addEventListener("click", openProjectModal);
  $("#project-submit").addEventListener("click", submitProject);
  $("#close-project").addEventListener("click", () => $("#project-modal").classList.add("hidden"));
  $("#project-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submitProject();
    }
  });
  $("#tools-modal").addEventListener("click", (e) => {
    if (e.target.id === "tools-modal") $("#tools-modal").classList.add("hidden");
  });

  const ta = $("#input");
  ta.addEventListener("input", autosize);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  });
  ta.addEventListener("paste", onPaste);
  const composer = $("#composer");
  composer.addEventListener("dragover", (e) => {
    e.preventDefault();
    composer.classList.add("dragover");
  });
  composer.addEventListener("dragleave", () => composer.classList.remove("dragover"));
  composer.addEventListener("drop", onDrop);

  createSession();
  updateRunIndicator();
  connect();
  loadRecents();
  // Live-refresh Recents: poll while the tab is visible, and on refocus.
  setInterval(() => {
    if (!document.hidden) loadRecents();
  }, 15000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) loadRecents();
  });
  ta.focus();
}

init();
