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
let voiceStatus = { speech_input: false, speech_output: false, max_seconds: 60 };
let recorder = null;
let recordingStream = null;
let recordingChunks = [];
let recordingTimer = null;
let micHeld = false;
let transcribing = false;
let replyAudio = null;
let replyRequest = 0;

function showError(message) {
  errorMessage.textContent = message;
  errorMessage.hidden = false;
}

function clearError() {
  errorMessage.textContent = "";
  errorMessage.hidden = true;
}

function appendMessage(role, content) {
  const message = document.createElement("article");
  message.className = `message ${role}`;

  const label = document.createElement("span");
  label.className = "message-role";
  label.textContent = role === "user" ? "YOU" : "U";

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
  confirmActionButton.disabled = busy || confirmationResolving;
  cancelActionButton.disabled = busy || confirmationResolving;
  documentFileInput.disabled = controlsBusy;
  uploadDocumentButton.disabled = controlsBusy || documentFileInput.files.length === 0;
  clearDocumentButton.disabled = controlsBusy || selectedDocument === null;
  requestState.hidden = !busy;
}

async function refreshHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    const result = await response.json();
    healthDot.classList.toggle("connected", result.connected === true);
    healthDot.classList.toggle("disconnected", result.connected !== true);
    healthLabel.textContent = result.connected ? `${result.model} ready` : "Assistant unavailable";
    healthLabel.title = result.error || "Ollama and the configured model are ready.";
  } catch {
    healthDot.classList.remove("connected");
    healthDot.classList.add("disconnected");
    healthLabel.textContent = "Backend unavailable";
    healthLabel.title = "Could not reach the local assistant backend.";
  }
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
  label.textContent = activity.status === "running"
    ? `${toolLabel(activity.tool)}...`
    : activity.summary || "Assistant action";
  toolActivity.append(label);
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
  }
  toolActivity.hidden = false;
}

function showPendingConfirmation(pending) {
  pendingConfirmationId = pending.confirmation_id;
  confirmationSummary.textContent = `${pending.summary} ${pending.message}`;
  confirmationPanel.hidden = false;
}

function acceptTurnResult(result) {
  renderToolActivities(result.activities);
  if (result.notice) appendMessage("assistant", result.notice);
  if (result.pending_confirmation) {
    showPendingConfirmation(result.pending_confirmation);
    return;
  }
  pendingConfirmationId = null;
  confirmationPanel.hidden = true;
  if (result.response) {
    const content = appendMessage("assistant", result.response);
    if (speakToggle.checked) speakReply(content.textContent);
  }
}

// --- Voice: push-to-talk transcription for review, optional spoken replies -----------

function setVoiceState(message = "") {
  voiceStateElement.textContent = message;
}

async function loadVoiceStatus() {
  try {
    voiceStatus = await apiRequest("/api/voice/status");
  } catch {
    voiceStatus = { speech_input: false, speech_output: false, max_seconds: 60 };
  }
  const canRecord = Boolean(navigator.mediaDevices?.getUserMedia) && typeof MediaRecorder !== "undefined";
  micButton.hidden = !(voiceStatus.speech_input && canRecord);
  speakToggleLabel.hidden = !voiceStatus.speech_output;
}

function preferredRecordingType() {
  const candidates = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || "";
}

function releaseMicrophone() {
  recordingStream?.getTracks().forEach((track) => track.stop());
  recordingStream = null;
}

async function startRecording() {
  if (recorder || transcribing || micButton.disabled) return;
  clearError();
  stopReplyAudio();
  try {
    recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    setVoiceState("");
    showError("Microphone access was denied or is unavailable.");
    return;
  }
  if (!micHeld) {
    releaseMicrophone();
    return;
  }
  const mimeType = preferredRecordingType();
  try {
    recorder = new MediaRecorder(recordingStream, mimeType ? { mimeType } : undefined);
  } catch {
    releaseMicrophone();
    showError("This browser cannot record audio in a supported format.");
    return;
  }
  recordingChunks = [];
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data.size > 0) recordingChunks.push(event.data);
  });
  recorder.addEventListener("stop", finishRecording, { once: true });
  recorder.start();
  micButton.classList.add("recording");
  micButton.setAttribute("aria-pressed", "true");
  setVoiceState(`Listening… release to stop (up to ${voiceStatus.max_seconds}s)`);
  recordingTimer = window.setTimeout(stopRecording, voiceStatus.max_seconds * 1000);
}

