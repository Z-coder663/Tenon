"use strict";

const token = document.querySelector('meta[name="rivet-token"]').content;
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const ui = {
  shell: $("#appShell"),
  sidebar: $("#sidebar"),
  inspector: $("#inspector"),
  backdrop: $("#mobileBackdrop"),
  conversation: $("#conversation"),
  empty: $("#emptyState"),
  messages: $("#messageList"),
  form: $("#composerForm"),
  input: $("#messageInput"),
  send: $("#sendButton"),
  newSession: $("#newSessionButton"),
  sessions: $("#sessionList"),
  sessionCount: $("#sessionCount"),
  sessionSearch: $("#sessionSearchInput"),
  workspaceName: $("#workspaceName"),
  workspacePath: $("#workspacePath"),
  modelName: $("#modelName"),
  modelProtocol: $("#modelProtocol"),
  connectionDot: $("#connectionDot"),
  pageTitle: $("#pageTitle"),
  pageMeta: $("#pageMeta"),
  runStatus: $("#runStatus"),
  runStatusText: $("#runStatusText"),
  approvalModeControl: $("#approvalModeControl"),
  approvalModeTrigger: $("#approvalModeTrigger"),
  approvalModeLabel: $("#approvalModeLabel"),
  approvalModeMenu: $("#approvalModeMenu"),
  compactButton: $("#compactContextButton"),
  compactUsage: $("#compactContextUsage"),
  compactModal: $("#compactContextModal"),
  compactUsageValue: $("#compactUsageValue"),
  compactUsageBar: $("#compactUsageBar"),
  compactUsageDetail: $("#compactUsageDetail"),
  confirmCompact: $("#confirmCompactButton"),
  turnSummary: $("#turnSummary"),
  planEmpty: $("#planEmpty"),
  planList: $("#planList"),
  inspectedCount: $("#inspectedCount"),
  changedCount: $("#changedCount"),
  projectFiles: $("#projectFileList"),
  fileSearch: $("#fileSearchInput"),
  fileListNote: $("#fileListNote"),
  verificationCard: $("#verificationCard"),
  verificationTitle: $("#verificationTitle"),
  verificationDetail: $("#verificationDetail"),
  activity: $("#activityList"),
  subagentSection: $("#subagentSection"),
  subagentCount: $("#subagentCount"),
  subagentList: $("#subagentList"),
  skillSection: $("#skillSection"),
  skillCount: $("#skillCount"),
  skillList: $("#skillList"),
  approvalModal: $("#approvalModal"),
  approvalTool: $("#approvalTool"),
  approvalSummary: $("#approvalSummary"),
  approve: $("#approveButton"),
  reject: $("#rejectApprovalButton"),
  stopApprovalTask: $("#stopApprovalTaskButton"),
  diffModal: $("#diffModal"),
  diffContent: $("#diffContent"),
  diffFileCount: $("#diffFileCount"),
  diffFileList: $("#diffFileList"),
  diffActivePath: $("#diffActivePath"),
  diffSummary: $("#diffSummary"),
  fileModal: $("#fileModal"),
  filePreviewTitle: $("#filePreviewTitle"),
  filePreviewMeta: $("#filePreviewMeta"),
  filePreviewContent: $("#filePreviewContent"),
  fileDiff: $("#fileDiffButton"),
  commandList: $("#commandList"),
  commandCount: $("#commandCount"),
  commandSummary: $("#commandSummary"),
  renameModal: $("#renameModal"),
  renameInput: $("#renameSessionInput"),
  confirmModal: $("#confirmModal"),
  confirmTitle: $("#confirmTitle"),
  confirmMessage: $("#confirmMessage"),
  toasts: $("#toastRegion"),
};

let currentSnapshot = null;
let busy = false;
let cancelling = false;
let activeAssistant = null;
let lastAssistantText = "";
let pendingApproval = null;
let activityStarted = false;
let thinkingIndicator = null;
const runningTools = [];
let currentDiffFiles = [];
let activeDiffIndex = -1;
let currentDiffTruncated = false;
let followLatestMessage = true;
const subagentState = new Map();
const skillState = new Map();
let allSessions = [];
let workspaceFiles = [];
let changedFileSet = new Set();
let hiddenWorkspaceFiles = 0;
let workspaceFilesTruncated = false;
const collapsedFolders = new Set();
const workspaceTreeCollator = new Intl.Collator("zh-CN", { numeric: true, sensitivity: "base" });
let activePreviewPath = null;
let pendingRenameSession = null;
let pendingConfirmAction = null;
let liveTurnNumber = 0;
let activeRecovery = null;
let compacting = false;
let approvalChanging = false;
const STREAM_RECORD_BATCH_SIZE = 64;

function productMessage(message) {
  return String(message ?? "").replace(/\bRivet\b/g, "CAworker");
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Rivet-Token", token);
  if (options.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try {
      const payload = await response.json();
      message = payload.error || message;
    } catch (_) {
      // Keep the stable status message.
    }
    throw new Error(productMessage(message));
  }
  return response;
}

async function jsonRequest(path, options = {}) {
  const response = await request(path, options);
  return response.json();
}

function setBusy(value, label = "就绪") {
  busy = value;
  if (!value) cancelling = false;
  ui.send.disabled = value && cancelling;
  ui.send.classList.toggle("stop", value);
  ui.send.setAttribute("aria-label", value ? "停止当前任务" : "发送消息");
  ui.newSession.disabled = value;
  ui.approvalModeTrigger.disabled = value || approvalChanging;
  if (value) setApprovalMenu(false);
  ui.compactButton.disabled = value || !contextCanCompact();
  $$(".recovery-actions button").forEach((button) => { button.disabled = value || button.dataset.blocked === "true"; });
  ui.input.disabled = value;
  ui.runStatus.className = `run-status ${value ? "working" : "idle"}`;
  ui.runStatusText.textContent = value ? label : "就绪";
  if (!value) {
    clearThinking();
    ui.input.disabled = false;
    ui.input.focus();
  }
}

function setRunError(message) {
  busy = false;
  cancelling = false;
  ui.send.disabled = false;
  ui.send.classList.remove("stop");
  ui.send.setAttribute("aria-label", "发送消息");
  ui.newSession.disabled = false;
  ui.approvalModeTrigger.disabled = approvalChanging;
  ui.compactButton.disabled = !contextCanCompact();
  ui.input.disabled = false;
  ui.runStatus.className = "run-status error";
  ui.runStatusText.textContent = "已停止";
  clearThinking();
  toast(productMessage(message), "error");
}

function contextCanCompact() {
  const status = currentSnapshot && currentSnapshot.status || {};
  return Number(status.turns || 0) > 0 && Number(status.messages || 0) > 3;
}

function approvalModeLabel(mode) {
  return { safe: "安全", ask: "每次确认", never: "只读" }[mode] || mode || "安全";
}

function approvalModeControlLabel(mode) {
  return { safe: "安全模式", ask: "每次确认", never: "只读模式" }[mode] || "安全模式";
}

function setApprovalMenu(open) {
  const shouldOpen = Boolean(open) && !busy && !approvalChanging;
  ui.approvalModeMenu.hidden = !shouldOpen;
  ui.approvalModeTrigger.setAttribute("aria-expanded", String(shouldOpen));
  ui.approvalModeControl.classList.toggle("open", shouldOpen);
  if (shouldOpen) {
    const activeOption = ui.approvalModeMenu.querySelector('[aria-checked="true"]');
    window.requestAnimationFrame(() => activeOption && activeOption.focus());
  }
}

function toast(message, kind = "info") {
  const item = document.createElement("div");
  item.className = `toast ${kind}`;
  item.textContent = message;
  ui.toasts.append(item);
  window.setTimeout(() => item.remove(), 4400);
}

function conversationIsNearBottom() {
  return ui.conversation.scrollHeight - ui.conversation.scrollTop - ui.conversation.clientHeight < 72;
}

function scrollToBottom({ force = false } = {}) {
  if (!force && !followLatestMessage) return;
  ui.conversation.scrollTop = ui.conversation.scrollHeight;
  followLatestMessage = true;
}

function showConversation() {
  ui.empty.classList.add("hidden");
}

function showThinking(step) {
  showConversation();
  if (thinkingIndicator) {
    thinkingIndicator.querySelector("span:last-child").textContent = `第 ${step} 步`;
    scrollToBottom();
    return;
  }
  const row = document.createElement("div");
  row.className = "thinking-indicator";
  const spinner = document.createElement("span");
  spinner.className = "thinking-spinner";
  spinner.setAttribute("aria-hidden", "true");
  const label = document.createElement("strong");
  label.textContent = "CAworker 正在思考";
  const meta = document.createElement("span");
  meta.textContent = `第 ${step} 步`;
  row.append(spinner, label, meta);
  ui.messages.append(row);
  thinkingIndicator = row;
  scrollToBottom();
}

