const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-button");
const newChatButton = document.querySelector("#new-chat");
const messagesElement = document.querySelector("#messages");
const emptyState = document.querySelector("#empty-state");
const requestState = document.querySelector("#request-state");
const micButton = document.querySelector("#mic-button");
const voiceStateElement = document.querySelector("#voice-state");
const speakToggle = document.querySelector("#speak-replies");
const speakToggleLabel = document.querySelector("#speak-toggle-label");
const handsFreeToggle = document.querySelector("#hands-free");
const handsFreeLabel = document.querySelector("#hands-free-label");
const voiceBar = document.querySelector("#voice-bar");
const voiceIndicator = document.querySelector("#voice-indicator");
const voiceIndicatorLabel = document.querySelector("#voice-indicator-label");
const stopSpeakingButton = document.querySelector("#stop-speaking");
const errorMessage = document.querySelector("#error-message");
const healthDot = document.querySelector("#health-dot");
const healthLabel = document.querySelector("#health-label");
const workspaceTabs = document.querySelectorAll(".workspace-tab");
const viewPanels = document.querySelectorAll(".view-panel");
const featureError = document.querySelector("#feature-error");
const taskForm = document.querySelector("#task-form");
const taskTitleInput = document.querySelector("#task-title");
const taskDescriptionInput = document.querySelector("#task-description");
const taskDueDateInput = document.querySelector("#task-due-date");
const taskDueTimeInput = document.querySelector("#task-due-time");
const taskList = document.querySelector("#tasks-list");
const tasksEmpty = document.querySelector("#tasks-empty");
const notesList = document.querySelector("#notes-list");
const notesEmpty = document.querySelector("#notes-empty");
const notePlaceholder = document.querySelector("#note-placeholder");
const noteForm = document.querySelector("#note-form");
const noteTitleInput = document.querySelector("#note-title");
const noteContentInput = document.querySelector("#note-content");
const newNoteButton = document.querySelector("#new-note");
const deleteNoteButton = document.querySelector("#delete-note");
const documentFileInput = document.querySelector("#document-file");
const uploadDocumentButton = document.querySelector("#upload-document");
const clearDocumentButton = document.querySelector("#clear-document");
const documentStatus = document.querySelector("#document-status");
const documentDetail = document.querySelector("#document-detail");
const toolActivity = document.querySelector("#tool-activity");
const confirmationPanel = document.querySelector("#confirmation-panel");
const confirmationSummary = document.querySelector("#confirmation-summary");
const confirmActionButton = document.querySelector("#confirm-action");
const cancelActionButton = document.querySelector("#cancel-action");
const chatView = document.querySelector("#chat-view");
const coreElement = document.querySelector("#core");
const coreStatus = document.querySelector("#core-status");
const alertList = document.querySelector("#alert-list");
const activityLog = document.querySelector("#activity-log");
const alertsCount = document.querySelector("#alerts-count");
const coreInstrument = document.querySelector("#core-instrument");
const coreLink = document.querySelector("#core-link");
const coreMode = document.querySelector("#core-mode");
const coreLast = document.querySelector("#core-last");
const telemetryCells = {
  cpu: document.querySelector("#telemetry-cpu"),
  memory: document.querySelector("#telemetry-memory"),
  gpu: document.querySelector("#telemetry-gpu"),
  thermal: document.querySelector("#telemetry-thermal"),
};
const systemTime = document.querySelector("#system-time");
const systemDate = document.querySelector("#system-date");

const markdownParser = window.markdownit({ html: false, linkify: false });
markdownParser.renderer.rules.link_open = (tokens, index, options, environment, renderer) => {
  const token = tokens[index];
  if (/^https?:\/\//i.test(token.attrGet("href") || "")) {
    token.attrSet("target", "_blank");
    token.attrSet("rel", "noopener noreferrer");
  }
  return renderer.renderToken(tokens, index, options);
};

const safeMarkdownTags = [
  "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4", "h5", "h6",
  "hr", "li", "ol", "p", "pre", "s", "strong", "table", "tbody", "td", "th", "thead",
  "tr", "ul",
];

let conversationId = null;
let isSending = false;
let currentNoteId = null;
let notesCache = [];
let originalNote = null;
let selectedDocument = null;
let documentUploadPending = false;
let selectedDocumentDetail = "";
let pendingConfirmationId = null;
let confirmationResolving = false;
let activityPollTimer = null;
const DEFAULT_VOICE_STATUS = {
  speech_input: false,
  speech_output: false,
  max_seconds: 60,
  end_silence_ms: 1200,
  min_recording_ms: 600,
  no_speech_timeout_ms: 6000,
};
let voiceStatus = { ...DEFAULT_VOICE_STATUS };
let recorder = null;
let recordingStream = null;
let recordingChunks = [];
let recordingTimer = null;
let micHeld = false;
let transcribing = false;
let replyAudio = null;
let replyRequest = 0;
// True while the current exchange was started hands-free; its replies, confirmation
// prompts, and errors are then spoken even if "Speak replies" is off.
let voiceTurn = false;
let sendingFromVoice = false;
let replyFinish = null; // resolves the current speakReply() promise
const SPEAK_BUSY_RETRIES = 12; // up to about 3 s for the previous synthesis to finish
const SPEAK_BUSY_RETRY_MS = 250;
let voiceLoop = window.VoiceLoop.createVoiceLoop();

// The hands-free loop (static/voice_loop.js) decides when to listen again; this reports
// events to it and carries out its actions. Events from the wrong state are ignored there.
function dispatchLoop(event) {
  const { loop, actions } = window.VoiceLoop.reduce(voiceLoop, event);
  voiceLoop = loop;
  for (const action of actions) {
    if (action.type === "start_listening") resumeListening();
    else if (action.type === "stop_listening" && (recorder || startPending)) stopRecording("loop_stopped");
    else if (action.type === "stop_playback") stopReplyAudio();
    else if (action.type === "notify") setVoiceState(action.message);
  }
  renderVoiceView();
}

// Shows the voice state near the microphone. The state comes from the loop (VoiceLoop.viewState)
// plus read-only page facts; rendering never changes the loop. The live region is only
// updated when the state changes, so screen readers are not flooded with repeats.
let renderedVoiceState = "idle";
function renderVoiceView() {
  const voiceAvailable = !micButton.hidden || !speakToggleLabel.hidden;
  const view = window.VoiceLoop.viewState(voiceLoop, {
    speaking: replyFinish !== null,
    recording: recorder !== null || startPending,
    transcribing,
    busy: isSending || confirmationResolving,
    error: !errorMessage.hidden,
  });
  const state = voiceAvailable ? view.state : "idle";
  if (state !== renderedVoiceState) {
    renderedVoiceState = state;
    voiceIndicator.dataset.state = state;
    voiceIndicatorLabel.textContent = window.VoiceLoop.VIEW_LABELS[state];
  }
  const hideStop = !(voiceAvailable && view.canStopSpeaking);
  if (hideStop && document.activeElement === stopSpeakingButton) {
    (micButton.hidden ? input : micButton).focus();
  }
  stopSpeakingButton.hidden = hideStop;
  updateMicLabel(!hideStop);
  voiceBar.classList.toggle("is-active", state !== "idle" || !hideStop);
  renderCore(view.state);
  setTelemetry(coreMode, handsFreeToggle.checked ? "Hands-free" : "Review", handsFreeToggle.checked ? "active" : "");
  renderAlerts();
}

