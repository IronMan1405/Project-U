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
const wakeToggle = document.querySelector("#wake-word");
const wakeLabel = document.querySelector("#wake-word-label");
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
const confirmationPreview = document.querySelector("#confirmation-preview");
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
// Wake word (static/wake_word.js decides; this carries out its actions). wakeConversation is
// true while the hands-free conversation it started is running.
let wake = window.WakeWord.createWakeState();
let wakeConversation = false;
let standby = null; // wake-word standby's { stream, track, source, node, onEnded }
let standbySession = 0;
let wakeWorkletReady = null;
let wakeSyncing = false;

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
  syncWake();
  const view = window.VoiceLoop.viewState(voiceLoop, {
    speaking: replyFinish !== null,
    recording: recorder !== null || startPending,
    transcribing,
    busy: isSending || confirmationResolving,
    error: !errorMessage.hidden,
    standby: window.WakeWord.capturing(wake),
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
  const mode = window.WakeWord.enabled(wake) ? "Wake word" : handsFreeToggle.checked ? "Hands-free" : "Review";
  setTelemetry(coreMode, mode, mode === "Review" ? "" : "active");
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
  if (wake.message && !window.WakeWord.enabled(wake)) alerts.push(["danger", "Wake word", wake.message]);
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

// --- Google Workspace Inbox / Calendar (opt-in, read-only) ---------------------------------
// The server holds every token; this code only sees display values. Inbox refreshes every
// minute and Calendar every five minutes, only while the page is visible and connected.
const GOOGLE_INBOX_MS = 60000;
const GOOGLE_CALENDAR_MS = 300000;
const GOOGLE_CONNECT_WATCH_MS = 600000; // how long to watch for a consent finished in another tab
const googlePanes = {
  inbox: {
    state: document.querySelector("#inbox-state"),
    stateText: document.querySelector("#inbox-state-text"),
    list: document.querySelector("#inbox-list"),
    note: document.querySelector("#inbox-note"),
    unread: document.querySelector("#inbox-unread"),
    connect: document.querySelector("#inbox-connect"),
    disconnect: document.querySelector("#inbox-disconnect"),
    url: "/api/google/inbox",
    every: GOOGLE_INBOX_MS,
    capability: "inbox.read",
  },
  calendar: {
    state: document.querySelector("#calendar-state"),
    stateText: document.querySelector("#calendar-state-text"),
    list: document.querySelector("#calendar-list"),
    note: document.querySelector("#calendar-note"),
    unread: null,
    connect: document.querySelector("#calendar-connect"),
    disconnect: document.querySelector("#calendar-disconnect"),
    url: "/api/google/calendar?days=14",
    every: GOOGLE_CALENDAR_MS,
    capability: "calendar.read",
  },
};
let googleStatus = null;
let googleWatchUntil = 0;
// The Calendar panel shows the next 2 weeks, or the next month when expanded. This is a display
// range only: the assistant's Calendar tools can search any past or future date.
let calendarDays = 14;
const calendarRange = document.querySelector("#calendar-range");
const calendarRangeLabel = document.querySelector("#calendar-range-label");
const calendarRangeToggle = document.querySelector("#calendar-range-toggle");
calendarRangeToggle.addEventListener("click", () => {
  calendarDays = calendarDays === 14 ? 30 : 14;
  googlePanes.calendar.url = `/api/google/calendar?days=${calendarDays}`;
  calendarRangeLabel.textContent = calendarDays === 14 ? "Next 2 weeks" : "Next month";
  calendarRangeToggle.textContent = calendarDays === 14 ? "Show next month" : "Show next 2 weeks";
  calendarRangeToggle.setAttribute("aria-pressed", String(calendarDays === 30));
  refreshGooglePane("calendar");
});
const googleTimers = { inbox: null, calendar: null, status: null };
const googleInFlight = { inbox: false, calendar: false, status: false };

async function googleRequest(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  return { ok: response.ok, payload };
}

function setGoogleNote(pane, message) {
  pane.note.textContent = message || "";
  pane.note.hidden = !message;
}

function renderGoogleConnection(status) {
  const view = window.GoogleView.connectionView(status);
  for (const pane of Object.values(googlePanes)) {
    pane.stateText.textContent = view.text;
    pane.state.dataset.tone = view.tone;
    pane.connect.hidden = !view.connect;
    pane.disconnect.hidden = !view.disconnect;
    if (!view.ready) {
      if (pane === googlePanes.calendar) calendarRange.hidden = true;
      pane.list.replaceChildren();
      setGoogleNote(pane, "");
      if (pane.unread) pane.unread.hidden = true;
    }
  }
}

function renderInbox(view) {
  const pane = googlePanes.inbox;
  if (view.error) {
    pane.list.replaceChildren();
    pane.unread.hidden = true;
    setGoogleNote(pane, view.error.message);
    return;
  }
  pane.unread.textContent = view.unreadText;
  pane.unread.hidden = false;
  pane.list.replaceChildren(...view.items.map((item) => {
    const row = document.createElement("li");
    row.className = "google-item mail-item";
    if (item.unread) row.dataset.unread = "true";
    const top = document.createElement("div");
    top.className = "google-item-top";
    const from = document.createElement("span");
    from.className = "google-item-from";
    from.textContent = item.from;
    const when = document.createElement("span");
    when.className = "google-item-when";
    when.textContent = item.when;
    top.append(from, when);
    const subject = document.createElement("div");
    subject.className = "google-item-subject";
    subject.textContent = item.subject;
    row.append(top, subject);
    if (item.id && googleStatus?.capabilities?.["mail.draft"]) {
      // Puts a request in the message box for the user to finish and send; nothing is drafted
      // until K.R.I.S. shows the exact draft and the user presses Confirm.
      const reply = document.createElement("button");
      reply.type = "button";
      reply.className = "google-item-action";
      reply.textContent = "Draft reply";
      reply.setAttribute("aria-label", `Draft a reply to ${item.from}: ${item.subject}`);
      reply.addEventListener("click", () => {
        input.value = `Draft a reply to the email from ${item.from} about "${item.subject}" ` +
          `(reply_to_message_id ${item.id}). Say: `;
        input.dispatchEvent(new Event("input"));
        showChatView();
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      });
      row.append(reply);
    }
    return row;
  }));
  setGoogleNote(pane, view.note);
}

function renderCalendar(view) {
  const pane = googlePanes.calendar;
  calendarRange.hidden = Boolean(view.error);
  if (view.error) {
    pane.list.replaceChildren();
    setGoogleNote(pane, view.error.message);
    return;
  }
  let lastDay = "";
  const rows = [];
  for (const item of view.items) {
    if (item.day !== lastDay) {
      lastDay = item.day;
      const heading = document.createElement("li");
      heading.className = "google-day";
      heading.textContent = item.day;
      rows.push(heading);
    }
    const row = document.createElement("li");
    row.className = "google-item event-item";
    const time = document.createElement("span");
    time.className = "google-item-when";
    time.textContent = item.time;
    const title = document.createElement("span");
    title.className = "google-item-subject";
    title.textContent = item.title;
    row.append(time, title);
    rows.push(row);
  }
  pane.list.replaceChildren(...rows);
  setGoogleNote(pane, view.note);
}

function scheduleGoogle(name, delay) {
  window.clearTimeout(googleTimers[name]);
  googleTimers[name] = null;
  if (document.visibilityState !== "hidden" && delay) {
    googleTimers[name] = window.setTimeout(
      () => (name === "status" ? refreshGoogleStatus() : refreshGooglePane(name)), delay,
    );
  }
}

async function refreshGooglePane(name) {
  const pane = googlePanes[name];
  if (googleInFlight[name] || document.visibilityState === "hidden") return;
  if (!googleStatus?.connected || !googleStatus.capabilities?.[pane.capability]) {
    if (googleStatus?.connected) {
      pane.list.replaceChildren();
      setGoogleNote(pane, "Access was not granted. Reconnect to allow it.");
    }
    return;
  }
  googleInFlight[name] = true;
  let needsStatus = false;
  try {
    const { payload } = await googleRequest(pane.url);
    const view = name === "inbox"
      ? window.GoogleView.inboxView(payload)
      : window.GoogleView.calendarView(payload, new Date(), calendarDays);
    if (name === "inbox") renderInbox(view);
    else renderCalendar(view);
    needsStatus = Boolean(view.error?.reconnect);
  } catch {
    const view = { error: window.GoogleView.errorView(null) };
    if (name === "inbox") renderInbox(view);
    else renderCalendar(view);
  } finally {
    googleInFlight[name] = false;
  }
  if (needsStatus) refreshGoogleStatus(); // disconnected or revoked: show the real state
  else scheduleGoogle(name, pane.every);
}

async function refreshGoogleStatus() {
  if (googleInFlight.status || document.visibilityState === "hidden") return;
  googleInFlight.status = true;
  try {
    const { ok, payload } = await googleRequest("/api/google/status");
    googleStatus = ok ? payload : null;
  } catch {
    googleStatus = null;
  } finally {
    googleInFlight.status = false;
  }
  renderGoogleConnection(googleStatus);
  if (googleStatus?.connected) {
    googleWatchUntil = 0;
    refreshGooglePane("inbox");
    refreshGooglePane("calendar");
  } else {
    scheduleGoogle("inbox", 0);
    scheduleGoogle("calendar", 0);
  }
  // While a consent page is open in another tab, check every few seconds for the result.
  scheduleGoogle("status", Date.now() < googleWatchUntil ? 4000 : 0);
}

async function connectGoogle() {
  try {
    const { ok, payload } = await googleRequest("/api/google/connect", { method: "POST" });
    if (!ok || !payload?.authorization_url?.startsWith("https://accounts.google.com/")) {
      for (const pane of Object.values(googlePanes)) setGoogleNote(pane, window.GoogleView.errorView(payload).message);
      return;
    }
    window.open(payload.authorization_url, "_blank", "noopener,noreferrer");
    googleWatchUntil = Date.now() + GOOGLE_CONNECT_WATCH_MS;
    scheduleGoogle("status", 4000);
  } catch {
    for (const pane of Object.values(googlePanes)) setGoogleNote(pane, window.GoogleView.errorView(null).message);
  }
}

async function disconnectGoogle() {
  if (!window.confirm("Disconnect Google Workspace? K.R.I.S. will revoke its access and remove the saved sign-in.")) return;
  try {
    const { ok, payload } = await googleRequest("/api/google/disconnect", { method: "POST" });
    if (!ok) {
      for (const pane of Object.values(googlePanes)) setGoogleNote(pane, window.GoogleView.errorView(payload).message);
    }
  } catch {
    // The status refresh below shows the real state either way.
  }
  refreshGoogleStatus();
}

for (const pane of Object.values(googlePanes)) {
  pane.connect.addEventListener("click", connectGoogle);
  pane.disconnect.addEventListener("click", disconnectGoogle);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    for (const name of Object.keys(googleTimers)) scheduleGoogle(name, 0);
  } else {
    refreshGoogleStatus(); // also picks up a connection finished in another tab
  }
});
refreshGoogleStatus();

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
    create_gmail_draft: "Creating Gmail draft",
    search_gmail: "Searching Gmail",
    read_gmail_message: "Reading email",
    mark_email_read: "Marking email as read",
    mark_email_unread: "Marking email as unread",
    star_email: "Starring email",
    unstar_email: "Removing email star",
    archive_email: "Archiving email",
    trash_email: "Moving email to Trash",
    send_email: "Sending email",
    reply_to_email: "Sending reply",
    forward_email: "Forwarding email",
    list_gmail_drafts: "Checking Gmail drafts",
    send_gmail_draft: "Sending Gmail draft",
    search_calendar_events: "Searching calendar",
    read_calendar_event: "Reading calendar event",
    create_calendar_event: "Creating calendar event",
    update_calendar_event: "Changing calendar event",
    delete_calendar_event: "Cancelling calendar event",
    add_label: "Adding Gmail label",
    remove_label: "Removing Gmail label",
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