function clearThinking() {
  if (!thinkingIndicator) return;
  thinkingIndicator.remove();
  thinkingIndicator = null;
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    const index = match.index || 0;
    if (index > cursor) parent.append(document.createTextNode(text.slice(cursor, index)));
    const tokenText = match[0];
    if (tokenText.startsWith("`")) {
      const code = document.createElement("code");
      code.className = "inline-code";
      code.textContent = tokenText.slice(1, -1);
      parent.append(code);
    } else if (tokenText.startsWith("**")) {
      const strong = document.createElement("strong");
      strong.textContent = tokenText.slice(2, -2);
      parent.append(strong);
    } else {
      const parts = /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(tokenText);
      if (parts) {
        const link = document.createElement("a");
        link.textContent = parts[1];
        link.href = parts[2];
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        parent.append(link);
      } else {
        parent.append(document.createTextNode(tokenText));
      }
    }
    cursor = index + tokenText.length;
  }
  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

function isMarkdownBlockStart(line) {
  return /^(?:```|#{1,4}\s+|\s*[-*+]\s+|\s*\d+\.\s+|>\s?|---+$)/.test(line);
}

function makeCodeBlock(language, source) {
  const block = document.createElement("section");
  block.className = "code-block";
  const header = document.createElement("div");
  header.className = "code-block-header";
  const label = document.createElement("span");
  label.textContent = language || "code";
  const actions = document.createElement("div");
  actions.className = "code-block-actions";
  const lineCount = source ? source.split("\n").length : 0;
  if (lineCount > 20) {
    block.classList.add("collapsible", "collapsed");
    const expand = document.createElement("button");
    expand.type = "button";
    expand.className = "code-expand-button";
    expand.textContent = `展开 ${lineCount} 行`;
    expand.addEventListener("click", () => {
      const collapsed = block.classList.toggle("collapsed");
      expand.textContent = collapsed ? `展开 ${lineCount} 行` : "收起";
    });
    actions.append(expand);
  }
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "code-copy-button";
  copy.textContent = "复制";
  copy.addEventListener("click", async () => {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(source);
      } else {
        const fallback = document.createElement("textarea");
        fallback.value = source;
        fallback.style.position = "fixed";
        fallback.style.opacity = "0";
        document.body.append(fallback);
        fallback.select();
        document.execCommand("copy");
        fallback.remove();
      }
      copy.textContent = "已复制";
      window.setTimeout(() => { copy.textContent = "复制"; }, 1400);
    } catch (_) {
      toast("无法复制代码，请手动选择", "warning");
    }
  });
  const pre = document.createElement("pre");
  const code = document.createElement("code");
  code.textContent = source;
  pre.append(code);
  actions.append(copy);
  header.append(label, actions);
  block.append(header, pre);
  return block;
}

function renderMarkdown(target, source) {
  const lines = String(source || "").replace(/\r\n?/g, "\n").split("\n");
  const fragment = document.createDocumentFragment();
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }
    const fence = /^```([^\s`]*)\s*$/.exec(line);
    if (fence) {
      index += 1;
      const codeLines = [];
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      fragment.append(makeCodeBlock(fence[1], codeLines.join("\n")));
      continue;
    }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) {
      const title = document.createElement(`h${heading[1].length + 1}`);
      appendInlineMarkdown(title, heading[2]);
      fragment.append(title);
      index += 1;
      continue;
    }
    if (/^---+$/.test(line.trim())) {
      fragment.append(document.createElement("hr"));
      index += 1;
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = document.createElement("blockquote");
      while (index < lines.length && /^>\s?/.test(lines[index])) {
        if (quote.childNodes.length) quote.append(document.createElement("br"));
        appendInlineMarkdown(quote, lines[index].replace(/^>\s?/, ""));
        index += 1;
      }
      fragment.append(quote);
      continue;
    }
    const unordered = /^\s*[-*+]\s+/.test(line);
    const ordered = /^\s*\d+\.\s+/.test(line);
    if (unordered || ordered) {
      const list = document.createElement(ordered ? "ol" : "ul");
      const matcher = ordered ? /^\s*\d+\.\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/;
      while (index < lines.length) {
        const itemMatch = matcher.exec(lines[index]);
        if (!itemMatch) break;
        const item = document.createElement("li");
        appendInlineMarkdown(item, itemMatch[1]);
        list.append(item);
        index += 1;
      }
      fragment.append(list);
      continue;
    }

    const paragraph = document.createElement("p");
    let firstLine = true;
    while (index < lines.length && lines[index].trim() && !isMarkdownBlockStart(lines[index])) {
      if (!firstLine) paragraph.append(document.createElement("br"));
      appendInlineMarkdown(paragraph, lines[index]);
      firstLine = false;
      index += 1;
    }
    if (firstLine) {
      appendInlineMarkdown(paragraph, line);
      index += 1;
    }
    fragment.append(paragraph);
  }
  target.replaceChildren(fragment);
}

function queueAssistantRender(message) {
  if (message.frame) return;
  message.frame = window.requestAnimationFrame(() => {
    message.frame = null;
    appendPendingAssistantText(message);
    scrollToBottom();
  });
}

function appendPendingAssistantText(message) {
  if (!message.pendingText || !message.textNode) return;
  message.textNode.appendData(message.pendingText);
  message.pendingText = "";
}

function flushAssistantRender(message) {
  if (message.frame) window.cancelAnimationFrame(message.frame);
  message.frame = null;
  appendPendingAssistantText(message);
  message.body.classList.remove("streaming-plain");
  renderMarkdown(message.body, message.raw);
}

function addMessage(role, text, { streaming = false, turn = null } = {}) {
  if (role === "assistant") clearThinking();
  showConversation();
  const article = document.createElement("article");
  article.className = `message ${role}${streaming ? " streaming" : ""}`;
  if (Number.isInteger(turn) && turn > 0) article.dataset.turn = String(turn);

  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  if (role === "assistant") {
    avatar.innerHTML = `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="m9 6.5-5.5 5.5L9 17.5"></path>
      <path d="m15 6.5 5.5 5.5-5.5 5.5"></path>
      <path d="m13.75 4.5-3.5 15"></path>
    </svg>`;
  } else {
    avatar.textContent = "你";
  }

  const content = document.createElement("div");
  content.className = "message-content";
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "assistant" ? "CAworker" : "You";
  const body = document.createElement("div");
  body.className = "message-text";
  let textNode = null;
  if (role === "assistant") {
    body.classList.add("markdown-body");
    if (streaming) {
      body.classList.add("streaming-plain");
      textNode = document.createTextNode(text);
      body.append(textNode);
    } else {
      renderMarkdown(body, text);
    }
  } else {
    body.textContent = text;
  }
  content.append(label, body);
  article.append(avatar, content);
  ui.messages.append(article);
  scrollToBottom();
  return { article, body, raw: text, textNode, pendingText: "", frame: null };
}

function addSystemNote(text) {
  showConversation();
  const note = document.createElement("div");
  note.className = "system-note";
  note.textContent = text;
  ui.messages.append(note);
  scrollToBottom();
}

function summarizeArguments(raw) {
  try {
    const data = JSON.parse(raw);
    const candidates = [data.path, data.command, data.query, data.explanation];
    const primary = candidates.find((value) => typeof value === "string" && value.trim());
    return primary ? primary.replace(/\s+/g, " ").slice(0, 130) : raw.slice(0, 130);
  } catch (_) {
    return String(raw || "").replace(/\s+/g, " ").slice(0, 130);
  }
}

function toolLabel(name) {
  const labels = {
    update_plan: "更新任务计划",
    list_files: "浏览文件",
    read_file: "读取文件",
    search_text: "搜索代码",
    search_history: "检索压缩历史",
    write_file: "写入文件",
    replace_text: "修改文件",
    show_diff: "检查改动",
    run_command: "运行命令",
    delegate_task: "委派子 Agent",
    delegate_readonly_tasks: "并行委派",
    list_skills: "浏览 Skills",
    activate_skill: "激活 Skill",
    read_skill_resource: "读取 Skill 资源",
  };
  return labels[name] || name;
}

function addToolCard(data) {
  clearThinking();
  showConversation();
  const card = document.createElement("div");
  card.className = "tool-card running";
  card.dataset.name = data.name;
  const header = document.createElement("div");
  header.className = "tool-card-header";
  const title = document.createElement("strong");
  title.textContent = toolLabel(data.name);
  const status = document.createElement("span");
  status.textContent = "执行中";
  header.append(title, status);
  const detail = document.createElement("div");
  detail.className = "tool-card-detail";
  detail.textContent = summarizeArguments(data.arguments);
  card.append(header, detail);
  ui.messages.append(card);
  runningTools.push({ name: data.name, card, status, detail });
  addActivity(toolLabel(data.name), summarizeArguments(data.arguments), "tool");
  scrollToBottom();
}

function finishToolCard(data) {
  const index = runningTools.findIndex((item) => item.name === data.name);
  if (index < 0) return;
  const item = runningTools.splice(index, 1)[0];
  let payload = null;
  try {
    payload = JSON.parse(data.result);
  } catch (_) {
    payload = { ok: false, error: "无法解析工具结果" };
  }
  const ok = payload && payload.ok !== false;
  item.card.classList.remove("running");
  item.card.classList.add(ok ? "success" : "failed");
  item.status.textContent = ok ? "完成" : "失败";
  const resultDetail = payload.error || payload.path || payload.command || "操作已返回结果";
  if (resultDetail) item.detail.textContent = String(resultDetail).replace(/\s+/g, " ").slice(0, 180);
  addActivity(toolLabel(data.name), ok ? "操作完成" : String(payload.error || "操作失败"), ok ? "success" : "error");
}

function addActivity(title, detail, kind = "") {
  if (!activityStarted) {
    ui.activity.replaceChildren();
    activityStarted = true;
  }
  const item = document.createElement("div");
  item.className = `activity-item ${kind}`;
  const strong = document.createElement("strong");
  strong.textContent = title;
  const span = document.createElement("span");
  span.textContent = detail;
  item.append(strong, span);
  ui.activity.append(item);
}

function renderSnapshot(snapshot, { renderMessages = false } = {}) {
  currentSnapshot = snapshot;
  const config = snapshot.config || {};
  const status = snapshot.status || {};
  ui.workspaceName.textContent = config.workspace_name || "Workspace";
  ui.workspacePath.textContent = config.workspace || "";
  ui.workspaceName.title = config.workspace || config.workspace_name || "本地工作目录";
  ui.workspacePath.title = config.workspace || "";
  ui.modelName.textContent = config.model || "model";
  ui.modelProtocol.textContent = protocolLabel(config.protocol);
  ui.connectionDot.classList.add("online");
  const activeSession = (snapshot.sessions || []).find((session) => session.session_id === snapshot.session_id);
  ui.pageTitle.textContent = activeSession ? activeSession.title || activeSession.task_preview || "未命名会话" : "新会话";
  ui.pageTitle.title = ui.pageTitle.textContent;
  const approvalMode = config.approval || "safe";
  ui.pageMeta.textContent = `${config.workspace_name || "Workspace"} · ${approvalModeLabel(approvalMode)}模式`;
  ui.approvalModeLabel.textContent = approvalModeControlLabel(approvalMode);
  ui.approvalModeControl.dataset.mode = approvalMode;
  ui.approvalModeTrigger.title = {
    safe: "安全模式：普通修改自动执行，敏感命令需要确认",
    ask: "每次确认：所有文件修改和命令都需要确认",
    never: "只读模式：禁止文件修改和命令执行",
  }[approvalMode] || "切换审批模式";
  $$(".approval-mode-option").forEach((option) => {
    const selected = option.dataset.approvalMode === approvalMode;
    option.classList.toggle("selected", selected);
    option.setAttribute("aria-checked", String(selected));
  });
  ui.turnSummary.textContent = `${status.turns || 0} 轮 · ${status.total_steps || 0} 个步骤`;
  renderPlan(snapshot.plan || status.plan || {});
  renderChanges(status, snapshot.diff || {});
  renderRuntime(config, status);
  renderCommands(status.commands || []);
  renderSubagentSnapshot(status.subagents || {});
  renderSkillSnapshot(status.skills || {});
  renderSessions(snapshot.sessions || [], snapshot.session_id);
  loadWorkspaceFiles();
  if (renderMessages) renderConversation(snapshot.conversation || []);
  renderUndoActions(status.operations || []);
  renderRecovery(status.recovery || {});
}

function renderRuntime(config, status) {
  $("#runtimeTurns").textContent = String(status.turns || 0);
  $("#runtimeSteps").textContent = String(status.total_steps || 0);
  const used = Number(status.context_chars || 0);
  const limit = Number(config.max_context_chars || 0);
  const percent = limit ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  $("#runtimeContext").textContent = `${percent}%`;
  ui.compactUsage.textContent = `${percent}%`;
  ui.compactUsageValue.textContent = `${percent}%`;
  ui.compactUsageBar.style.width = `${percent}%`;
  ui.compactUsageDetail.textContent = `${used.toLocaleString("zh-CN")} / ${limit.toLocaleString("zh-CN")} 字符`;
  ui.compactButton.disabled = busy || !contextCanCompact();
  const subagents = status.subagents || {};
  const active = Array.isArray(subagents.active) ? subagents.active.length : 0;
  const history = Array.isArray(subagents.history) ? subagents.history.length : 0;
  $("#runtimeSubagents").textContent = String(active + history);
  const skills = status.skills || {};
  $("#runtimeSkills").textContent = String(
    Array.isArray(skills.available) ? skills.available.length : 0,
  );
  $("#runtimeModel").textContent = config.model || "—";
  $("#runtimeApproval").textContent = config.approval === "safe" ? "安全确认" : config.approval || "—";
  $("#runtimeTracking").textContent = status.workspace_tracking_complete === false ? "部分" : "完整";
  $("#runtimeTracking").classList.toggle("warning-text", status.workspace_tracking_complete === false);
  $("#runtimeBudget").textContent = `${config.max_steps || 0} 步`;
}

function formatDuration(milliseconds) {
  const value = Number(milliseconds);
  if (!Number.isFinite(value)) return "—";
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(value < 10000 ? 1 : 0)} s`;
}

function renderCommands(commands) {
  const items = Array.isArray(commands) ? [...commands].reverse() : [];
  ui.commandCount.textContent = String(items.length);
  const successful = items.filter((item) => item.exit_code === 0 && !item.timed_out && !item.cancelled).length;
  ui.commandSummary.textContent = items.length ? `${successful} 次成功 · ${items.length - successful} 次未通过` : "尚未执行命令";
  ui.commandList.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "panel-empty compact";
    empty.innerHTML = "<strong>暂无运行记录</strong><span>Agent 执行的检查和命令输出会显示在这里。</span>";
    ui.commandList.append(empty);
    return;
  }
  for (const command of items) {
    const success = command.exit_code === 0 && !command.timed_out && !command.cancelled;
    const card = document.createElement("article");
    card.className = `command-card ${success ? "success" : "failed"}`;
    const header = document.createElement("div");
    header.className = "command-card-header";
    const label = document.createElement("span");
    label.textContent = command.verification ? "验证" : "命令";
    const result = document.createElement("strong");
    result.textContent = command.cancelled ? "已取消" : command.timed_out ? "超时" : success ? "通过" : `退出 ${command.exit_code ?? "—"}`;
    header.append(label, result);
    const code = document.createElement("code");
    code.textContent = command.command || "";
    const meta = document.createElement("div");
    meta.className = "command-card-meta";
    meta.textContent = `${formatDuration(command.duration_ms)} · ${command.file_change_count || 0} 个文件变更`;
    const outputText = [command.stdout, command.stderr].filter(Boolean).join("\n").trim();
    card.append(header, code, meta);
    if (outputText) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "查看输出";
      const output = document.createElement("pre");
      output.textContent = outputText;
      details.append(summary, output);
      card.append(details);
    }
    ui.commandList.append(card);
  }
}

function renderSubagentSnapshot(snapshot) {
  subagentState.clear();
  const history = Array.isArray(snapshot.history) ? snapshot.history : [];
  const active = Array.isArray(snapshot.active) ? snapshot.active : [];
  for (const item of [...history, ...active]) {
    if (item && item.agent_id) subagentState.set(item.agent_id, item);
  }
  renderSubagents();
}

function renderSkillSnapshot(snapshot) {
  skillState.clear();
  const available = Array.isArray(snapshot.available) ? snapshot.available : [];
  for (const item of available) {
    if (item && item.name) skillState.set(item.name, item);
  }
  renderSkills();
}

function updateSkill(data) {
  if (!data || !data.name) return;
  const previous = skillState.get(data.name) || {};
  skillState.set(data.name, {
    ...previous,
    ...data,
    active: true,
    used_count: Math.max(1, Number(previous.used_count || 0) + (previous.active ? 0 : 1)),
  });
  renderSkills();
}

function renderSkills() {
  const items = [...skillState.values()];
  ui.skillSection.hidden = items.length === 0;
  ui.skillCount.textContent = String(items.length);
  ui.skillList.replaceChildren();
  for (const item of items) {
    const card = document.createElement("article");
    card.className = `skill-card ${item.active ? "active" : item.used_count ? "used" : ""}`;
    const header = document.createElement("div");
    header.className = "skill-card-header";
    const title = document.createElement("strong");
    title.textContent = item.name || "skill";
    const badge = document.createElement("span");
    badge.textContent = item.active ? "已激活" : item.used_count ? "已使用" : "可用";
    header.append(title, badge);
    const description = document.createElement("p");
    description.textContent = item.description || "可复用任务工作流";
    const meta = document.createElement("div");
    meta.className = "skill-card-meta";
    const resources = Number(item.resources || 0);
    meta.textContent = `${item.source || "本地"}${resources ? ` · ${resources} 个资源` : ""}`;
    card.append(header, description, meta);
    ui.skillList.append(card);
  }
}

function updateSubagent(data) {
  if (!data || !data.agent_id) return;
  const previous = subagentState.get(data.agent_id) || {};
  subagentState.set(data.agent_id, { ...previous, ...data });
  renderSubagents();
}

function renderSubagents() {
  const items = [...subagentState.values()].slice(-8).reverse();
  ui.subagentSection.hidden = items.length === 0;
  ui.subagentCount.textContent = String(items.length);
  ui.subagentList.replaceChildren();
  const modeNames = { explore: "探索", review: "审查" };
  const statusNames = {
    running: "执行中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  for (const item of items) {
    const card = document.createElement("article");
    card.className = `subagent-card ${item.status || "running"}`;
    const header = document.createElement("div");
    header.className = "subagent-card-header";
    const title = document.createElement("strong");
    title.textContent = item.label || item.agent_id || "子 Agent";
    const badge = document.createElement("span");
    badge.textContent = statusNames[item.status] || "执行中";
    header.append(title, badge);
    const meta = document.createElement("div");
    meta.className = "subagent-card-meta";
    const mode = modeNames[item.mode] || item.mode || "探索";
    const progress = item.status === "running" && item.step ? ` · 第 ${item.step} 步` : "";
    meta.textContent = `${mode}${progress}`;
    const summary = document.createElement("p");
    summary.textContent = item.status === "running"
      ? item.tool ? `正在使用 ${toolLabel(item.tool)}` : "正在分析独立任务"
      : item.summary || "已返回结构化报告";
    card.append(header, meta, summary);
    ui.subagentList.append(card);
  }
}

function protocolLabel(protocol) {
  const labels = {
    openai_chat: "OpenAI Chat 兼容接口",
    openai_responses: "OpenAI Responses 兼容接口",
    anthropic: "Anthropic 兼容接口",
  };
  return labels[protocol] || String(protocol || "本地模型接口").replaceAll("_", " ");
}

function renderConversation(messages) {
  clearThinking();
  ui.messages.replaceChildren();
  activeAssistant = null;
  lastAssistantText = "";
  if (!messages.length) {
    ui.empty.classList.remove("hidden");
    return;
  }
  ui.empty.classList.add("hidden");
  let inferredTurn = 0;
  for (const message of messages) {
    if (message.role !== "assistant") inferredTurn += 1;
    const turn = Number.isInteger(message.turn) && message.turn > 0 ? message.turn : inferredTurn;
    addMessage(message.role === "assistant" ? "assistant" : "user", message.content || "", { turn });
    if (message.role === "assistant") lastAssistantText = message.content || "";
  }
  window.requestAnimationFrame(() => scrollToBottom({ force: true }));
}

function renderPlan(plan) {
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const completed = steps.filter((step) => step.status === "completed").length;
  $("#planProgress").textContent = `${completed} / ${steps.length}`;
  ui.planList.replaceChildren();
  ui.planEmpty.hidden = steps.length > 0;
  ui.planList.hidden = steps.length === 0;
  const statusNames = {
    pending: "等待中",
    in_progress: "进行中",
    completed: "已完成",
    blocked: "已阻塞",
  };
  steps.forEach((step, index) => {
    const item = document.createElement("li");
    item.className = `plan-item ${step.status || "pending"}`;
    const marker = document.createElement("div");
    marker.className = "plan-marker";
    marker.textContent = step.status === "completed" ? "✓" : String(index + 1);
    const copy = document.createElement("div");
    copy.className = "plan-copy";
    const title = document.createElement("strong");
    title.textContent = step.step || "未命名步骤";
    const state = document.createElement("span");
    state.textContent = statusNames[step.status] || step.status || "等待中";
    copy.append(title, state);
    item.append(marker, copy);
    ui.planList.append(item);
  });
}

function renderChanges(status, diff) {
  const inspected = Array.isArray(status.inspected_files) ? status.inspected_files : [];
  const changed = Array.isArray(status.changed_files) ? status.changed_files : [];
  ui.inspectedCount.textContent = String(inspected.length);
  ui.changedCount.textContent = String(changed.length);
  changedFileSet = new Set(changed);
  ui.verificationCard.className = "verification-card neutral";
  if (!status.verification_required) {
    ui.verificationTitle.textContent = "无需验证";
    ui.verificationDetail.textContent = changed.length ? "当前状态不需要额外检查" : "还没有文件修改";
  } else if (status.verification_passed) {
    ui.verificationCard.className = "verification-card passed";
    ui.verificationTitle.textContent = "验证通过";
    ui.verificationDetail.textContent = "最新修改之后的检查已成功";
  } else {
    ui.verificationCard.className = "verification-card pending";
    ui.verificationTitle.textContent = "等待验证";
    ui.verificationDetail.textContent = "修改完成后需要运行检查";
  }
  if (diff && diff.truncated) toast("Diff 内容较长，界面仅显示截断结果", "warning");
}

function renderSessions(sessions, activeId) {
  allSessions = Array.isArray(sessions) ? sessions : [];
  renderSessionList(activeId);
}

function renderSessionList(activeId = currentSnapshot && currentSnapshot.session_id) {
  ui.sessions.replaceChildren();
  const query = ui.sessionSearch.value.trim().toLocaleLowerCase();
  const sessions = allSessions.filter((session) => {
    const text = `${session.title || ""} ${session.task_preview || ""}`.toLocaleLowerCase();
    return !query || text.includes(query);
  });
  ui.sessionCount.textContent = query ? `${sessions.length}/${allSessions.length}` : String(allSessions.length);
  if (!allSessions.length) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = "完成第一个任务后，会话会自动保存在这里。";
    ui.sessions.append(empty);
    return;
  }
  if (!sessions.length) {
    const empty = document.createElement("div");
    empty.className = "session-empty";
    empty.textContent = "没有匹配的会话。";
    ui.sessions.append(empty);
    return;
  }
  sessions.forEach((session) => {
    const row = document.createElement("div");
    row.className = `session-entry${session.session_id === activeId ? " active" : ""}`;
    row.dataset.pinned = String(Boolean(session.pinned));
    const button = document.createElement("button");
    button.type = "button";
    button.className = "session-item";
    button.dataset.sessionId = session.session_id;
    button.title = `${session.title || session.task_preview || "未命名会话"} · ${session.turns} 轮 · ${session.total_steps} 步`;
    const title = document.createElement("span");
    title.className = "session-title";
    title.textContent = session.title || session.task_preview || "未命名会话";
    button.append(title);
    button.addEventListener("click", () => resumeSession(session.session_id));
    const actions = document.createElement("div");
    actions.className = "session-actions";
    const pin = document.createElement("button");
    pin.type = "button";
    pin.className = "session-pin-button";
    pin.setAttribute("aria-label", session.pinned ? `取消固定 ${title.textContent}` : `固定 ${title.textContent}`);
    pin.title = session.pinned ? "取消固定" : "固定";
    pin.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 17v5M5 17h14M15 3.5a4 4 0 0 1 0 7l-1 6h-4l-1-6a4 4 0 0 1 0-7Z"/></svg>';
    pin.addEventListener("click", (event) => {
      event.stopPropagation();
      pinSession(session.session_id, !session.pinned);
    });
    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "session-menu-trigger";
    trigger.setAttribute("aria-label", `管理 ${title.textContent}`);
    trigger.title = "更多";
    trigger.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="19" cy="12" r="1.5"/></svg>';
    const menu = document.createElement("div");
    menu.className = "session-menu";
    const menuItems = [
      [session.pinned ? "取消固定" : "固定", () => pinSession(session.session_id, !session.pinned)],
      ["重命名", () => openRenameSession(session)],
      ["导出 Markdown", () => exportSession(session.session_id)],
      ["删除", () => confirmDeleteSession(session)],
    ];
    for (const [label, action] of menuItems) {
      const item = document.createElement("button");
      item.type = "button";
      item.textContent = label;
      if (label === "删除") item.className = "danger-text";
      item.addEventListener("click", (event) => {
        event.stopPropagation();
        menu.classList.remove("open");
        action();
      });
      menu.append(item);
    }
    trigger.addEventListener("click", (event) => {
      event.stopPropagation();
      $$(".session-menu.open").forEach((item) => { if (item !== menu) item.classList.remove("open"); });
      menu.classList.toggle("open");
    });
    actions.append(pin, trigger, menu);
    row.append(button, actions);
    ui.sessions.append(row);
  });
}