function stopRecording() {
  micHeld = false;
  window.clearTimeout(recordingTimer);
  if (recorder && recorder.state !== "inactive") recorder.stop();
}

async function finishRecording() {
  const type = recorder?.mimeType || recordingChunks[0]?.type || "";
  const recording = new Blob(recordingChunks, { type });
  recorder = null;
  recordingChunks = [];
  releaseMicrophone();
  micButton.classList.remove("recording");
  micButton.setAttribute("aria-pressed", "false");
  if (recording.size === 0) {
    setVoiceState("");
    showError("Nothing was recorded. Hold the button while you speak.");
    setBusy(isSending);
    return;
  }

  transcribing = true;
  setBusy(isSending);
  setVoiceState("Transcribing locally…");
  try {
    const formData = new FormData();
    formData.append("audio", recording, "recording");
    const result = await apiRequest("/api/voice/transcribe", { method: "POST", body: formData });
    const existing = input.value.trim();
    input.value = existing ? `${existing} ${result.text}` : result.text;
    input.dispatchEvent(new Event("input"));
    setVoiceState("Review the transcript, then press Enter to send.");
  } catch (error) {
    setVoiceState("");
    showError(error.message || "The recording could not be transcribed.");
  } finally {
    transcribing = false;
    setBusy(isSending);
    input.focus();
  }
}

function stopReplyAudio() {
  replyRequest += 1;
  if (replyAudio) {
    replyAudio.pause();
    URL.revokeObjectURL(replyAudio.src);
    replyAudio = null;
  }
}

async function speakReply(text) {
  stopReplyAudio();
  const request = replyRequest;
  try {
    const response = await fetch("/api/voice/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) {
      const result = await response.json().catch(() => null);
      throw new Error(result?.error || "Local speech output failed.");
    }
    const audio = await response.blob();
    if (request !== replyRequest || !speakToggle.checked) return;
    replyAudio = new Audio(URL.createObjectURL(audio));
    replyAudio.addEventListener("ended", stopReplyAudio, { once: true });
    if (response.headers.get("X-Speech-Truncated") === "1") {
      setVoiceState("Speaking the beginning of a long reply.");
    }
    await replyAudio.play();
  } catch (error) {
    if (request === replyRequest) {
      stopReplyAudio();
      setVoiceState(`Spoken reply unavailable: ${error.message}`);
    }
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
  micHeld = true;
  startRecording();
});
for (const eventName of ["pointerup", "pointercancel"]) {
  micButton.addEventListener(eventName, stopRecording);
}
micButton.addEventListener("keydown", (event) => {
  if ((event.key === " " || event.key === "Enter") && !event.repeat) {
    event.preventDefault();
    micHeld = true;
    startRecording();
  }
});
micButton.addEventListener("keyup", (event) => {
  if (event.key === " " || event.key === "Enter") stopRecording();
});
speakToggle.addEventListener("change", () => {
  if (!speakToggle.checked) {
    stopReplyAudio();
    setVoiceState("");
  }
});

async function resolvePendingAction(approved) {
  if (!conversationId || !pendingConfirmationId || confirmationResolving) return;
  confirmationResolving = true;
  confirmActionButton.disabled = true;
  cancelActionButton.disabled = true;
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
    acceptTurnResult(result);
  } catch (error) {
    showError(error.message || "The pending action could not be resolved.");
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
  if (!message || isSending || documentUploadPending) return;
  if (!conversationId) {
    showError("The conversation is still starting. Try again in a moment.");
    return;
  }

  clearError();
  setVoiceState();
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
    acceptTurnResult(result);
    if (selectedDocument && result.document_context) {
      const scope = result.document_context === "full" ? "full extracted text" : "selected excerpts only";
      documentDetail.textContent = `${selectedDocumentDetail} · Last answer used ${scope}`;
    }
  } catch (error) {
    showError(error.message || "Could not reach the local assistant backend.");
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