// --- Command center: the core, telemetry, alerts and the activity log --------------
// All of these only display facts the page already has; none of them changes behaviour.

const CORE_LABELS = { idle: "Standing by", offline: "Offline" };
let assistantOnline = null; // null until the first health check
let renderedCoreState = "";
function renderCore(viewStateName) {
  // The core reflects the assistant as a whole, so a typed request still shows Thinking and
  // a typed request's Confirm/Cancel prompt shows Waiting for confirmation.
  let state = viewStateName;
  if (state === "idle" && !confirmationPanel.hidden) state = "awaiting_confirmation";
  else if (state === "idle" && assistantOnline === false) state = "offline";
  if (state === renderedCoreState) return;
  renderedCoreState = state;
  coreElement.dataset.state = state;
  coreInstrument.dataset.state = state;
  coreStatus.dataset.state = state;
  coreStatus.textContent = CORE_LABELS[state] || window.VoiceLoop.VIEW_LABELS[state] || state;
  if (state !== "idle" && state !== "offline") {
    const tone = state === "error" ? "danger" : state === "paused" || state === "awaiting_confirmation" ? "warning" : "";
    logActivity(coreStatus.textContent, tone);
  }
}

function setTelemetry(element, text, tone) {
  if (element.textContent !== text) element.textContent = text;
  if (element.dataset.tone !== tone) element.dataset.tone = tone;
}

let renderedAlerts = "";
function renderAlerts() {
  const alerts = [];
  if (assistantOnline === false) alerts.push(["danger", "Model", healthLabel.title || healthLabel.textContent]);
  if (!errorMessage.hidden && errorMessage.textContent) alerts.push(["danger", "Error", errorMessage.textContent]);
  if (!confirmationPanel.hidden) alerts.push(["warning", "Needs approval", confirmationSummary.textContent]);
  if (voiceLoop.notice) alerts.push([voiceLoop.notice.kind === "error" ? "danger" : "warning", "Hands-free", voiceLoop.notice.message]);
  const key = JSON.stringify(alerts);
  if (key === renderedAlerts) return;
  renderedAlerts = key;
  alertsCount.textContent = alerts.length ? String(alerts.length) : ""; // count beside the Alerts heading
  alertList.replaceChildren(...alerts.map(([kind, title, text]) => {
    const item = document.createElement("li");
    item.className = "alert-item";
    item.dataset.kind = kind;
    const label = document.createElement("span");
    label.className = "alert-kind";
    label.textContent = title;
    item.append(label, text);
    return item;
  }));
}

const ACTIVITY_LOG_LIMIT = 60;
let lastActivityText = "";
function logActivity(text, kind = "") {
  if (!text || text === lastActivityText) return;
  lastActivityText = text;
  const line = document.createElement("li");
  line.className = "activity-line";
  if (kind) line.dataset.kind = kind;
  const time = document.createElement("span");
  time.className = "activity-time";
  time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  coreLast.textContent = time.textContent;
  const body = document.createElement("span");
  body.className = "activity-text";
  body.textContent = text;
  line.append(time, body);
  activityLog.append(line);
  while (activityLog.childElementCount > ACTIVITY_LOG_LIMIT) activityLog.firstElementChild.remove();
  activityLog.scrollTop = activityLog.scrollHeight;
}

// Local time and date in the System panel (display only), e.g. 17:46 and 16 OCT 2026.
function updateClock() {
  const now = new Date();
  systemTime.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  systemTime.dateTime = now.toISOString();
  const month = now.toLocaleDateString("en-GB", { month: "short" }).toUpperCase();
  systemDate.textContent = `${String(now.getDate()).padStart(2, "0")} ${month} ${now.getFullYear()}`;
}
updateClock();
window.setInterval(updateClock, 1000); // checked every second so the minute changes on time

// Live System telemetry (GET /api/system/telemetry), every few seconds while the page is
// visible. One request at a time; a failed request shows every metric as Unavailable.
const TELEMETRY_POLL_MS = 3000;
let telemetryTimer = null;
let telemetryInFlight = false;

function renderTelemetry(metrics) {
  const views = window.TelemetryView.formatAll(metrics);
  for (const [name, cell] of Object.entries(telemetryCells)) {
    setTelemetry(cell, views[name].text, views[name].tone);
    cell.title = views[name].title || "";
  }
}

function scheduleTelemetry() {
  window.clearTimeout(telemetryTimer);
  telemetryTimer = null;
  if (document.visibilityState !== "hidden") {
    telemetryTimer = window.setTimeout(refreshTelemetry, TELEMETRY_POLL_MS);
  }
}

async function refreshTelemetry() {
  if (telemetryInFlight || document.visibilityState === "hidden") return;
  telemetryInFlight = true;
  try {
    const response = await fetch("/api/system/telemetry", { cache: "no-store" });
    const result = await response.json();
    renderTelemetry(response.ok ? result.metrics : null);
  } catch {
    renderTelemetry(null);
  } finally {
    telemetryInFlight = false;
    scheduleTelemetry();
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    window.clearTimeout(telemetryTimer);
    telemetryTimer = null;
  } else {
    refreshTelemetry();
  }
});
refreshTelemetry();

// The confirmation panel is shown and hidden from several places; keep the core and alerts in step.
new MutationObserver(renderVoiceView).observe(confirmationPanel, { attributes: true, attributeFilter: ["hidden"] });

function showChatView() {
  if (chatView.hidden) switchView("chat");
}

// Switchable side panels ([data-tabs]): a standard tab list. Arrow keys, Home and End move
// between tabs; only the selected tab is in the Tab order. Panels keep their content when hidden.
function setupPanelTabs(group) {
  const tabs = [...group.querySelectorAll('[role="tab"]')];
  const select = (tab, focus = false) => {
    for (const other of tabs) {
      const selected = other === tab;
      other.setAttribute("aria-selected", String(selected));
      other.tabIndex = selected ? 0 : -1;
      document.getElementById(other.getAttribute("aria-controls")).hidden = !selected;
    }
    if (focus) tab.focus();
  };
  for (const tab of tabs) {
    tab.addEventListener("click", () => select(tab));
    tab.addEventListener("keydown", (event) => {
      const index = tabs.indexOf(tab);
      const next = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: tabs.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      select(tabs[(next + tabs.length) % tabs.length], true);
    });
  }
}
document.querySelectorAll("[data-tabs]").forEach(setupPanelTabs);

// Called only by the loop, after a completed reply, never on page load.
async function resumeListening() {
  // A reply is reported from inside its request handler; wait until that handler has
  // finished (cleared isSending etc.) before checking whether listening can start.
  await new Promise((resolve) => window.setTimeout(resolve, 0));
  const blocked =
    !handsFreeToggle.checked || document.visibilityState === "hidden" || recorder || startPending ||
    transcribing || isSending || confirmationResolving || pendingConfirmationId !== null ||
    documentUploadPending;
  const result = blocked ? "failed" : await startRecording(true);
  if (result === "failed") dispatchLoop({ type: "listen_failed" });
  else if (result === "cancelled") dispatchLoop({ type: "stopped_by_user" });
}