async function loadWorkspaceFiles() {
  try {
    const payload = await jsonRequest("/api/files");
    workspaceFiles = Array.isArray(payload.entries) ? payload.entries : [];
    changedFileSet = new Set(Array.isArray(payload.changed_files) ? payload.changed_files : []);
    hiddenWorkspaceFiles = Number(payload.hidden_files || 0);
    workspaceFilesTruncated = Boolean(payload.truncated);
    renderWorkspaceFiles();
  } catch (error) {
    ui.projectFiles.textContent = error.message;
  }
}

function buildWorkspaceTree(entries) {
  const root = { children: new Map() };
  for (const rawPath of entries) {
    const explicitFolder = rawPath.endsWith("/");
    const cleanPath = explicitFolder ? rawPath.slice(0, -1) : rawPath;
    const parts = cleanPath.split("/").filter(Boolean);
    let parent = root;
    for (let index = 0; index < parts.length; index += 1) {
      const name = parts[index];
      const folder = index < parts.length - 1 || explicitFolder;
      const path = parts.slice(0, index + 1).join("/") + (folder ? "/" : "");
      let node = parent.children.get(name);
      if (!node) {
        node = { name, path, folder, children: new Map(), changed: false, visible: true, selfMatches: true };
        parent.children.set(name, node);
      } else if (folder) {
        node.folder = true;
        node.path = path;
      }
      parent = node;
    }
  }
  return root;
}