// Shows exactly what an approval does: every field of a Gmail draft, or the exact Gmail
// action and the one message it applies to.
function renderConfirmationPreview(preview) {
  confirmationPreview.replaceChildren();
  const kind = preview && preview.kind;
  confirmationPreview.hidden = !["gmail_draft", "gmail_action", "sections"].includes(kind);
  if (confirmationPreview.hidden) return;
  if (kind === "sections") {
    renderPreviewSections(preview);
    return;
  }
  const rows = [];
  if (kind === "gmail_draft") {
    rows.push(
      ["Type", preview.reply ? "Reply draft in the original thread (not sent)" : "New email draft (not sent)"],
      ["To", (preview.to || []).join(", ")],
    );
    if (preview.cc && preview.cc.length) rows.push(["Cc", preview.cc.join(", ")]);
    rows.push(["Subject", preview.subject || "(no subject)"], ["Body", preview.body || ""]);
  } else {
    rows.push(["Action", preview.describe || ""]);
    if (preview.label) rows.push(["Label", preview.label]);
    rows.push(
      ["From", preview.from || ""],
      ["Subject", preview.subject || "(no subject)"],
      ["Date", preview.date || ""],
      ["Message", `${preview.message_id || ""} (thread ${preview.thread_id || ""})`],
    );
  }
  for (const [label, value] of rows) {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = String(value);
    if (label === "Body") detail.className = "confirmation-preview-body";
    confirmationPreview.append(term, detail);
  }
}