// Reports a finished chat or confirmation request, and its playback, to the loop.
function reportTurn(turn, outcome, speech) {
  dispatchLoop({ type: "turn_finished", turn, outcome, spoken: Boolean(speech) });
  // A cancelled playback (null) counts as finished: it was stopped on purpose.
  speech?.then((ok) => dispatchLoop({ type: "playback_finished", turn, ok: ok !== false }));
}

function showError(message) {
  errorMessage.textContent = message;
  errorMessage.hidden = false;
  logActivity(message, "danger");
  renderVoiceView();
}

function clearError() {
  errorMessage.textContent = "";
  errorMessage.hidden = true;
  renderVoiceView();
}

function appendMessage(role, content) {
  const message = document.createElement("article");
  message.className = `message ${role}`;

  const label = document.createElement("span");
  label.className = "message-role";
  label.textContent = role === "user" ? "YOU" : "KRIS";

  const text = document.createElement("div");
  text.className = "message-content";
  if (role === "assistant") {
    appendAssistantMarkdown(text, content);
  } else {
    text.textContent = content;
  }

  message.append(label, text);
  messagesElement.append(message);
  emptyState.hidden = true;
  message.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return text;
}

function appendAssistantMarkdown(target, content) {
  if (typeof content !== "string" || !content.trim()) {
    target.textContent = "The assistant returned an empty response.";
    return;
  }

  try {
    const parsed = markdownParser.render(content);
    const safeFragment = window.DOMPurify.sanitize(parsed, {
      RETURN_DOM_FRAGMENT: true,
      ALLOWED_TAGS: safeMarkdownTags,
      ALLOWED_ATTR: ["class", "href", "rel", "start", "target", "title"],
    });
    target.append(safeFragment);
  } catch {
    target.textContent = content;
  }
}

function setBusy(busy) {
  isSending = busy;
  const controlsBusy =
    busy || documentUploadPending || confirmationResolving || pendingConfirmationId !== null || transcribing;
  // A pending confirmation does not block typing: sending a new message cancels it.
  const composerBusy = busy || documentUploadPending || confirmationResolving || transcribing;
  sendButton.disabled = composerBusy;
  micButton.disabled = composerBusy && recorder === null;
  newChatButton.disabled = controlsBusy;
  input.disabled = composerBusy;
  // Not while transcribing either, so a confirmation can't start under a pending transcript.
  confirmActionButton.disabled = busy || confirmationResolving || transcribing;
  cancelActionButton.disabled = busy || confirmationResolving || transcribing;
  documentFileInput.disabled = controlsBusy;
  uploadDocumentButton.disabled = controlsBusy || documentFileInput.files.length === 0;
  clearDocumentButton.disabled = controlsBusy || selectedDocument === null;
  requestState.hidden = !busy;
  renderVoiceView();
}

async function refreshHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    const result = await response.json();
    healthDot.classList.toggle("connected", result.connected === true);
    healthDot.classList.toggle("disconnected", result.connected !== true);
    healthLabel.textContent = result.connected ? `${result.model} ready` : "Assistant unavailable";
    healthLabel.title = result.error || "Ollama and the configured model are ready.";
    setHealthTelemetry(result.connected === true, result.connected ? result.model : "Unavailable");
  } catch {
    healthDot.classList.remove("connected");
    healthDot.classList.add("disconnected");
    healthLabel.textContent = "Backend unavailable";
    healthLabel.title = "Could not reach the local assistant backend.";
    setHealthTelemetry(false, "Backend unavailable");
  }
}

function setHealthTelemetry(online, text) {
  if (online !== assistantOnline) {
    logActivity(online ? `Model ready: ${text}` : healthLabel.title, online ? "" : "danger");
  }
  assistantOnline = online;
  setTelemetry(coreLink, online ? "Online" : "Offline", online ? "ok" : "danger");
  renderVoiceView();
}

async function startConversation(previousId = null) {
  const response = await fetch("/api/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ previous_id: previousId }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Could not start a new conversation.");

  conversationId = result.conversation_id;
  messagesElement.replaceChildren();
  emptyState.hidden = false;
  setSelectedDocument(null);
  pendingConfirmationId = null;
  confirmationPanel.hidden = true;
  clearToolActivity();
  clearError();
  input.focus();
}

function setSelectedDocument(documentInfo) {
  selectedDocument = documentInfo;
  if (!documentInfo) {
    selectedDocumentDetail = "";
    documentStatus.textContent = "No document selected";
    documentDetail.textContent = "";
    documentDetail.hidden = true;
    clearDocumentButton.disabled = true;
    return;
  }

  documentStatus.textContent = `${documentInfo.filename} selected`;
  const details = [
    `${documentInfo.file_type} · ${documentInfo.page_count} ${documentInfo.page_count === 1 ? "page" : "pages"}`,
    `${documentInfo.extracted_chars.toLocaleString()} characters extracted`,
  ];
  if (documentInfo.truncated) {
    details.push("extraction limit reached; remainder unavailable");
  } else if (documentInfo.extracted_chars > documentInfo.context_budget) {
    details.push("relevant excerpts used per question, not the full document");
  } else {
    details.push("full extracted text fits the context budget");
  }
  if (documentInfo.little_text) {
    details.push("little selectable text; scanned PDFs are not OCR'd");
  }
  selectedDocumentDetail = details.join(" · ");
  documentDetail.textContent = selectedDocumentDetail;
  documentDetail.hidden = false;
  clearDocumentButton.disabled = false;
}

documentFileInput.addEventListener("change", () => {
  uploadDocumentButton.disabled = documentUploadPending || documentFileInput.files.length === 0;
});

uploadDocumentButton.addEventListener("click", async () => {
  const file = documentFileInput.files[0];
  if (!file || !conversationId || documentUploadPending || isSending) return;

  documentUploadPending = true;
  setBusy(false);
  documentStatus.textContent = "Reading document locally...";
  clearError();
  const formData = new FormData();
  formData.append("conversation_id", conversationId);
  formData.append("file", file);
  try {
    const result = await apiRequest("/api/documents", { method: "POST", body: formData });
    if (result.conversation_reset) {
      messagesElement.replaceChildren();
      emptyState.hidden = false;
    }
    setSelectedDocument(result.document);
    if (result.conversation_reset) {
      documentDetail.textContent += " · chat history reset to keep sources separate";
    }
    documentFileInput.value = "";
  } catch (error) {
    documentStatus.textContent = selectedDocument
      ? `${selectedDocument.filename} selected`
      : "No document selected";
    showError(error.message || "The document could not be read.");
  } finally {
    documentUploadPending = false;
    setBusy(false);
  }
});

clearDocumentButton.addEventListener("click", async () => {
  if (!conversationId || !selectedDocument) return;
  clearDocumentButton.disabled = true;
  try {
    await apiRequest(`/api/conversations/${conversationId}/document`, { method: "DELETE" });
    setSelectedDocument(null);
    documentFileInput.value = "";
    clearError();
  } catch (error) {
    clearDocumentButton.disabled = false;
    showError(error.message || "The selected document could not be cleared.");
  }
});