function prepareWorkspaceTree(node, query) {
  let descendantMatches = false;
  let descendantChanged = false;
  for (const child of node.children.values()) {
    prepareWorkspaceTree(child, query);
    descendantMatches ||= child.visible;
    descendantChanged ||= child.changed;
  }
  if (!node.name) return;
  node.selfMatches = !query || node.path.toLocaleLowerCase().includes(query);
  node.visible = !query || node.selfMatches || descendantMatches;
  node.changed = changedFileSet.has(node.path) || descendantChanged;
}

function sortedWorkspaceNodes(children) {
  return [...children.values()].sort((left, right) => {
    if (left.folder !== right.folder) return left.folder ? -1 : 1;
    return workspaceTreeCollator.compare(left.name, right.name);
  });
}

function renderWorkspaceFiles() {
  const query = ui.fileSearch.value.trim().toLocaleLowerCase();
  const tree = buildWorkspaceTree(workspaceFiles);
  prepareWorkspaceTree(tree, query);
  ui.projectFiles.replaceChildren();
  const hasMatches = sortedWorkspaceNodes(tree.children).some((node) => node.visible);
  if (!hasMatches) {
    const empty = document.createElement("div");
    empty.className = "panel-empty compact";
    const strong = document.createElement("strong");
    strong.textContent = query ? "没有匹配文件" : "项目中没有可预览文件";
    const detail = document.createElement("span");
    detail.textContent = query ? "尝试缩短筛选关键词。" : "空目录和忽略目录不会显示。";
    empty.append(strong, detail);
    ui.projectFiles.append(empty);
  }
  let visibleCount = 0;
  const appendNodes = (children, depth, ancestorMatches = false) => {
    for (const node of sortedWorkspaceNodes(children)) {
      if (query && !ancestorMatches && !node.visible) continue;
      visibleCount += 1;
      const collapsed = node.folder && collapsedFolders.has(node.path) && !query;
      const row = document.createElement("button");
      row.type = "button";
      row.className = `project-file-row ${node.folder ? "folder" : "file"}${node.changed ? " changed" : ""}`;
      row.style.setProperty("--file-depth", String(depth));
      row.title = node.path;

      const chevron = document.createElement("span");
      chevron.className = `project-file-chevron${collapsed ? " collapsed" : ""}`;
      if (node.folder) {
        chevron.innerHTML = '<svg viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m5.5 3.5 4 4-4 4"/></svg>';
        row.setAttribute("aria-expanded", String(!collapsed));
      }
      const icon = document.createElement("span");
      icon.className = "project-file-icon";
      icon.innerHTML = node.folder
        ? '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3.5 7V5.5a2 2 0 0 1 2-2H10l2 2h6.5a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2Z"/></svg>'
        : '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 3.5h8l4 4v13H6z"/><path d="M14 3.5v4h4"/></svg>';
      const name = document.createElement("span");
      name.className = "project-file-name";
      name.textContent = node.name;
      row.append(chevron, icon, name);

      if (node.folder) {
        row.addEventListener("click", () => {
          if (collapsedFolders.has(node.path)) collapsedFolders.delete(node.path);
          else collapsedFolders.add(node.path);
          renderWorkspaceFiles();
        });
      } else {
        if (changedFileSet.has(node.path)) {
          const badge = document.createElement("small");
          badge.textContent = "M";
          row.append(badge);
        }
        row.addEventListener("click", () => openFilePreview(node.path));
      }
      ui.projectFiles.append(row);
      if (node.folder && !collapsed) {
        appendNodes(node.children, depth + 1, ancestorMatches || node.selfMatches);
      }
    }
  };
  appendNodes(tree.children, 0);
  ui.fileListNote.textContent = `${visibleCount} / ${workspaceFiles.length} 项${workspaceFilesTruncated ? " · 列表已截断" : ""}${hiddenWorkspaceFiles ? ` · ${hiddenWorkspaceFiles} 个敏感改动已隐藏` : ""}`;
}