// Sends and Calendar changes: headed groups of exact label/value rows plus warnings, all
// built by the server from the action it will run. Rendered with textContent only.
function renderPreviewSections(preview) {
  for (const section of preview.sections || []) {
    if (section.heading) {
      const heading = document.createElement("dt");
      heading.className = "confirmation-preview-heading";
      heading.textContent = section.heading;
      confirmationPreview.append(heading);
    }
    for (const [label, value] of section.rows || []) {
      const term = document.createElement("dt");
      term.textContent = label;
      const detail = document.createElement("dd");
      detail.textContent = String(value);
      if (label === "Body" || label === "Description") detail.className = "confirmation-preview-body";
      confirmationPreview.append(term, detail);
    }
  }
  for (const note of preview.notes || []) {
    const warning = document.createElement("dd");
    warning.className = "confirmation-preview-note";
    warning.textContent = note;
    confirmationPreview.append(warning);
  }
}

function showPendingConfirmation(pending) {
  pendingConfirmationId = pending.confirmation_id;
  confirmationSummary.textContent = `${pending.summary} ${pending.message}`;
  renderConfirmationPreview(pending.preview);
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
  wakeLabel.hidden = micButton.hidden || !voiceStatus.wake_word;
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
    // A conversation started by the wake word waits wake_follow_up_ms, then returns to standby.
    noSpeechTimeoutMs: wakeConversation ? voiceStatus.wake_follow_up_ms : voiceStatus.no_speech_timeout_ms,
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
  renderVoiceView(); // also releases wake-word standby's microphone before this one opens
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
    // Only a user tap (or the wake word) starts the hands-free loop; a tap during a wake-word
    // conversation keeps its shorter silence limit.
    dispatchLoop({ type: "start", ...(wakeConversation ? { maxSilences: 1 } : {}) });
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
  if (document.visibilityState !== "hidden") {
    renderVoiceView(); // wake-word standby resumes when the page is visible again
    return;
  }
  if (recorder || startPending) stopRecording("page_hidden");
  dispatchLoop({ type: "page_hidden" });
});
window.addEventListener("pagehide", () => {
  closeStandby();
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
    // The wake word starts hands-free conversations, so it switches off with Hands-free.
    wakeConversation = false;
    dispatchWake({ type: "disable" });
    dispatchLoop({ type: "disable" });
    voiceTurn = false;
    stopReplyAudio();
  }
  setVoiceState(
    handsFreeToggle.checked ? "Hands-free: tap the mic and speak; it stops when you pause and sends." : "",
  );
  renderVoiceView(); // display only: the core's MODE readout
});