async function apiRequest(url, options = {}) {
  const response = await fetch(url, options);
  let result = null;
  if (response.status !== 204) {
    try {
      result = await response.json();
    } catch {
      throw new Error("The local backend returned an invalid response.");
    }
  }
  if (!response.ok) {
    throw new Error(result?.error || "The local request could not be completed.");
  }
  return result;
}

function clearToolActivity() {
  toolActivity.replaceChildren();
  toolActivity.hidden = true;
}

function toolLabel(toolName) {
  const labels = {
    create_task: "Creating task",
    list_tasks: "Checking tasks",
    set_task_completed: "Updating task",
    set_task_due_date: "Updating due date",
    delete_task: "Deleting task",
    list_notes: "Checking notes",
    get_note: "Reading note",
    create_note: "Creating note",
    update_note: "Updating note",
    delete_note: "Deleting note",
    get_system_info: "Checking system information",
    open_website: "Requesting browser launch",
    find_files: "Searching approved folders",
    open_file: "Requesting file open",
    open_application: "Requesting app launch",
    document_context: "Using document context",
  };
  return labels[toolName] || "Using an assistant tool";
}

function showLiveActivity(activity) {
  if (!activity || activity.status === "idle") {
    clearToolActivity();
    return;
  }
  toolActivity.replaceChildren();
  const label = document.createElement("span");
  // The runtime's "thinking" summary still names the assistant "U"; the UI says K.R.I.S.
  label.textContent = activity.status === "running"
    ? `${toolLabel(activity.tool)}...`
    : activity.status === "thinking" ? "K.R.I.S. is thinking..." : activity.summary || "Assistant action";
  toolActivity.append(label);
  if (activity.status !== "thinking") logActivity(label.textContent, "tool"); // the core already logs Thinking
  toolActivity.hidden = false;
}

async function pollConversationActivity() {
  if (!conversationId) return;
  try {
    const result = await apiRequest(`/api/conversations/${conversationId}/activity`);
    showLiveActivity(result.activity);
  } catch {
    // Activity feedback is best-effort; the chat response remains authoritative.
  }
}

function startActivityPolling() {
  stopActivityPolling();
  pollConversationActivity();
  activityPollTimer = window.setInterval(pollConversationActivity, 350);
}

function stopActivityPolling() {
  if (activityPollTimer !== null) {
    window.clearInterval(activityPollTimer);
    activityPollTimer = null;
  }
}

function renderToolActivities(activities) {
  if (!Array.isArray(activities) || activities.length === 0) {
    clearToolActivity();
    return;
  }
  toolActivity.replaceChildren();
  for (const activity of activities) {
    const line = document.createElement("div");
    const result = activity.status === "success" ? "Done" : activity.status;
    line.textContent = `${toolLabel(activity.tool)}: ${activity.summary} (${result})`;
    toolActivity.append(line);
    logActivity(line.textContent, activity.status === "success" ? "tool" : "warning");
  }
  toolActivity.hidden = false;
}

function showPendingConfirmation(pending) {
  pendingConfirmationId = pending.confirmation_id;
  confirmationSummary.textContent = `${pending.summary} ${pending.message}`;
  confirmationPanel.hidden = false;
  showChatView(); // the Confirm and Cancel buttons must be visible
  messagesElement.lastElementChild?.scrollIntoView({ block: "nearest" }); // the panel pushes messages down
}

function speechWanted() {
  return voiceStatus.speech_output && (speakToggle.checked || voiceTurn);
}

function acceptTurnResult(result) {
  renderToolActivities(result.activities);
  const spoken = [];
  if (result.notice) {
    appendMessage("assistant", result.notice);
    spoken.push(result.notice);
  }
  if (result.pending_confirmation) {
    showPendingConfirmation(result.pending_confirmation);
    const voiceHint = voiceStatus.speech_input ? ", or say no to cancel" : "";
    spoken.push(
      `Waiting for your confirmation: ${result.pending_confirmation.summary}. ` +
      `Press Confirm to approve it, or Cancel${voiceHint}.`,
    );
  } else {
    pendingConfirmationId = null;
    confirmationPanel.hidden = true;
    if (result.response) {
      const content = appendMessage("assistant", result.response);
      spoken.push(content.textContent);
    }
  }
  const speech = speechWanted() && spoken.length ? speakReply(spoken.join(" ")) : null;
  return { outcome: result.pending_confirmation ? "pending" : "response", speech };
}

function reportVoiceError(message) {
  // Errors are spoken only during a hands-free exchange, never for typed messages.
  showError(message);
  if (voiceTurn && voiceStatus.speech_output) speakReply(message);
}

// --- Voice: push-to-talk transcription for review, optional spoken replies -----------

function setVoiceState(message = "") {
  voiceStateElement.textContent = message;
}

async function loadVoiceStatus() {
  try {
    voiceStatus = { ...DEFAULT_VOICE_STATUS, ...(await apiRequest("/api/voice/status")) };
  } catch {
    voiceStatus = { ...DEFAULT_VOICE_STATUS };
  }
  const canRecord = Boolean(navigator.mediaDevices?.getUserMedia) && typeof MediaRecorder !== "undefined";
  micButton.hidden = !(voiceStatus.speech_input && canRecord);
  speakToggleLabel.hidden = !voiceStatus.speech_output;
  handsFreeLabel.hidden = micButton.hidden;
  renderVoiceView();
}