async function openFilePreview(path) {
  activePreviewPath = path;
  ui.fileModal.hidden = false;
  ui.filePreviewTitle.textContent = path;
  ui.filePreviewMeta.textContent = "正在读取…";
  ui.filePreviewContent.textContent = "正在读取…";
  ui.fileDiff.hidden = true;
  try {
    const file = await jsonRequest(`/api/file?path=${encodeURIComponent(path)}`);
    ui.filePreviewTitle.textContent = file.path;
    ui.filePreviewMeta.textContent = `${file.lines} 行 · ${formatBytes(file.size)}${file.truncated ? " · 预览已截断" : ""}`;
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = file.content || "（空文件）";
    pre.append(code);
    ui.filePreviewContent.replaceChildren(pre);
    ui.fileDiff.hidden = !file.changed;
  } catch (error) {
    ui.filePreviewMeta.textContent = "无法预览";
    ui.filePreviewContent.textContent = error.message;
  }
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function switchTab(name) {
  $$(".tab").forEach((tab) => {
    const selected = tab.dataset.tab === name;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-selected", String(selected));
  });
  $$(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.dataset.panel === name));
}

function openApproval(data) {
  pendingApproval = data;
  ui.approvalTool.textContent = toolLabel(data.tool);
  ui.approvalSummary.textContent = data.summary || "该操作会修改本地状态。";
  ui.approvalModal.hidden = false;
  ui.reject.focus();
}

async function changeApprovalMode(nextMode) {
  if (busy || approvalChanging) return;
  const currentMode = currentSnapshot && currentSnapshot.config
    ? currentSnapshot.config.approval || "safe"
    : "safe";
  setApprovalMenu(false);
  if (nextMode === currentMode) return;
  approvalChanging = true;
  ui.approvalModeTrigger.disabled = true;
  try {
    const snapshot = await jsonRequest("/api/settings/approval", {
      method: "POST",
      body: JSON.stringify({ mode: nextMode }),
    });
    renderSnapshot(snapshot);
    toast(`已切换为${approvalModeLabel(nextMode)}模式`, "success");
    addActivity("审批模式已切换", `${approvalModeLabel(currentMode)} → ${approvalModeLabel(nextMode)}`, "warning");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    approvalChanging = false;
    ui.approvalModeTrigger.disabled = busy;
  }
}

async function answerApproval(approved) {
  if (!pendingApproval) return;
  const data = pendingApproval;
  pendingApproval = null;
  ui.approvalModal.hidden = true;
  try {
    await jsonRequest("/api/approval", {
      method: "POST",
      body: JSON.stringify({ id: data.id, approved }),
    });
    addActivity(approved ? "已允许操作" : "已拒绝操作", toolLabel(data.tool), approved ? "success" : "warning");
  } catch (error) {
    toast(error.message, "error");
  }
}

function handleEvent(event, data) {
  switch (event) {
    case "turn_started":
      addActivity("任务已提交", "开始分析当前工作区");
      break;
    case "model_start":
      setBusy(true, `第 ${data.step} 步`);
      showThinking(data.step);
      addActivity("模型思考", `第 ${data.step} 步 · 第 ${data.turn} 轮`);
      break;
    case "assistant_stream_start":
      clearThinking();
      activeAssistant = addMessage("assistant", "", { streaming: true, turn: data.turn || liveTurnNumber });
      break;
    case "assistant_text_delta":
      if (!activeAssistant) activeAssistant = addMessage("assistant", "", { streaming: true, turn: data.turn || liveTurnNumber });
      activeAssistant.pendingText += data.text || "";
      activeAssistant.raw += data.text || "";
      lastAssistantText = activeAssistant.raw;
      queueAssistantRender(activeAssistant);
      break;
    case "assistant_stream_end":
      if (activeAssistant) {
        flushAssistantRender(activeAssistant);
        activeAssistant.article.classList.remove("streaming");
      }
      activeAssistant = null;
      break;
    case "assistant_text":
      clearThinking();
      if (!data.streamed && data.text) {
        addMessage("assistant", data.text, { turn: data.turn || liveTurnNumber });
        lastAssistantText = data.text;
      }
      break;
    case "tool_start":
      addToolCard(data);
      break;
    case "tool_end":
      finishToolCard(data);
      break;
    case "subagent_started":
      updateSubagent(data);
      addActivity("子 Agent 已启动", `${data.label || data.agent_id} · ${data.mode}`, "subagent");
      break;
    case "subagent_progress":
      updateSubagent(data);
      break;
    case "subagent_finished":
      updateSubagent(data);
      addActivity(
        "子 Agent 已回报",
        `${data.label || data.agent_id} · ${data.status || "completed"}`,
        data.status === "completed" ? "success" : "error",
      );
      break;
    case "skill_activated":
      updateSkill(data);
      addActivity("Skill 已激活", `${data.name || "skill"} · ${data.source || "本地"}`, "skill");
      break;
    case "skill_resource_read":
      addActivity("读取 Skill 资源", `${data.name || "skill"} · ${data.path || ""}`, "skill");
      break;
    case "plan_updated":
      renderPlan(data);
      addActivity("计划已更新", `${(data.steps || []).length} 个步骤`, "success");
      break;
    case "context_compacted":
      addSystemNote(`上下文已压缩为 ${data.messages} 条消息`);
      addActivity("上下文压缩", `保留 ${data.messages} 条消息`);
      break;
    case "recovery_started":
      addActivity(
        data.mode === "retry" ? "正在恢复并重试" : "正在继续任务",
        data.mode === "retry" && (data.restored_files || []).length
          ? `已恢复 ${(data.restored_files || []).length} 个文件`
          : "沿用当前工作区状态",
        "warning",
      );
      break;
    case "verification_required":
      addSystemNote("文件已修改，CAworker 正在执行必要的验证");
      addActivity("等待验证", `${(data.files || []).length} 个文件`, "warning");
      break;
    case "plan_completion_required":
      addActivity("计划尚未完成", "继续执行剩余步骤", "warning");
      break;
    case "approval_required":
      openApproval(data);
      addActivity("等待你的确认", toolLabel(data.tool), "warning");
      break;
    case "session_saved":
      toast("会话已自动保存");
      break;
    case "session_error":
      toast(data.message || "会话保存失败", "error");
      break;
    case "checkpoint_error":
      toast("任务已完成，但本轮修改无法创建安全撤销点", "warning");
      break;
    case "cancelled":
      clearThinking();
      pendingApproval = null;
      ui.approvalModal.hidden = true;
      addSystemNote(`操作已取消：${data.phase || "当前步骤"}`);
      break;
    case "completed":
      clearThinking();
      addActivity("任务完成", "结果已生成", "success");
      break;
    case "stopped":
      clearThinking();
      addActivity("任务停止", data.reason || "未完成", "error");
      break;
    default:
      break;
  }
}