// --- Wake word ("Kris") ---------------------------------------------------------------
// Standby holds its own microphone stream only while static/wake_word.js allows it (page
// visible, no recording, transcription, spoken reply or hands-free conversation). On the wake
// word it releases that stream before the acknowledgment plays, and the existing hands-free
// loop then opens the microphone as if the mic had been tapped, so there is only ever one
// consumer and K.R.I.S.'s own speech is never heard by standby.
const WAKE_ACK_TEXT = "Yes?";
const WAKE_RING_SECONDS = 3;

function wakeFacts() {
  return {
    visible: document.visibilityState !== "hidden",
    recording: recorder !== null || startPending,
    transcribing,
    speaking: replyFinish !== null,
    conversation: voiceLoop.state !== "off",
  };
}

function dispatchWake(event) {
  const result = window.WakeWord.reduce(wake, event);
  wake = result.wake;
  for (const action of result.actions) {
    if (action.type === "open_capture") openStandby();
    else if (action.type === "stop_capture") closeStandby();
    else if (action.type === "check_clip") checkWakeClip(action.clip);
    else if (action.type === "acknowledge") acknowledgeWake();
    else if (action.type === "start_conversation") startWakeConversation();
    else if (action.type === "notify") setVoiceState(action.message);
  }
  wakeToggle.checked = window.WakeWord.enabled(wake);
  renderVoiceView();
}

