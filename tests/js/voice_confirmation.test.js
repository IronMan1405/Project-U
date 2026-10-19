// Spoken confirmation decisions and wake acknowledgements. Run with: node --test tests/js/
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { confirmationDecision } = require(path.join(__dirname, "../../static/voice_loop.js"));
const { pickAcknowledgement, ACKNOWLEDGEMENTS, ACK_HISTORY } = require(path.join(__dirname, "../../static/wake_word.js"));

const base = { pendingId: "abc", idAtStart: "abc", resolving: false, handsFree: true };

test("only an explicit confirm for the action read out before recording approves it", () => {
  assert.deepEqual(confirmationDecision({ ...base, kind: "confirm" }), { action: "approve", reason: null });
  // Recorded before this action was read out (or while another one was waiting): never approves.
  assert.deepEqual(confirmationDecision({ ...base, kind: "confirm", idAtStart: null }), { action: "guard", reason: "stale" });
  assert.deepEqual(confirmationDecision({ ...base, kind: "confirm", idAtStart: "older" }), { action: "guard", reason: "stale" });
});

test("a bare yes or an unclear reply keeps the action waiting and asks for confirm", () => {
  for (const kind of ["affirm", "unclear"]) {
    assert.deepEqual(confirmationDecision({ ...base, kind }), { action: "guard", reason: "say_confirm" }, kind);
  }
});

test("a spoken cancel cancels, like the Cancel button", () => {
  assert.equal(confirmationDecision({ ...base, kind: "decline" }).action, "cancel");
});

test("replies while a confirmation is already being resolved never act twice", () => {
  for (const kind of ["confirm", "affirm", "decline", "unclear"]) {
    assert.deepEqual(confirmationDecision({ ...base, kind, resolving: true }), { action: "guard", reason: "resolving" });
  }
});

test("ordinary speech while an action waits is not sent hands-free (it could be background audio)", () => {
  assert.deepEqual(confirmationDecision({ ...base, kind: null }), { action: "guard", reason: "still_waiting" });
  // Push-to-talk shows it for review as before; nothing is sent automatically either way.
  assert.equal(confirmationDecision({ ...base, kind: null, handsFree: false }).action, "ordinary");
});

test("with nothing pending, every transcript is an ordinary message", () => {
  for (const kind of ["confirm", "affirm", "decline", "unclear", null]) {
    assert.equal(confirmationDecision({ ...base, kind, pendingId: null }).action, "ordinary");
  }
});

test("wake acknowledgements are short, varied, and cannot wake K.R.I.S. again", () => {
  assert.ok(ACKNOWLEDGEMENTS.length >= 5);
  for (const text of ACKNOWLEDGEMENTS) {
    assert.ok(text.split(/\s+/).length <= 4, text);
    assert.doesNotMatch(text, /\b(kris|chris|cris|kriss|chriss|krys|k\.r\.i\.s)\b/i, text);
    assert.doesNotMatch(text, /kindly remember/i, text);
  }
  let recent = [];
  const seen = [];
  let seed = 3;
  const random = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  for (let i = 0; i < 200; i += 1) {
    const pick = pickAcknowledgement(recent, random);
    assert.ok(!recent.includes(pick.text), `repeated ${pick.text}`);
    assert.ok(pick.recent.length <= ACK_HISTORY);
    recent = pick.recent;
    seen.push(pick.text);
  }
  assert.equal(new Set(seen).size, ACKNOWLEDGEMENTS.length); // every one gets used
  // Even a random source stuck at its extremes stays within the list.
  assert.ok(ACKNOWLEDGEMENTS.includes(pickAcknowledgement([], () => 0.9999999).text));
  assert.ok(ACKNOWLEDGEMENTS.includes(pickAcknowledgement([], () => 0).text));
});
