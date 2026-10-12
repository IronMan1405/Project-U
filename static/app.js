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