// Moves standby in or out of the microphone as page facts change; called on every voice
// render. Re-entrant calls (from the dispatches below) are skipped.
function syncWake() {
  if (wakeSyncing) return;
  wakeSyncing = true;
  try {
    const facts = wakeFacts();
    const allowed = window.WakeWord.standbyAllowed(facts);
    if (wake.state === "active" && voiceLoop.state === "off" && !facts.recording && !facts.transcribing &&
        !facts.speaking && !isSending) {
      wakeConversation = false;
      // The conversation ended (silence, an error, or typing took over): clear the
      // "Hands-free paused" notice, since standby is what happens next.
      const notice = voiceLoop.notice;
      if (notice) dispatchLoop({ type: "disable" });
      if (notice?.kind === "error") logActivity(notice.message, "danger");
      dispatchWake({ type: "conversation_ended", allowed });
      if (allowed) setVoiceState("Back to standby. Say “Kris” when you need me.");
    } else if (window.WakeWord.capturing(wake) && !allowed) {
      dispatchWake({ type: "suspend" });
    } else if (wake.state === "paused" && allowed) {
      dispatchWake({ type: "resume" });
    }
  } finally {
    wakeSyncing = false;
  }
}

async function openStandby() {
  const session = ++standbySession;
  if (!audioContext?.audioWorklet || typeof AudioWorkletNode === "undefined") {
    dispatchWake({ type: "unsupported" });
    return;
  }
  let stream;
  try {
    wakeWorkletReady ||= audioContext.audioWorklet.addModule("/static/wake_capture.js");
    await wakeWorkletReady;
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (error) {
    if (error?.name !== "NotAllowedError" && error?.name !== "SecurityError") wakeWorkletReady = null;
    if (session === standbySession) {
      dispatchWake({ type: "capture_failed", denied: error?.name === "NotAllowedError" || error?.name === "SecurityError" });
    }
    return;
  }
  if (session !== standbySession || wake.state !== "starting") {
    stream.getTracks().forEach((track) => track.stop()); // switched off or suspended meanwhile
    return;
  }
  let source;
  let node;
  try {
    if (audioContext.state === "suspended") await audioContext.resume();
    source = audioContext.createMediaStreamSource(stream);
    node = new AudioWorkletNode(audioContext, "wake-capture");
    source.connect(node);
    node.connect(audioContext.destination); // the worklet outputs silence; this keeps it running
  } catch {
    stream.getTracks().forEach((track) => track.stop());
    if (session === standbySession) dispatchWake({ type: "capture_failed", denied: false });
    return;
  }
  const rate = audioContext.sampleRate;
  const ring = window.WakeWord.createSampleRing(rate, WAKE_RING_SECONDS);
  const detector = window.WakeWord.createBurstDetector();
  const track = stream.getAudioTracks()[0] || null;
  const onEnded = () => {
    if (session === standbySession) dispatchWake({ type: "capture_lost" });
  };
  track?.addEventListener("ended", onEnded);
  node.port.onmessage = (event) => {
    if (session !== standbySession) return;
    ring.push(event.data);
    const burst = detector.update(window.VoiceActivity.rmsDb(event.data), (ring.written / rate) * 1000);
    if (!burst || wake.state !== "standby") return;
    const samples = ring.slice(Math.floor((burst.startMs / 1000) * rate), Math.ceil((burst.endMs / 1000) * rate));
    const clip = window.WakeWord.encodeWav(window.WakeWord.downsample(samples, rate, 16000), Math.min(rate, 16000));
    dispatchWake({ type: "burst", clip });
  };
  standby = { stream, track, source, node, onEnded };
  dispatchWake({ type: "capture_started" });
}

function closeStandby() {
  standbySession += 1;
  if (!standby) return;
  const { stream, track, source, node, onEnded } = standby;
  standby = null;
  track?.removeEventListener("ended", onEnded);
  node.port.onmessage = null;
  try {
    source.disconnect();
    node.disconnect();
  } catch {
    // already disconnected
  }
  stream.getTracks().forEach((item) => item.stop());
}

async function checkWakeClip(clip) {
  let result;
  try {
    const formData = new FormData();
    formData.append("audio", new Blob([clip], { type: "audio/wav" }), "wake.wav");
    const response = await fetch("/api/voice/wake", { method: "POST", body: formData });
    if (response.status === 429) {
      result = { type: "check_result", wake: false }; // busy transcribing something else
    } else if (!response.ok) {
      result = { type: "check_failed" };
    } else {
      result = { type: "check_result", wake: Boolean((await response.json()).wake) };
    }
  } catch {
    result = { type: "check_failed" };
  }
  dispatchWake(result);
}

async function acknowledgeWake() {
  logActivity("Wake word heard");
  setVoiceState(WAKE_ACK_TEXT);
  if (voiceStatus.speech_output) await speakReply(WAKE_ACK_TEXT);
  // A short pause so the end of the acknowledgment is not at the start of the recording.
  await new Promise((resolve) => window.setTimeout(resolve, 150));
  dispatchWake({ type: "acknowledged" });
}

function startWakeConversation() {
  wakeConversation = true;
  voiceTurn = true;
  dispatchLoop({ type: "start", maxSilences: 1 });
  startRecording(true).then((result) => {
    if (result === "failed") dispatchLoop({ type: "listen_failed" });
    else if (result === "cancelled") dispatchLoop({ type: "stopped_by_user" });
  });
}

wakeToggle.addEventListener("change", () => {
  if (!wakeToggle.checked) {
    wakeConversation = false;
    dispatchWake({ type: "disable" });
    setVoiceState("Wake word off. The microphone is not monitored.");
    return;
  }
  // The wake word starts hands-free conversations, so it needs Hands-free.
  if (!handsFreeToggle.checked) {
    handsFreeToggle.checked = true;
    updateMicLabel();
  }
  ensureAudioContext(); // this click is the user gesture browsers require for audio processing
  dispatchWake({ type: "enable", allowed: window.WakeWord.standbyAllowed(wakeFacts()) });
  if (wake.state === "paused") setVoiceState("Wake word on; standby starts when K.R.I.S. is idle.");
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
    if (approved && googleStatus?.connected) { // show a Gmail or Calendar change now
      refreshGooglePane("inbox");
      refreshGooglePane("calendar");
    }
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