function preferredRecordingType() {
  const candidates = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

// Recording lifecycle. Review mode: hold to talk, release to stop (unchanged). Hands-free:
// a tap starts listening and end-of-speech detection (static/voice_activity.js) stops it
// after a pause following speech; tapping again stops at once; press-and-hold still stops on
// release. Every exit path goes through stopRecording() (first reason wins) and
// teardownRecording(), which stops all tracks, the level monitor, and timers.
const HOLD_TO_TALK_MS = 400;
const LEVEL_POLL_MS = 50;
const DISCARD_MESSAGES = {
  loop_stopped: "Stopped listening; nothing was sent.",
  no_speech: "Didn't hear any speech, so nothing was sent.",
  page_hidden: "Stopped listening because the page was hidden; nothing was sent.",
  track_ended: "The microphone stopped, so nothing was sent.",
};
let listeningSession = 0; // bumped on start and teardown, so late callbacks are ignored
let startPending = false; // waiting for the microphone stream
let cancelPendingStart = false;
let autoStopRecording = false;
let stopReason = null;
let pressStartedAt = 0;
let holdTimer = null;
let holdActive = false; // a hands-free press held past HOLD_TO_TALK_MS: manual control only
let levelMonitor = null;
let audioContext = null;
let recordingTrack = null;

function ensureAudioContext() {
  // Created or resumed during the tap itself, because browsers only allow audio
  // processing to start from a user gesture.
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return;
  if (!audioContext) {
    try {
      audioContext = new AudioContextClass();
    } catch {
      return;
    }
  }
  if (audioContext.state === "suspended") audioContext.resume().catch(() => {});
}

function onTrackEnded() {
  stopRecording("track_ended");
}

function releaseMicrophone() {
  recordingTrack?.removeEventListener("ended", onTrackEnded);
  recordingTrack = null;
  recordingStream?.getTracks().forEach((track) => track.stop());
  recordingStream = null;
}

function stopLevelMonitor() {
  levelMonitor?.stop();
  levelMonitor = null;
}

function teardownRecording() {
  listeningSession += 1;
  window.clearTimeout(recordingTimer);
  recordingTimer = null;
  stopLevelMonitor();
  releaseMicrophone();
  recorder = null;
  recordingChunks = [];
  micHeld = false;
  autoStopRecording = false;
  holdActive = false;
  window.clearTimeout(holdTimer);
  holdTimer = null;
  micButton.classList.remove("recording");
  micButton.setAttribute("aria-pressed", "false");
  renderVoiceView();
}

// Measures the same MediaStream the MediaRecorder uploads. Returns null when the audio
// context is not running (for example still suspended), so the caller falls back to manual stop.
function startLevelMonitor(stream, session) {
  if (!audioContext || audioContext.state !== "running" || !window.VoiceActivity) return null;
  let source;
  let analyser;
  try {
    source = audioContext.createMediaStreamSource(stream);
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 2048;
    source.connect(analyser);
  } catch {
    return null;
  }
  const detector = window.VoiceActivity.createEndOfSpeechDetector({
    endSilenceMs: voiceStatus.end_silence_ms,
    minRecordingMs: voiceStatus.min_recording_ms,
    noSpeechTimeoutMs: voiceStatus.no_speech_timeout_ms,
    maxMs: voiceStatus.max_seconds * 1000,
  });
  const samples = new Float32Array(analyser.fftSize);
  const startedAt = performance.now();
  const timer = window.setInterval(() => {
    if (session !== listeningSession) return;
    analyser.getFloatTimeDomainData(samples);
    const decision = detector.update(window.VoiceActivity.rmsDb(samples), performance.now() - startedAt);
    if (decision) stopRecording(decision.reason);
  }, LEVEL_POLL_MS);
  return {
    stop() {
      window.clearInterval(timer);
      try {
        source.disconnect();
        analyser.disconnect();
      } catch {
        // already disconnected
      }
    },
  };
}

// Resolves "started", "cancelled" (stopped by the user or page while opening), or "failed".
async function startRecording(autoStop) {
  if (recorder || startPending || transcribing || micButton.disabled) return "failed";
  clearError();
  stopReplyAudio();
  const session = ++listeningSession;
  startPending = true;
  renderVoiceView();
  cancelPendingStart = false;
  autoStopRecording = autoStop;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    startPending = false;
    autoStopRecording = false;
    setVoiceState("");
    showError("Microphone access was denied or is unavailable.");
    return "failed";
  }
  startPending = false;
  // Cancelled while the microphone was opening: a review-mode release, a second tap, the
  // page being hidden, or a newer recording.
  if (session !== listeningSession || cancelPendingStart || document.visibilityState === "hidden") {
    stream.getTracks().forEach((track) => track.stop());
    cancelPendingStart = false;
    if (session === listeningSession) autoStopRecording = false;
    return "cancelled";
  }
  recordingStream = stream;
  const mimeType = preferredRecordingType();
  try {
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  } catch {
    teardownRecording();
    showError("This browser cannot record audio in a supported format.");
    return "failed";
  }
  recordingTrack = stream.getAudioTracks()[0] || null;
  recordingTrack?.addEventListener("ended", onTrackEnded);
  stopReason = null;
  recordingChunks = [];
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data.size > 0) recordingChunks.push(event.data);
  });
  recorder.addEventListener("stop", finishRecording, { once: true });
  recorder.start();
  renderVoiceView();
  micButton.classList.add("recording");
  micButton.setAttribute("aria-pressed", "true");
  recordingTimer = window.setTimeout(() => stopRecording("max_duration"), voiceStatus.max_seconds * 1000);
  if (autoStop && holdActive) {
    setVoiceState("Listening… release to stop.");
  } else if (autoStop) {
    levelMonitor = startLevelMonitor(stream, session);
    setVoiceState(
      levelMonitor
        ? "Listening… stops when you pause (tap the mic to stop now)."
        : "Listening… automatic stop is unavailable here, so tap the mic to stop.",
    );
  } else {
    setVoiceState(`Listening… release to stop (up to ${voiceStatus.max_seconds}s)`);
  }
  return "started";
}

function stopRecording(reason = "manual") {
  micHeld = false;
  if (startPending) {
    cancelPendingStart = true;
    return;
  }
  if (!recorder || recorder.state === "inactive" || stopReason) return;
  stopReason = reason;
  window.clearTimeout(recordingTimer);
  recordingTimer = null;
  stopLevelMonitor();
  try {
    recorder.stop();
  } catch {
    finishRecording();
  }
}

async function finishRecording() {
  const reason = stopReason || "manual";
  stopReason = null;
  const type = recorder?.mimeType || recordingChunks[0]?.type || "";
  const recording = new Blob(recordingChunks, { type });
  const handsFree = handsFreeToggle.checked;
  teardownRecording();
  if (DISCARD_MESSAGES[reason]) {
    setBusy(isSending);
    if (reason === "track_ended") {
      showError(DISCARD_MESSAGES[reason]);
    } else {
      setVoiceState(DISCARD_MESSAGES[reason]);
      // While the loop runs, it decides what happens next (listen again, or pause).
      if (reason === "no_speech" && handsFree && voiceLoop.state === "off") {
        voiceTurn = true;
        if (speechWanted()) speakReply(DISCARD_MESSAGES[reason]);
      }
    }
    dispatchLoop({ type: "recording_discarded", reason }); // last: its message wins
    return;
  }
  if (recording.size === 0) {
    setVoiceState("");
    showError("Nothing was recorded. Hold the button while you speak.");
    setBusy(isSending);
    return;
  }

  transcribing = true;
  setBusy(isSending);
  setVoiceState("Transcribing locally…");
  dispatchLoop({ type: "recording_uploaded" });
  let transcript = null;
  try {
    const formData = new FormData();
    formData.append("audio", recording, "recording");
    transcript = await apiRequest("/api/voice/transcribe", { method: "POST", body: formData });
  } catch (error) {
    setVoiceState("");
    voiceTurn = handsFree;
    reportVoiceError(error.message || "The recording could not be transcribed.");
    dispatchLoop({ type: "transcription_failed" });
  } finally {
    transcribing = false;
    setBusy(isSending);
    input.focus();
  }
  if (transcript) handleTranscript(transcript.text, transcript.reply_kind, handsFree);
}

