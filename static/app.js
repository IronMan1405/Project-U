const form = document.querySelector("#chat-form");
const input = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-button");
const newChatButton = document.querySelector("#new-chat");
const messagesElement = document.querySelector("#messages");
const emptyState = document.querySelector("#empty-state");
const requestState = document.querySelector("#request-state");
const errorMessage = document.querySelector("#error-message");
const healthDot = document.querySelector("#health-dot");
const healthLabel = document.querySelector("#health-label");
const workspaceTabs = document.querySelectorAll(".workspace-tab");
const viewPanels = document.querySelectorAll(".view-panel");
const featureError = document.querySelector("#feature-error");
const taskForm = document.querySelector("#task-form");
const taskTitleInput = document.querySelector("#task-title");
const taskDescriptionInput = document.querySelector("#task-description");
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
  sendButton.disabled = busy;
  newChatButton.disabled = busy;
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
  clearError();
  input.focus();
}

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

    row.append(checkbox, copy, remove);
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
    await apiRequest("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, description: taskDescriptionInput.value }),
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
  if (!message || isSending) return;
  if (!conversationId) {
    showError("The conversation is still starting. Try again in a moment.");
    return;
  }

  clearError();
  appendMessage("user", message);
  input.value = "";
  input.style.height = "auto";
  setBusy(true);

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: conversationId, message }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The assistant could not respond.");
    appendMessage("assistant", result.response);
  } catch (error) {
    showError(error.message || "Could not reach the local assistant backend.");
  } finally {
    setBusy(false);
    input.focus();
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
  if (isSending) return;
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
window.setInterval(refreshHealth, 10000);
startConversation().catch((error) => showError(error.message));