function handleRecord(record) {
  if (record.type === "event") {
    handleEvent(record.event, record.data || {});
    return;
  }
  if (record.type === "fatal_error") {
    setRunError(record.message || "任务执行失败");
    return;
  }
  if (record.type === "recovery_error") {
    if (record.snapshot) renderSnapshot(record.snapshot);
    setRunError(record.message || "无法恢复这项任务");
    return;
  }
  if (record.type === "turn_complete") {
    const result = record.result || {};
    if (result.final && result.final.trim() !== lastAssistantText.trim()) {
      addMessage("assistant", result.final, { turn: liveTurnNumber });
      lastAssistantText = result.final;
    }
    if (record.snapshot) renderSnapshot(record.snapshot);
    setBusy(false);
    ui.runStatusText.textContent = result.success ? "已完成" : "已停止";
    window.setTimeout(() => {
      if (!busy) ui.runStatusText.textContent = "就绪";
    }, 1800);
  }
}

async function consumeTurnResponse(response) {
  if (!response.body) throw new Error("浏览器不支持流式响应");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let recordsSinceYield = 0;
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      handleRecord(JSON.parse(line));
      recordsSinceYield += 1;
      if (recordsSinceYield >= STREAM_RECORD_BATCH_SIZE) {
        recordsSinceYield = 0;
        await new Promise((resolve) => window.setTimeout(resolve, 0));
      }
    }
    if (done) break;
  }
  if (buffer.trim()) handleRecord(JSON.parse(buffer));
}

async function sendTurn(message) {
  const task = message.trim();
  if (!task || busy) return;
  followLatestMessage = true;
  liveTurnNumber = Number(currentSnapshot && currentSnapshot.status && currentSnapshot.status.turns || 0) + 1;
  addMessage("user", task, { turn: liveTurnNumber });
  lastAssistantText = "";
  activeAssistant = null;
  activeRecovery = null;
  $$(".recovery-card").forEach((item) => item.remove());
  ui.input.value = "";
  resizeComposer();
  setBusy(true, "正在开始");
  try {
    const response = await request("/api/turn", {
      method: "POST",
      body: JSON.stringify({ message: task }),
    });
    await consumeTurnResponse(response);
  } catch (error) {
    setRunError(error.message || "无法完成任务");
  } finally {
    if (busy) setBusy(false);
  }
}

function recoveryPrompt(mode, recovery) {
  const task = String(recovery && recovery.task || "").trim();
  if (mode === "retry") {
    return `重新尝试上一轮任务：“${task}”。失败轮次产生的文件修改已安全恢复；请重新分析，完成任务并验证结果。`;
  }
  return `继续完成上一轮未完成的任务：“${task}”。保留当前已有的有效修改，先检查现状，再完成剩余工作并验证结果。`;
}