function handleTranscript(text, replyKind, handsFree) {
  if ((pendingConfirmationId && replyKind === "affirm") || (confirmationResolving && replyKind === "decline")) {
    // Voice never approves an action, and sending "yes" as a message would cancel it.
    voiceTurn = handsFree;
    const message = confirmationResolving
      ? "The confirmation is already being handled."
      : "Voice can't approve actions. Press Confirm to approve it, or say no to cancel.";
    dispatchLoop({ type: "transcript_handled", outcome: "guarded" });
    setVoiceState(message);
    if (speechWanted()) speakReply(message);
    return;
  }
  if (pendingConfirmationId && replyKind === "decline") {
    voiceTurn = handsFree;
    setVoiceState("");
    dispatchLoop({ type: "transcript_handled", outcome: "cancel_requested" });
    resolvePendingAction(false);
    return;
  }
  const existing = input.value.trim();
  const busy = isSending || confirmationResolving || documentUploadPending || !conversationId;
  if (handsFree && !existing && !busy) {
    input.value = text;
    sendingFromVoice = true;
    dispatchLoop({ type: "transcript_handled", outcome: "submitted" });
    form.requestSubmit();
    return;
  }
  if (handsFree) {
    // Never strand an utterance: keep it in the box for the user, and stop the loop.
    input.value = existing ? `${existing} ${text}` : text;
    input.dispatchEvent(new Event("input"));
    dispatchLoop({ type: "transcript_handled", outcome: "kept_for_review" });
    return;
  }
  input.value = existing ? `${existing} ${text}` : text;
  input.dispatchEvent(new Event("input"));
  setVoiceState(
    handsFree
      ? "Added to the text you were typing; review it, then press Enter to send."
      : "Review the transcript, then press Enter to send.",
  );
}

function stopReplyAudio() {
  replyRequest += 1;
  if (replyAudio) {
    replyAudio.pause();
    URL.revokeObjectURL(replyAudio.src);
    replyAudio = null;
  }
  const finish = replyFinish;
  replyFinish = null;
  finish?.(null); // cancelled or replaced
  renderVoiceView();
}

// Speaks text. The promise resolves true when playback ends, false if it fails, and null if
// it is stopped or replaced first; only the newest request ever plays.
function speakReply(text) {
  stopReplyAudio();
  const request = replyRequest;
  const done = new Promise((resolve) => {
    replyFinish = resolve;
  });
  renderVoiceView();
  const settle = (result) => {
    if (request !== replyRequest) return;
    const finish = replyFinish;
    replyFinish = null;
    stopReplyAudio();
    finish?.(result);
  };
  (async () => {
    try {
      // The server synthesizes one reply at a time. A reply that follows a stopped or
      // replaced one within a second or two can get 429 while that synthesis finishes, so
      // retry briefly, only while this is still the current request.
      let response;
      for (let attempt = 0; ; attempt += 1) {
        response = await fetch("/api/voice/speak", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        if (response.status !== 429 || attempt >= SPEAK_BUSY_RETRIES || request !== replyRequest) break;
        await new Promise((resolve) => window.setTimeout(resolve, SPEAK_BUSY_RETRY_MS));
      }
      if (request !== replyRequest) return; // stopped or replaced meanwhile (already resolved)
      if (!response.ok) {
        const result = await response.json().catch(() => null);
        throw new Error(result?.error || "Local speech output failed.");
      }
      const audio = await response.blob();
      if (request !== replyRequest) return;
      replyAudio = new Audio(URL.createObjectURL(audio));
      replyAudio.addEventListener("ended", () => settle(true), { once: true });
      if (response.headers.get("X-Speech-Truncated") === "1") {
        setVoiceState("Speaking the beginning of a long reply.");
      }
      await replyAudio.play();
    } catch (error) {
      if (request === replyRequest) {
        setVoiceState(`Spoken reply unavailable: ${error.message}`);
        settle(false);
      }
    }
  })();
  return done;
}

function micPressStart() {
  const handsFree = handsFreeToggle.checked;
  if (handsFree && (recorder || startPending)) {
    stopRecording("manual"); // a second tap stops immediately
    return;
  }
  pressStartedAt = performance.now();
  micHeld = true;
  if (handsFree) {
    dispatchLoop({ type: "start" }); // only a user tap ever starts the hands-free loop
    ensureAudioContext();
    // Holding the button means manual control: automatic stopping is switched off.
    holdTimer = window.setTimeout(() => {
      if (!micHeld || !autoStopRecording) return;
      holdActive = true;
      stopLevelMonitor();
      if (recorder) setVoiceState("Listening… release to stop.");
    }, HOLD_TO_TALK_MS);
  }
  startRecording(handsFree).then((result) => {
    if (!handsFree) return;
    if (result === "failed") dispatchLoop({ type: "listen_failed" });
    else if (result === "cancelled") dispatchLoop({ type: "stopped_by_user" });
  });
}

function micPressEnd() {
  if (!micHeld) return; // e.g. the release of a second tap, which already stopped
  micHeld = false;
  window.clearTimeout(holdTimer);
  holdTimer = null;
  // Review mode always stops on release; hands-free stops on release only after a hold.
  if (!autoStopRecording || holdActive || performance.now() - pressStartedAt >= HOLD_TO_TALK_MS) {
    stopRecording("manual");
  }
}

micButton.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  event.preventDefault();
  try {
    micButton.setPointerCapture(event.pointerId);
  } catch {
    // Without capture, releasing over the button still stops recording.
  }
  micPressStart();
});
for (const eventName of ["pointerup", "pointercancel"]) {
  micButton.addEventListener(eventName, micPressEnd);
}
micButton.addEventListener("keydown", (event) => {
  if ((event.key === " " || event.key === "Enter") && !event.repeat) {
    event.preventDefault();
    micPressStart();
  }
});
micButton.addEventListener("keyup", (event) => {
  if (event.key === " " || event.key === "Enter") micPressEnd();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "hidden") return;
  if (recorder || startPending) stopRecording("page_hidden");
  dispatchLoop({ type: "page_hidden" });
});
window.addEventListener("pagehide", () => {
  if (recorder || startPending) stopRecording("page_hidden");
  dispatchLoop({ type: "page_hidden" });
  teardownRecording();
});
speakToggle.addEventListener("change", () => {
  if (!speakToggle.checked) {
    stopReplyAudio();
    setVoiceState("");
  }
});
// Stop Speaking goes through the loop, which always stops playback and, only for a
// hands-free reply, resumes listening exactly once. In Review mode it just stops playback.
stopSpeakingButton.addEventListener("click", () => {
  if (stopSpeakingButton.hidden || replyFinish === null) return; // inert when nothing is speaking
  dispatchLoop({ type: "stop_speaking" });
  renderVoiceView();
});

// The mic's accessible name follows the mode. While a hands-free reply is playing, tapping it
// (or pressing Space/Enter on it) interrupts the reply and starts listening (barge-in).
function updateMicLabel(speaking = false) {
  const handsFree = handsFreeToggle.checked;
  const label = handsFree ? (speaking ? "Interrupt and talk" : "Tap to talk") : "Hold to talk";
  if (micButton.getAttribute("aria-label") === label) return;
  micButton.setAttribute("aria-label", label);
  micButton.title = !handsFree
    ? "Hold to talk; release to transcribe"
    : speaking
      ? "Interrupt the reply and talk; it stops when you pause"
      : "Tap to talk; it stops when you pause (tap again to stop now)";
}

handsFreeToggle.addEventListener("change", () => {
  updateMicLabel();
  if (!handsFreeToggle.checked) {
    dispatchLoop({ type: "disable" });
    voiceTurn = false;
    stopReplyAudio();
  }
  setVoiceState(
    handsFreeToggle.checked ? "Hands-free: tap the mic and speak; it stops when you pause and sends." : "",
  );
  renderVoiceView(); // display only: the core's MODE readout
});

