// Run with: node --test tests/js/
// Google Workspace panes (static/google_view.js): connection states, inbox and calendar
// display values, and safe handling of missing or hostile data.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { connectionView, inboxView, calendarView, errorView } = require(path.join(__dirname, "../../static/google_view.js"));

const caps = { identity: true, "inbox.read": true, "calendar.read": true };

test("connection states decide the message and which button shows", () => {
  const unset = connectionView({ configured: false, connected: false });
  assert.match(unset.text, /not set up/);
  assert.equal(unset.connect, false);
  const disconnected = connectionView({ configured: true, connected: false, message: "Not connected." });
  assert.equal(disconnected.connect, true);
  assert.equal(disconnected.disconnect, false);
  const connected = connectionView({ configured: true, connected: true, account: "ops@example.org", capabilities: caps });
  assert.equal(connected.text, "Connected as ops@example.org");
  assert.equal(connected.tone, "ok");
  assert.equal(connected.connect, false);
  assert.equal(connected.disconnect, true);
  const partial = connectionView({ configured: true, connected: true, account: "x", message: "Some access was not granted. Reconnect to allow it." });
  assert.equal(partial.tone, "warning");
  assert.equal(partial.connect, true);
  assert.equal(connectionView(null).ready, false);
});

test("the inbox shows an unread count and plain-text headers", () => {
  const now = new Date(2027, 2, 10, 15, 0);
  const view = inboxView({
    status: "ok",
    unread_count: 0,
    messages: [
      { from: "Ada", subject: "Review", received: new Date(2027, 2, 10, 9, 5).toISOString(), unread: true },
      { from: "<b>Bob</b>", subject: "x".repeat(400), received: new Date(2027, 2, 2, 9, 5).toISOString(), unread: false },
      { from: "", subject: "", received: "not a date" },
    ],
  }, now);
  assert.equal(view.unreadText, "0 unread");
  assert.equal(view.items[0].when, "09:05");
  assert.equal(view.items[0].unread, true);
  assert.equal(view.items[1].from, "<b>Bob</b>"); // rendered with textContent, so shown literally
  assert.equal(view.items[1].subject.length, 200);
  assert.match(view.items[1].when, /02 Mar/);
  assert.equal(view.items[2].from, "Unknown sender");
  assert.equal(view.items[2].when, "");
  assert.equal(inboxView({ status: "ok", unread_count: null, messages: [] }).unreadText, "Unread count unavailable");
  assert.equal(inboxView({ status: "ok", unread_count: 2, messages: [] }).note, "Your inbox is empty.");
});

test("calendar events are grouped by day, with all-day events kept on their date", () => {
  const now = new Date(2027, 2, 10, 15, 0);
  const view = calendarView({
    status: "ok",
    events: [
      { title: "Stand-up", start: new Date(2027, 2, 10, 9, 30).toISOString(), end: new Date(2027, 2, 10, 9, 45).toISOString(), all_day: false },
      { title: "Offsite", start: "2027-03-11", end: "2027-03-12", all_day: true },
      { title: "", start: new Date(2027, 2, 13, 8, 0).toISOString(), end: null, all_day: false },
      { title: "Broken", start: "garbage", all_day: false },
    ],
    truncated: false,
  }, now);
  assert.deepEqual(view.items.map((item) => item.day.slice(0, 8)), ["Today", "Tomorrow", view.items[2].day.slice(0, 8)]);
  assert.equal(view.items[0].time, "09:30–09:45");
  assert.equal(view.items[1].time, "All day");
  assert.equal(view.items[2].title, "(No title)");
  assert.equal(view.items[2].time, "08:00");
  assert.equal(calendarView({ status: "ok", events: [] }).note, "No events in the next two weeks.");
  assert.equal(calendarView({ status: "ok", events: [], truncated: true }).note, "Showing the first 100 events.");
});

test("errors carry only the server's safe message and the reconnect hint", () => {
  const failure = { status: "error", kind: "revoked", message: "Google access was revoked or has expired. Connect again.", reconnect: true };
  assert.deepEqual(inboxView(failure).error, { message: failure.message, reconnect: true });
  assert.deepEqual(calendarView(null).error, { message: "Google Workspace could not be reached.", reconnect: false });
  assert.equal(errorView({ message: { toString: () => "x" } }).message, "Google Workspace could not be reached.");
});

test("a connection made before draft permission existed asks to reconnect", () => {
  const view = connectionView({
    configured: true, connected: false, reconnect_required: true,
    message: "K.R.I.S. now needs permission to create Gmail drafts. Reconnect Google to continue.",
  });
  assert.equal(view.tone, "warning");
  assert.equal(view.connect, true);
  assert.equal(view.disconnect, true);
  assert.equal(view.ready, false);
  assert.match(view.text, /Reconnect/);
});

test("inbox items keep only valid Gmail message IDs for reply drafts", () => {
  const view = inboxView({
    status: "ok", unread_count: 0,
    messages: [{ id: "abc123", from: "A", subject: "S" }, { id: "../drafts", from: "B" }, { from: "C" }, { id: 42 }],
  });
  assert.deepEqual(view.items.map((item) => item.id), ["abc123", null, null, null]);
});

test("the calendar panel describes its own display range", () => {
  assert.equal(calendarView({ status: "ok", events: [] }).note, "No events in the next two weeks.");
  assert.equal(calendarView({ status: "ok", events: [] }, new Date(), 14).note, "No events in the next two weeks.");
  assert.equal(calendarView({ status: "ok", events: [] }, new Date(), 30).note, "No events in the next month.");
  assert.equal(calendarView({ status: "ok", events: [], truncated: true }, new Date(), 30).note, "Showing the first 100 events.");
});

test("with several Google accounts the status names the default and the others", () => {
  const status = {
    configured: true, connected: true, account: "me@gmail.example", message: null, default_account: "personal",
    accounts: [
      { name: "personal", email: "me@gmail.example", default: true, connected: true },
      { name: "BITS", email: "daks@bits.example", default: false, connected: true },
      { name: "work", email: null, default: false, connected: false },
    ],
  };
  const view = connectionView(status);
  assert.equal(view.text, "Connected as me@gmail.example · default: personal; also BITS (daks@bits.example), work (not connected)");
  const single = connectionView({ ...status, accounts: [status.accounts[0]] });
  assert.equal(single.text, "Connected as me@gmail.example");
});