async function recoverTurn(mode) {
  const recovery = activeRecovery;
  if (!recovery || busy || !["continue", "retry"].includes(mode)) return;
  if (mode === "retry" && recovery.can_retry !== true) return;
  followLatestMessage = true;
  liveTurnNumber = Number(currentSnapshot && currentSnapshot.status && currentSnapshot.status.turns || 0) + 1;
  addMessage("user", recoveryPrompt(mode, recovery), { turn: liveTurnNumber });
  lastAssistantText = "";
  activeAssistant = null;
  activeRecovery = null;
  $$(".recovery-card").forEach((item) => item.remove());
  setBusy(true, mode === "retry" ? "正在恢复" : "正在继续");
  try {
    const response = await request("/api/recover", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    await consumeTurnResponse(response);
  } catch (error) {
    setRunError(error.message || "无法恢复这项任务");
  } finally {
    if (busy) setBusy(false);
  }
}

async function cancelTurn() {
  if (!busy || cancelling) return false;
  cancelling = true;
  ui.send.disabled = true;
  ui.runStatusText.textContent = "正在停止";
  try {
    await jsonRequest("/api/cancel", {
      method: "POST",
      body: "{}",
    });
    toast("正在安全停止当前任务");
    return true;
  } catch (error) {
    cancelling = false;
    ui.send.disabled = false;
    toast(error.message || "无法停止当前任务", "error");
    return false;
  }
}

async function stopTaskFromApproval() {
  if (await cancelTurn()) {
    pendingApproval = null;
    ui.approvalModal.hidden = true;
  }
}

function openCompactContext() {
  if (busy || compacting || !contextCanCompact()) return;
  ui.compactModal.hidden = false;
  ui.confirmCompact.focus();
}

async function compactContext() {
  if (busy || compacting) return;
  compacting = true;
  ui.confirmCompact.disabled = true;
  ui.confirmCompact.textContent = "正在压缩…";
  ui.compactButton.disabled = true;
  try {
    const snapshot = await jsonRequest("/api/context/compact", {
      method: "POST",
      body: "{}",
    });
    const report = snapshot.compaction || {};
    renderSnapshot(snapshot);
    ui.compactModal.hidden = true;
    if (report.compacted) {
      const before = Number(report.before_chars || 0);
      const after = Number(report.after_chars || 0);
      addSystemNote(`上下文已手动压缩：${before.toLocaleString("zh-CN")} → ${after.toLocaleString("zh-CN")} 字符`);
      addActivity("手动压缩上下文", `释放 ${(Math.max(0, before - after)).toLocaleString("zh-CN")} 字符`, "success");
      toast("上下文已压缩，完整聊天记录仍然保留");
    } else {
      toast("当前上下文还很短，暂时不需要压缩", "warning");
    }
  } catch (error) {
    toast(error.message || "无法压缩上下文", "error");
  } finally {
    compacting = false;
    ui.confirmCompact.disabled = false;
    ui.confirmCompact.textContent = "立即压缩";
    ui.compactButton.disabled = busy || !contextCanCompact();
  }
}

async function newSession() {
  if (busy) return;
  try {
    const snapshot = await jsonRequest("/api/session/new", {
      method: "POST",
      body: "{}",
    });
    activityStarted = false;
    ui.activity.replaceChildren();
    renderSnapshot(snapshot, { renderMessages: true });
    closeMobilePanels();
    ui.input.focus();
    toast("已创建新的空白会话");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function resumeSession(id) {
  if (busy) return;
  try {
    const snapshot = await jsonRequest("/api/session/resume", {
      method: "POST",
      body: JSON.stringify({ id }),
    });
    activityStarted = false;
    ui.activity.replaceChildren();
    renderSnapshot(snapshot, { renderMessages: true });
    closeMobilePanels();
    ui.input.focus();
    const drifted = snapshot.resume && snapshot.resume.drifted ? snapshot.resume.drifted.length : 0;
    toast(drifted ? `会话已恢复；${drifted} 个文件在关闭期间发生变化` : "会话已恢复", drifted ? "warning" : "info");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function pinSession(id, pinned) {
  if (busy) return;
  try {
    const snapshot = await jsonRequest("/api/session/pin", {
      method: "POST",
      body: JSON.stringify({ id, pinned }),
    });
    renderSnapshot(snapshot);
    toast(pinned ? "会话已固定" : "已取消固定");
  } catch (error) {
    toast(error.message, "error");
  }
}

function openRenameSession(session) {
  pendingRenameSession = session.session_id;
  ui.renameInput.value = session.title || session.task_preview || "";
  ui.renameModal.hidden = false;
  window.setTimeout(() => {
    ui.renameInput.focus();
    ui.renameInput.select();
  }, 0);
}

async function saveSessionRename() {
  const id = pendingRenameSession;
  const title = ui.renameInput.value.trim();
  if (!id || !title) return;
  try {
    const snapshot = await jsonRequest("/api/session/rename", {
      method: "POST",
      body: JSON.stringify({ id, title }),
    });
    ui.renameModal.hidden = true;
    pendingRenameSession = null;
    renderSnapshot(snapshot);
    toast("会话已重命名");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function exportSession(id) {
  try {
    const response = await request(`/api/session/export?id=${encodeURIComponent(id)}`);
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
    const filename = match ? decodeURIComponent(match[1]) : `caworker-${id}.md`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
    toast("会话已导出为 Markdown");
  } catch (error) {
    toast(error.message, "error");
  }
}

function askForConfirmation(title, message, action) {
  pendingConfirmAction = action;
  ui.confirmTitle.textContent = title;
  ui.confirmMessage.textContent = message;
  ui.confirmModal.hidden = false;
  $("#cancelConfirmButton").focus();
}

function confirmDeleteSession(session) {
  askForConfirmation(
    "删除这个会话？",
    `“${session.title || session.task_preview || "未命名会话"}”的本地会话记录将被永久删除，项目文件不会受到影响。`,
    async () => {
      const snapshot = await jsonRequest("/api/session/delete", {
        method: "POST",
        body: JSON.stringify({ id: session.session_id }),
      });
      renderSnapshot(snapshot);
      toast("会话记录已删除");
    },
  );
}

function confirmUndoOperation(operation) {
  const fileCount = Number(operation.file_count || 0);
  askForConfirmation(
    "撤销本轮修改？",
    `将恢复第 ${operation.turn} 轮修改涉及的 ${fileCount} 个文件。后续无关修改不会受到影响。`,
    async () => {
      const snapshot = await jsonRequest("/api/undo", {
        method: "POST",
        body: JSON.stringify({ operation_id: operation.id }),
      });
      renderSnapshot(snapshot);
      const count = snapshot.undo && Number(snapshot.undo.file_count || 0);
      toast(`已撤销第 ${operation.turn} 轮对 ${count} 个文件的修改`);
    },
  );
}

function renderUndoActions(operations) {
  $$(".turn-undo-action").forEach((item) => item.remove());
  if (!Array.isArray(operations)) return;
  for (const operation of operations) {
    if (!operation || !Number.isInteger(operation.turn) || !operation.file_count) continue;
    const messages = $$(`.message.assistant[data-turn="${operation.turn}"]`);
    const message = messages.at(-1);
    if (!message) continue;
    const content = message.querySelector(".message-content");
    if (!content) continue;

    const actions = document.createElement("div");
    actions.className = "message-actions turn-undo-action";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "turn-undo-button";
    button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M9 7H5v-4"/><path d="M5.5 7.5A8 8 0 1 1 4 14"/></svg><span></span>';
    const label = button.querySelector("span");
    if (operation.status === "undone") {
      label.textContent = "本轮修改已撤销";
      button.disabled = true;
    } else if (!operation.can_undo) {
      label.textContent = "本轮修改无法撤销";
      button.disabled = true;
      button.title = operation.blocked_reason || "相关文件已发生后续变化";
    } else {
      label.textContent = "撤销本轮修改";
      button.title = `恢复本轮修改的 ${operation.file_count} 个文件`;
      button.addEventListener("click", () => confirmUndoOperation(operation));
    }
    actions.append(button);
    content.append(actions);
  }
}

function recoveryReasonLabel(reason) {
  const labels = {
    cancelled: "任务已由用户停止",
    max_steps: "已达到单轮步骤上限",
    repeated_tool_call: "检测到重复工具调用",
    unverified_changes: "修改尚未通过验证",
    incomplete_plan: "计划仍有未完成步骤",
    empty_model_response: "模型没有返回有效内容",
    blocked: "任务被外部条件阻塞",
    runtime_error: "模型或本地运行发生错误",
  };
  return labels[reason] || "任务在完成前停止";
}

function renderRecovery(recovery) {
  $$(".recovery-card").forEach((item) => item.remove());
  activeRecovery = recovery && recovery.available ? recovery : null;
  if (!activeRecovery) return;

  const card = document.createElement("div");
  card.className = "recovery-card";
  const heading = document.createElement("div");
  heading.className = "recovery-card-heading";
  const icon = document.createElement("span");
  icon.className = "recovery-card-icon";
  icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 8v4l2.5 1.5"/><path d="M5.5 7.5H2.5v-3"/><path d="M4 7a8.5 8.5 0 1 1-.5 9"/></svg>';
  const copy = document.createElement("div");
  copy.className = "recovery-card-copy";
  const title = document.createElement("strong");
  title.textContent = "这项任务可以恢复";
  const detail = document.createElement("span");
  detail.textContent = `${recoveryReasonLabel(activeRecovery.reason)}。你可以保留当前进度继续，或恢复文件后重新尝试。`;
  copy.append(title, detail);
  heading.append(icon, copy);

  const actions = document.createElement("div");
  actions.className = "recovery-actions";
  const continueButton = document.createElement("button");
  continueButton.type = "button";
  continueButton.className = "primary";
  continueButton.textContent = "继续任务";
  continueButton.addEventListener("click", () => recoverTurn("continue"));
  const retryButton = document.createElement("button");
  retryButton.type = "button";
  retryButton.textContent = "恢复并重试";
  retryButton.dataset.blocked = String(activeRecovery.can_retry !== true);
  retryButton.disabled = busy || activeRecovery.can_retry !== true;
  retryButton.title = activeRecovery.can_retry === true
    ? "恢复失败轮次开始前的文件状态并重新执行"
    : activeRecovery.retry_blocked_reason || "没有可用的安全撤销点";
  retryButton.addEventListener("click", () => recoverTurn("retry"));
  actions.append(continueButton, retryButton);
  card.append(heading, actions);

  if (activeRecovery.can_retry !== true && activeRecovery.retry_blocked_reason) {
    const note = document.createElement("p");
    note.className = "recovery-blocked-note";
    note.textContent = `无法安全重试：${activeRecovery.retry_blocked_reason}`;
    card.append(note);
  }

  const messages = $$(`.message.assistant[data-turn="${activeRecovery.turn}"]`);
  const content = messages.at(-1) && messages.at(-1).querySelector(".message-content");
  if (content) {
    content.append(card);
  } else {
    showConversation();
    ui.messages.append(card);
  }
}

async function acceptConfirmation() {
  const action = pendingConfirmAction;
  if (!action) return;
  pendingConfirmAction = null;
  ui.confirmModal.hidden = true;
  try {
    await action();
  } catch (error) {
    toast(error.message, "error");
  }
}

function cleanDiffPath(header) {
  const value = String(header || "").replace(/^(?:---|\+\+\+)\s+/, "").split("\t")[0].trim();
  if (!value || value === "/dev/null") return "";
  return value.replace(/^[ab]\//, "");
}

function parseUnifiedDiff(raw, declaredFiles = []) {
  const lines = String(raw || "").replace(/\r\n?/g, "\n").split("\n");
  const files = [];
  let current = null;
  let oldLine = null;
  let newLine = null;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.startsWith("--- ") && index + 1 < lines.length && lines[index + 1].startsWith("+++ ")) {
      const oldPath = cleanDiffPath(line);
      const newPath = cleanDiffPath(lines[index + 1]);
      current = {
        path: newPath || oldPath || `文件 ${files.length + 1}`,
        oldPath,
        newPath,
        additions: 0,
        deletions: 0,
        lines: [],
      };
      files.push(current);
      oldLine = null;
      newLine = null;
      index += 1;
      continue;
    }
    if (!current) continue;

    const hunk = /^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@(.*)$/.exec(line);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      current.lines.push({ type: "hunk", content: line, old: null, new: null });
      continue;
    }
    if (line.startsWith("+") && !line.startsWith("+++")) {
      current.lines.push({ type: "add", content: line.slice(1), old: null, new: newLine });
      current.additions += 1;
      if (newLine !== null) newLine += 1;
      continue;
    }
    if (line.startsWith("-") && !line.startsWith("---")) {
      current.lines.push({ type: "delete", content: line.slice(1), old: oldLine, new: null });
      current.deletions += 1;
      if (oldLine !== null) oldLine += 1;
      continue;
    }
    if (line.startsWith(" ")) {
      current.lines.push({ type: "context", content: line.slice(1), old: oldLine, new: newLine });
      if (oldLine !== null) oldLine += 1;
      if (newLine !== null) newLine += 1;
      continue;
    }
    if (line) current.lines.push({ type: "meta", content: line, old: null, new: null });
  }

  for (const path of declaredFiles) {
    if (files.some((file) => file.path === path)) continue;
    files.push({
      path,
      oldPath: path,
      newPath: path,
      additions: 0,
      deletions: 0,
      lines: [{ type: "meta", content: "该文件的文本 Diff 当前不可用。", old: null, new: null }],
    });
  }
  return files;
}

function renderDiffFile(index) {
  const file = currentDiffFiles[index];
  if (!file) return;
  activeDiffIndex = index;
  $$(".diff-file-item").forEach((item, itemIndex) => {
    item.classList.toggle("active", itemIndex === index);
  });
  ui.diffActivePath.textContent = file.path;
  ui.diffActivePath.title = file.path;
  ui.diffSummary.replaceChildren();
  const additions = document.createElement("span");
  additions.className = "diff-additions";
  additions.textContent = `+${file.additions}`;
  const deletions = document.createElement("span");
  deletions.className = "diff-deletions";
  deletions.textContent = `−${file.deletions}`;
  ui.diffSummary.append(additions, deletions);
  if (currentDiffTruncated) {
    const truncated = document.createElement("span");
    truncated.className = "diff-truncated";
    truncated.textContent = "内容已截断";
    ui.diffSummary.append(truncated);
  }

  const fragment = document.createDocumentFragment();
  for (const line of file.lines) {
    const row = document.createElement("div");
    row.className = `diff-line ${line.type}`;
    if (line.type === "hunk" || line.type === "meta") {
      const meta = document.createElement("code");
      meta.className = "diff-line-meta";
      meta.textContent = line.content;
      row.append(meta);
    } else {
      const oldNumber = document.createElement("span");
      oldNumber.className = "diff-line-number";
      oldNumber.textContent = line.old === null ? "" : String(line.old);
      const newNumber = document.createElement("span");
      newNumber.className = "diff-line-number";
      newNumber.textContent = line.new === null ? "" : String(line.new);
      const prefix = document.createElement("span");
      prefix.className = "diff-line-prefix";
      prefix.textContent = line.type === "add" ? "+" : line.type === "delete" ? "−" : " ";
      const code = document.createElement("code");
      code.textContent = line.content || " ";
      row.append(oldNumber, newNumber, prefix, code);
    }
    fragment.append(row);
  }
  if (!file.lines.length) {
    const empty = document.createElement("div");
    empty.className = "diff-empty";
    empty.textContent = "这个文件没有可显示的文本差异。";
    fragment.append(empty);
  }
  ui.diffContent.replaceChildren(fragment);
  ui.diffContent.scrollTop = 0;
  ui.diffContent.scrollLeft = 0;
}

function renderDiff(diff) {
  currentDiffTruncated = Boolean(diff && diff.truncated);
  currentDiffFiles = parseUnifiedDiff(diff && diff.diff, Array.isArray(diff && diff.files) ? diff.files : []);
  activeDiffIndex = -1;
  ui.diffFileList.replaceChildren();
  ui.diffFileCount.textContent = String(currentDiffFiles.length);
  if (!currentDiffFiles.length) {
    ui.diffActivePath.textContent = "本次会话尚无文件改动";
    ui.diffSummary.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "diff-empty";
    empty.textContent = "CAworker 修改文件后，差异会按文件显示在这里。";
    ui.diffContent.replaceChildren(empty);
    return;
  }

  currentDiffFiles.forEach((file, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "diff-file-item";
    button.title = file.path;
    const path = document.createElement("span");
    path.textContent = file.path;
    const stats = document.createElement("small");
    stats.textContent = `+${file.additions}  −${file.deletions}`;
    button.append(path, stats);
    button.addEventListener("click", () => renderDiffFile(index));
    ui.diffFileList.append(button);
  });
  renderDiffFile(0);
}

async function showDiff() {
  ui.diffModal.hidden = false;
  ui.diffFileList.replaceChildren();
  ui.diffFileCount.textContent = "0";
  ui.diffActivePath.textContent = "正在读取改动…";
  ui.diffSummary.replaceChildren();
  ui.diffContent.textContent = "正在读取…";
  try {
    renderDiff(await jsonRequest("/api/diff"));
  } catch (error) {
    ui.diffActivePath.textContent = "无法读取改动";
    ui.diffContent.textContent = error.message;
  }
}

async function showFileDiff() {
  if (!activePreviewPath) return;
  ui.fileModal.hidden = true;
  ui.diffModal.hidden = false;
  ui.diffActivePath.textContent = "正在读取改动…";
  ui.diffContent.textContent = "正在读取…";
  try {
    renderDiff(await jsonRequest(`/api/diff?path=${encodeURIComponent(activePreviewPath)}`));
  } catch (error) {
    ui.diffActivePath.textContent = "无法读取改动";
    ui.diffContent.textContent = error.message;
  }
}

function resizeComposer() {
  ui.input.style.height = "auto";
  ui.input.style.height = `${Math.min(ui.input.scrollHeight, 170)}px`;
}

function closeMobilePanels() {
  ui.sidebar.classList.remove("open");
  ui.inspector.classList.remove("open");
  ui.backdrop.classList.remove("visible");
}

function openMobilePanel(panel) {
  closeMobilePanels();
  panel.classList.add("open");
  ui.backdrop.classList.add("visible");
}

async function bootstrap() {
  try {
    const snapshot = await jsonRequest("/api/bootstrap");
    renderSnapshot(snapshot, { renderMessages: true });
    setBusy(Boolean(snapshot.busy), snapshot.busy ? "正在运行" : "就绪");
  } catch (error) {
    setRunError(`无法连接本地 CAworker：${error.message}`);
  }
}

ui.form.addEventListener("submit", (event) => {
  event.preventDefault();
  if (busy) {
    cancelTurn();
    return;
  }
  sendTurn(ui.input.value);
});

ui.input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    ui.form.requestSubmit();
  }
});
ui.input.addEventListener("input", resizeComposer);
ui.conversation.addEventListener("scroll", () => {
  followLatestMessage = conversationIsNearBottom();
}, { passive: true });
ui.newSession.addEventListener("click", newSession);
ui.sessionSearch.addEventListener("input", () => renderSessionList());
ui.fileSearch.addEventListener("input", () => renderWorkspaceFiles());
$("#refreshSessionsButton").addEventListener("click", bootstrap);
$("#refreshFilesButton").addEventListener("click", loadWorkspaceFiles);
$("#diffButton").addEventListener("click", showDiff);
ui.compactButton.addEventListener("click", openCompactContext);
ui.approvalModeTrigger.addEventListener("click", () => {
  setApprovalMenu(ui.approvalModeMenu.hidden);
});
ui.approvalModeMenu.addEventListener("click", (event) => {
  const option = event.target.closest(".approval-mode-option");
  if (option) changeApprovalMode(option.dataset.approvalMode);
});
ui.approvalModeMenu.addEventListener("keydown", (event) => {
  const options = $$(".approval-mode-option");
  const current = options.indexOf(document.activeElement);
  let next = null;
  if (event.key === "ArrowDown") next = (current + 1 + options.length) % options.length;
  if (event.key === "ArrowUp") next = (current - 1 + options.length) % options.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = options.length - 1;
  if (next !== null) {
    event.preventDefault();
    options[next].focus();
  } else if (event.key === "Escape") {
    event.preventDefault();
    setApprovalMenu(false);
    ui.approvalModeTrigger.focus();
  }
});
document.addEventListener("click", (event) => {
  if (!ui.approvalModeControl.contains(event.target)) setApprovalMenu(false);
});
$("#cancelCompactButton").addEventListener("click", () => { ui.compactModal.hidden = true; });
ui.confirmCompact.addEventListener("click", compactContext);
ui.compactModal.addEventListener("click", (event) => {
  if (event.target === ui.compactModal && !compacting) ui.compactModal.hidden = true;
});
$("#closeDiffButton").addEventListener("click", () => { ui.diffModal.hidden = true; });
ui.diffModal.addEventListener("click", (event) => {
  if (event.target === ui.diffModal) ui.diffModal.hidden = true;
});
$("#closeFileButton").addEventListener("click", () => { ui.fileModal.hidden = true; });
ui.fileModal.addEventListener("click", (event) => {
  if (event.target === ui.fileModal) ui.fileModal.hidden = true;
});
ui.fileDiff.addEventListener("click", showFileDiff);
$("#cancelRenameButton").addEventListener("click", () => {
  ui.renameModal.hidden = true;
  pendingRenameSession = null;
});
$("#saveRenameButton").addEventListener("click", saveSessionRename);
ui.renameInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter") saveSessionRename();
});
$("#cancelConfirmButton").addEventListener("click", () => {
  ui.confirmModal.hidden = true;
  pendingConfirmAction = null;
});
$("#acceptConfirmButton").addEventListener("click", acceptConfirmation);
ui.approve.addEventListener("click", () => answerApproval(true));
ui.reject.addEventListener("click", () => answerApproval(false));
ui.stopApprovalTask.addEventListener("click", stopTaskFromApproval);
$("#sidebarToggle").addEventListener("click", () => openMobilePanel(ui.sidebar));
$("#inspectorToggle").addEventListener("click", () => openMobilePanel(ui.inspector));
$("#inspectorClose").addEventListener("click", closeMobilePanels);
ui.backdrop.addEventListener("click", closeMobilePanels);

$$('.tab').forEach((tab) => tab.addEventListener("click", () => switchTab(tab.dataset.tab)));
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  if (!ui.approvalModeMenu.hidden) {
    setApprovalMenu(false);
    ui.approvalModeTrigger.focus();
  } else if (!ui.approvalModal.hidden) {
    answerApproval(false);
  } else if (!ui.diffModal.hidden) {
    ui.diffModal.hidden = true;
  } else if (!ui.fileModal.hidden) {
    ui.fileModal.hidden = true;
  } else if (!ui.renameModal.hidden) {
    ui.renameModal.hidden = true;
    pendingRenameSession = null;
  } else if (!ui.compactModal.hidden && !compacting) {
    ui.compactModal.hidden = true;
  } else if (!ui.confirmModal.hidden) {
    ui.confirmModal.hidden = true;
    pendingConfirmAction = null;
  } else {
    closeMobilePanels();
  }
});

document.addEventListener("click", (event) => {
  if (!event.target.closest(".session-actions")) {
    $$(".session-menu.open").forEach((menu) => menu.classList.remove("open"));
  }
});

window.addEventListener("resize", () => {
  if (window.innerWidth > 1180) closeMobilePanels();
});

bootstrap();