async function resolvePendingAction(approved) {
  if (!conversationId || !pendingConfirmationId || confirmationResolving) return;
  confirmationResolving = true;
  confirmActionButton.disabled = true;
  cancelActionButton.disabled = true;
  dispatchLoop({ type: "confirmation_resolving" });
  const turn = voiceLoop.turn;
  setBusy(true);
  startActivityPolling();
  try {
    const result = await apiRequest("/api/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        conversation_id: conversationId,
        confirmation_id: pendingConfirmationId,
        approved,
      }),
    });
    const { outcome, speech } = acceptTurnResult(result);
    reportTurn(turn, outcome, speech);
  } catch (error) {
    reportVoiceError(error.message || "The pending action could not be resolved.");
    dispatchLoop({ type: "turn_finished", turn, outcome: "error" });
  } finally {
    confirmationResolving = false;
    confirmActionButton.disabled = false;
    cancelActionButton.disabled = false;
    stopActivityPolling();
    setBusy(false);
    input.focus();
  }
}

confirmActionButton.addEventListener("click", () => resolvePendingAction(true));
cancelActionButton.addEventListener("click", () => resolvePendingAction(false));

function showFeatureError(message = "") {
  featureError.textContent = message;
  featureError.hidden = !message;
}

function switchView(viewName) {
  for (const tab of workspaceTabs) {
    const selected = tab.dataset.view === viewName;
    tab.classList.toggle("active", selected);
    tab.setAttribute("aria-pressed", String(selected));
  }
  for (const panel of viewPanels) {
    panel.hidden = panel.id !== `${viewName}-view`;
  }
  newChatButton.hidden = viewName !== "chat";
  showFeatureError();

  if (viewName === "tasks") loadTasks();
  if (viewName === "notes") loadNotes();
}

// due_at is "YYYY-MM-DD" (a calendar date) or "YYYY-MM-DD HH:MM:SS[.ffffff]Z" (UTC).
function buildDueAt(dateValue, timeValue) {
  if (!dateValue) {
    if (timeValue) throw new Error("Choose a due date to go with the time.");
    return null;
  }
  if (!timeValue) return dateValue;
  const local = new Date(`${dateValue}T${timeValue}`);
  if (Number.isNaN(local.getTime())) throw new Error("Enter a valid due date and time.");
  return `${local.toISOString().slice(0, 19).replace("T", " ")}Z`;
}

function parseDueAt(dueAt) {
  if (dueAt.length === 10) {
    const [year, month, day] = dueAt.split("-").map(Number);
    return { dateOnly: true, date: new Date(year, month - 1, day) };
  }
  const iso = dueAt.replace(" ", "T").replace(/(\.\d{3})\d*Z$/, "$1Z");
  return { dateOnly: false, date: new Date(iso) };
}

function dueInputValues(dueAt) {
  if (!dueAt) return { date: "", time: "" };
  const { dateOnly, date } = parseDueAt(dueAt);
  if (dateOnly) return { date: dueAt, time: "" };
  const pad = (value) => String(value).padStart(2, "0");
  return {
    date: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    time: `${pad(date.getHours())}:${pad(date.getMinutes())}`,
  };
}

function describeDue(task) {
  const { dateOnly, date } = parseDueAt(task.due_at);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const overdue = !task.completed && (dateOnly ? date < today : date < now);
  const label = dateOnly
    ? date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" })
    : date.toLocaleString(undefined, {
      weekday: "short", month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit",
    });
  return { overdue, text: `${overdue ? "Overdue" : "Due"} ${label}` };
}

async function updateTaskDueDate(taskId, dueAt) {
  await apiRequest(`/api/tasks/${taskId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ due_at: dueAt }),
  });
  showFeatureError();
  await loadTasks();
}

function buildDueEditor(task) {
  const editor = document.createElement("div");
  editor.className = "task-due-editor";
  editor.hidden = true;

  const initial = dueInputValues(task.due_at);
  const dateInput = document.createElement("input");
  dateInput.type = "date";
  dateInput.value = initial.date;
  dateInput.setAttribute("aria-label", `Due date for ${task.title}`);
  const timeInput = document.createElement("input");
  timeInput.type = "time";
  timeInput.value = initial.time;
  timeInput.setAttribute("aria-label", `Due time for ${task.title} (optional)`);

  const save = document.createElement("button");
  save.className = "secondary-action";
  save.type = "button";
  save.textContent = "Save";
  save.addEventListener("click", async () => {
    try {
      const dueAt = buildDueAt(dateInput.value, timeInput.value);
      if (dueAt === null) throw new Error("Choose a due date, or use Remove to clear it.");
      await updateTaskDueDate(task.id, dueAt);
    } catch (error) {
      showFeatureError(error.message);
    }
  });

  const cancel = document.createElement("button");
  cancel.className = "delete-action";
  cancel.type = "button";
  cancel.textContent = "Cancel";
  cancel.addEventListener("click", () => {
    dateInput.value = initial.date;
    timeInput.value = initial.time;
    editor.hidden = true;
  });

  editor.append(dateInput, timeInput, save);
  if (task.due_at) {
    const clear = document.createElement("button");
    clear.className = "delete-action";
    clear.type = "button";
    clear.textContent = "Remove";
    clear.addEventListener("click", async () => {
      try {
        await updateTaskDueDate(task.id, null);
      } catch (error) {
        showFeatureError(error.message);
      }
    });
    editor.append(clear);
  }
  editor.append(cancel);
  return editor;
}

function renderTasks(tasks) {
  taskList.replaceChildren();
  tasksEmpty.hidden = tasks.length > 0;

  for (const task of tasks) {
    const item = document.createElement("li");
    const row = document.createElement("div");
    row.className = `task-row${task.completed ? " completed" : ""}`;

    const checkbox = document.createElement("input");
    checkbox.className = "task-check";
    checkbox.type = "checkbox";
    checkbox.checked = task.completed;
    checkbox.setAttribute("aria-label", `${task.completed ? "Reopen" : "Complete"} ${task.title}`);
    checkbox.addEventListener("change", async () => {
      try {
        await apiRequest(`/api/tasks/${task.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ completed: checkbox.checked }),
        });
        showFeatureError();
        await loadTasks();
      } catch (error) {
        showFeatureError(error.message);
        await loadTasks();
      }
    });

    const copy = document.createElement("div");
    copy.className = "task-copy";
    const title = document.createElement("div");
    title.className = "task-title";
    title.textContent = task.title;
    copy.append(title);
    if (task.description) {
      const description = document.createElement("p");
      description.className = "task-description";
      description.textContent = task.description;
      copy.append(description);
    }
    if (task.due_at) {
      const { overdue, text } = describeDue(task);
      const due = document.createElement("div");
      due.className = `task-due${overdue ? " overdue" : ""}`;
      due.textContent = text;
      copy.append(due);
    }
    const dueEditor = buildDueEditor(task);
    copy.append(dueEditor);

    const editDue = document.createElement("button");
    editDue.className = "delete-action edit-due-action";
    editDue.type = "button";
    editDue.textContent = task.due_at ? "Edit due" : "Set due";
    editDue.addEventListener("click", () => {
      dueEditor.hidden = !dueEditor.hidden;
      if (!dueEditor.hidden) dueEditor.querySelector("input").focus();
    });

    const remove = document.createElement("button");
    remove.className = "delete-action";
    remove.type = "button";
    remove.textContent = "Delete";
    remove.addEventListener("click", async () => {
      if (!window.confirm(`Delete task "${task.title}"?`)) return;
      try {
        await apiRequest(`/api/tasks/${task.id}`, { method: "DELETE" });
        showFeatureError();
        await loadTasks();
      } catch (error) {
        showFeatureError(error.message);
      }
    });

    const actions = document.createElement("div");
    actions.className = "task-actions";
    actions.append(editDue, remove);

    row.append(checkbox, copy, actions);
    item.append(row);
    taskList.append(item);
  }
}

