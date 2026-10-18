// Google Workspace panes (Inbox / Calendar): turns API results into plain display values.
// DOM-free so it can be tested in Node; the browser uses window.GoogleView and renders the
// result with textContent only. Nothing here ever sees a token.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GoogleView = api;
})(typeof self !== "undefined" ? self : this, function () {
  const text = (value, limit = 200) => (typeof value === "string" ? value.slice(0, limit) : "");

  function connectionView(status) {
    if (!status || typeof status !== "object") {
      return { text: "Google Workspace status is unavailable.", tone: "warning", connect: false, disconnect: false, ready: false };
    }
    if (!status.configured) {
      return {
        text: "Google Workspace is not set up (see README: Google Workspace).",
        tone: "muted", connect: false, disconnect: false, ready: false,
      };
    }
    if (status.reconnect_required) {
      // Connected under older permissions: reconnect (or disconnect) before anything runs.
      return { text: text(status.message) || "Reconnect Google to continue.", tone: "warning", connect: true, disconnect: true, ready: false };
    }
    if (!status.connected) {
      return { text: text(status.message) || "Not connected.", tone: "muted", connect: true, disconnect: false, ready: false };
    }
    const account = text(status.account, 120);
    const partial = Boolean(status.message);
    return {
      text: partial ? text(status.message) : account ? `Connected as ${account}` : "Connected.",
      tone: partial ? "warning" : "ok",
      connect: partial, // reconnect to grant what is missing
      disconnect: true,
      ready: true,
    };
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  const timeFormat = { hour: "2-digit", minute: "2-digit", hour12: false };

  function shortWhen(iso, now) {
    const date = new Date(iso);
    if (typeof iso !== "string" || Number.isNaN(date.getTime())) return "";
    if (sameDay(date, now)) return date.toLocaleTimeString([], timeFormat);
    return date.toLocaleDateString("en-GB", { day: "2-digit", month: "short" });
  }

  function errorView(payload) {
    return {
      message: text(payload && payload.message) || "Google Workspace could not be reached.",
      reconnect: Boolean(payload && payload.reconnect),
    };
  }

  function inboxView(result, now = new Date()) {
    if (!result || result.status !== "ok" || !Array.isArray(result.messages)) return { error: errorView(result) };
    const unread = Number.isSafeInteger(result.unread_count) && result.unread_count >= 0 ? result.unread_count : null;
    const items = result.messages.slice(0, 10).map((message) => ({
      id: message && typeof message.id === "string" && /^[A-Za-z0-9]{1,64}$/.test(message.id) ? message.id : null,
      from: text(message && message.from, 120) || "Unknown sender",
      subject: text(message && message.subject) || "(No subject)",
      when: shortWhen(message && message.received, now),
      unread: Boolean(message && message.unread),
    }));
    return {
      unreadText: unread === null ? "Unread count unavailable" : `${unread} unread`,
      items,
      note: items.length ? "" : "Your inbox is empty.",
    };
  }

  function dayLabel(date, now) {
    const tomorrow = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
    if (sameDay(date, now)) return "Today";
    if (sameDay(date, tomorrow)) return "Tomorrow";
    return date.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short" });
  }

  // All-day events use a calendar date (no time zone); timed events use their instant.
  function eventStart(event) {
    if (event.all_day && /^\d{4}-\d{2}-\d{2}$/.test(event.start)) {
      const [year, month, day] = event.start.split("-").map(Number);
      return new Date(year, month - 1, day);
    }
    return new Date(event.start);
  }

  function calendarView(result, now = new Date(), days = 14) {
    if (!result || result.status !== "ok" || !Array.isArray(result.events)) return { error: errorView(result) };
    const items = [];
    for (const event of result.events) {
      if (!event || typeof event.start !== "string") continue;
      const start = eventStart(event);
      if (Number.isNaN(start.getTime())) continue;
      let time = "All day";
      if (!event.all_day) {
        const end = typeof event.end === "string" ? new Date(event.end) : null;
        time = start.toLocaleTimeString([], timeFormat);
        if (end && !Number.isNaN(end.getTime())) time += `–${end.toLocaleTimeString([], timeFormat)}`;
      }
      items.push({ day: dayLabel(start, now), time, title: text(event.title) || "(No title)" });
    }
    const span = days === 30 ? "the next month" : "the next two weeks";
    let note = items.length ? "" : `No events in ${span}.`;
    if (result.truncated) note = "Showing the first 100 events.";
    return { items, note };
  }

  return { connectionView, inboxView, calendarView, errorView };
});