async function loadTasks() {
  try {
    const result = await apiRequest("/api/tasks");
    renderTasks(result.tasks);
  } catch (error) {
    showFeatureError(error.message);
  }
}

function renderNotes(notes) {
  notesCache = notes;
  notesList.replaceChildren();
  notesEmpty.hidden = notes.length > 0;

  for (const note of notes) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.className = `note-list-button${note.id === currentNoteId ? " selected" : ""}`;
    button.type = "button";
    button.setAttribute("aria-current", note.id === currentNoteId ? "true" : "false");

    const title = document.createElement("span");
    title.className = "note-list-title";
    title.textContent = note.title;
    const preview = document.createElement("span");
    preview.className = "note-list-preview";
    preview.textContent = note.preview || "Empty note";
    button.append(title, preview);
    button.addEventListener("click", () => openNote(note.id));
    item.append(button);
    notesList.append(item);
  }
}

async function loadNotes() {
  try {
    const result = await apiRequest("/api/notes");
    renderNotes(result.notes);
  } catch (error) {
    showFeatureError(error.message);
  }
}

async function openNote(noteId) {
  if (!confirmNoteChanges()) return;
  try {
    const result = await apiRequest(`/api/notes/${noteId}`);
    currentNoteId = result.note.id;
    originalNote = { title: result.note.title, content: result.note.content };
    noteTitleInput.value = result.note.title;
    noteContentInput.value = result.note.content;
    noteForm.hidden = false;
    notePlaceholder.hidden = true;
    deleteNoteButton.hidden = false;
    showFeatureError();
    renderNotes(notesCache);
  } catch (error) {
    showFeatureError(error.message);
  }
}

function startNewNote() {
  if (!confirmNoteChanges()) return;
  currentNoteId = null;
  originalNote = null;
  noteForm.reset();
  noteForm.hidden = false;
  notePlaceholder.hidden = true;
  deleteNoteButton.hidden = true;
  showFeatureError();
  noteTitleInput.focus();
  renderNotes(notesCache);
}

function confirmNoteChanges() {
  if (noteForm.hidden) return true;
  const unchanged = originalNote
    ? noteTitleInput.value === originalNote.title && noteContentInput.value === originalNote.content
    : !noteTitleInput.value && !noteContentInput.value;
  return unchanged || window.confirm("Discard unsaved note changes?");
}

for (const tab of workspaceTabs) {
  tab.addEventListener("click", () => switchView(tab.dataset.view));
}

taskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = taskTitleInput.value.trim();
  if (!title) {
    showFeatureError("Enter a task title.");
    taskTitleInput.focus();
    return;
  }
  try {
    const dueAt = buildDueAt(taskDueDateInput.value, taskDueTimeInput.value);
    await apiRequest("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, description: taskDescriptionInput.value, due_at: dueAt }),
    });
    taskForm.reset();
    showFeatureError();
    await loadTasks();
    taskTitleInput.focus();
  } catch (error) {
    showFeatureError(error.message);
  }
});

noteForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = noteTitleInput.value.trim();
  if (!title) {
    showFeatureError("Enter a note title.");
    noteTitleInput.focus();
    return;
  }
  const note = { title, content: noteContentInput.value };
  const isNewNote = currentNoteId === null;
  try {
    const result = await apiRequest(
      isNewNote ? "/api/notes" : `/api/notes/${currentNoteId}`,
      {
        method: isNewNote ? "POST" : "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(note),
      },
    );
    currentNoteId = result.note.id;
    originalNote = { title: result.note.title, content: result.note.content };
    noteForm.hidden = false;
    notePlaceholder.hidden = true;
    deleteNoteButton.hidden = false;
    showFeatureError();
    await loadNotes();
  } catch (error) {
    showFeatureError(error.message);
  }
});

newNoteButton.addEventListener("click", startNewNote);

deleteNoteButton.addEventListener("click", async () => {
  if (currentNoteId === null) return;
  if (!window.confirm(`Delete note "${noteTitleInput.value}"?`)) return;
  try {
    await apiRequest(`/api/notes/${currentNoteId}`, { method: "DELETE" });
    currentNoteId = null;
    originalNote = null;
    noteForm.reset();
    noteForm.hidden = true;
    notePlaceholder.hidden = false;
    deleteNoteButton.hidden = true;
    showFeatureError();
    await loadNotes();
  } catch (error) {
    showFeatureError(error.message);
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = input.value.trim();
  const fromVoice = sendingFromVoice;
  sendingFromVoice = false;
  if (!message || isSending || documentUploadPending || !conversationId) {
    if (!conversationId) showError("The conversation is still starting. Try again in a moment.");
    // handleTranscript checks these first, so a voice message should never get here.
    if (fromVoice) dispatchLoop({ type: "turn_finished", turn: voiceLoop.turn, outcome: "error" });
    return;
  }
  if (!fromVoice) dispatchLoop({ type: "typed_message" }); // typing takes over from the loop
  const turn = voiceLoop.turn;
  showChatView(); // the command bar works from any view; the exchange shows in the transcript

  clearError();
  setVoiceState();
  voiceTurn = fromVoice;
  if (pendingConfirmationId) {
    // The server cancels the pending action before handling this message.
    pendingConfirmationId = null;
    confirmationPanel.hidden = true;
  }
  appendMessage("user", message);
  input.value = "";
  input.style.height = "auto";
  setBusy(true);
  startActivityPolling();

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: conversationId, message }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The assistant could not respond.");
    const { outcome, speech } = acceptTurnResult(result);
    reportTurn(turn, outcome, speech);
    if (selectedDocument && result.document_context) {
      const scope = result.document_context === "full" ? "full extracted text" : "selected excerpts only";
      documentDetail.textContent = `${selectedDocumentDetail} · Last answer used ${scope}`;
    }
  } catch (error) {
    reportVoiceError(error.message || "Could not reach the local assistant backend.");
    dispatchLoop({ type: "turn_finished", turn, outcome: "error" });
  } finally {
    stopActivityPolling();
    setBusy(false);
    if (!pendingConfirmationId) input.focus();
  }
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 180)}px`;
});

newChatButton.addEventListener("click", async () => {
  if (isSending || documentUploadPending) return;
  dispatchLoop({ type: "disable" });
  stopReplyAudio();
  newChatButton.disabled = true;
  clearError();
  try {
    await startConversation(conversationId);
  } catch (error) {
    showError(error.message || "Could not start a new conversation.");
  } finally {
    newChatButton.disabled = false;
  }
});

refreshHealth();
loadVoiceStatus();
window.setInterval(refreshHealth, 10000);
startConversation().catch((error) => showError(error.message));