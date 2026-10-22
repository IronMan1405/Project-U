// Interrupting a spoken hands-free reply with the wake word. Run with: node --test tests/js/
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const Wake = require(path.join(__dirname, "../../static/wake_word.js"));
const Loop = require(path.join(__dirname, "../../static/voice_loop.js"));

function play(events, wake = Wake.createWakeState()) {
  const actions = [];
  for (const event of events) {
    const result = Wake.reduce(wake, event);
    wake = result.wake;
    actions.push(...result.actions.map((action) => ({ ...action, after: event.type })));
  }
  return { wake, actions, types: actions.map((a) => a.type) };
}

const activeConversation = [
  { type: "enable", allowed: true }, { type: "capture_started" }, { type: "burst", clip: "c" },
  { type: "check_result", wake: true }, { type: "acknowledged" },
];

test("the idle wake-word flow is unchanged", () => {
  const { wake, types } = play(activeConversation);
  assert.equal(wake.state, "active");
  assert.deepEqual(types, ["open_capture", "notify", "check_clip", "stop_capture", "acknowledge", "start_conversation"]);
});

test("a reply being spoken opens the light detector; the name interrupts exactly once", () => {
  const { wake, actions } = play([
    ...activeConversation,
    { type: "speech_started" }, { type: "capture_started" },
    { type: "burst", clip: "kris" }, { type: "burst", clip: "again" }, // a second burst while checking is ignored
    { type: "check_result", wake: true }, { type: "check_result", wake: true }, // a late duplicate is ignored
  ]);
  assert.equal(wake.state, "active");
  const after = actions.slice(6).map((a) => a.type);
  assert.deepEqual(after, ["open_capture", "check_clip", "stop_capture", "interrupt"]);
  assert.equal(actions.filter((a) => a.type === "interrupt").length, 1);
});

test("a tap-started conversation (wake word paused) can be interrupted too, and returns to paused", () => {
  const paused = play([{ type: "enable", allowed: false }]).wake;
  assert.equal(paused.state, "paused");
  const started = play([{ type: "speech_started" }, { type: "capture_started" }], paused);
  assert.equal(started.wake.state, "barge_standby");
  const ended = play([{ type: "speech_ended" }], started.wake);
  assert.equal(ended.wake.state, "paused");
  assert.deepEqual(ended.types, ["stop_capture"]);
});

test("ordinary speech, failed or busy checks never interrupt or disable anything", () => {
  const listening = play([...activeConversation, { type: "speech_started" }, { type: "capture_started" }]).wake;
  let result = play([{ type: "burst", clip: "x" }, { type: "check_result", wake: false }], listening);
  assert.equal(result.wake.state, "barge_standby");
  assert.ok(!result.types.includes("interrupt"));
  for (let i = 0; i < 5; i += 1) result = play([{ type: "burst", clip: "x" }, { type: "check_failed" }], result.wake);
  assert.equal(result.wake.state, "barge_standby"); // standby's 3-failure shutdown does not apply mid-reply
  assert.equal(result.wake.message, null);
});

test("a microphone problem during a reply leaves the reply and the wake word alone", () => {
  for (const type of ["capture_failed", "capture_lost", "suspend"]) {
    const { wake, types } = play([...activeConversation, { type: "speech_started" }, { type: type, denied: true }]);
    assert.equal(wake.state, "active", type);
    assert.equal(wake.message, null, type);
    assert.deepEqual(types.slice(-1), ["stop_capture"], type);
  }
});

test("a check that answers after the reply ended is ignored", () => {
  const { wake, types } = play([
    ...activeConversation, { type: "speech_started" }, { type: "capture_started" }, { type: "burst", clip: "k" },
    { type: "speech_ended" }, { type: "check_result", wake: true },
  ]);
  assert.equal(wake.state, "active");
  assert.ok(!types.includes("interrupt"));
});

test("speech never starts barge listening from off, standby or a check in progress", () => {
  for (const events of [[], [{ type: "enable", allowed: true }, { type: "capture_started" }],
                        [{ type: "enable", allowed: true }, { type: "capture_started" }, { type: "burst", clip: "c" }]]) {
    const before = play(events).wake;
    const after = play([{ type: "speech_started" }], before);
    assert.equal(after.wake.state, before.state);
    assert.deepEqual(after.types, []);
  }
});

test("switching the wake word off mid-reply releases the microphone", () => {
  const { wake, types } = play([...activeConversation, { type: "speech_started" }, { type: "disable" }]);
  assert.equal(wake.state, "off");
  assert.deepEqual(types.slice(-1), ["stop_capture"]);
  assert.equal(Wake.enabled(wake), false);
});

test("barge listening is allowed only while the hands-free loop speaks, visibly, with the mic free", () => {
  const ok = { visible: true, handsFree: true, loopSpeaking: true, recording: false, transcribing: false };
  assert.equal(Wake.bargeAllowed(ok), true);
  for (const change of [{ visible: false }, { handsFree: false }, { loopSpeaking: false }, { recording: true }, { transcribing: true }]) {
    assert.equal(Wake.bargeAllowed({ ...ok, ...change }), false, JSON.stringify(change));
  }
});

test("sentences that say the name are not checked (self-trigger guard)", () => {
  for (const text of ["I'm K.R.I.S., your assistant.", "Kris here.", "Ask Chris later", "K R I S"]) {
    assert.equal(Wake.mentionsWakeName(text), true, text);
  }
  for (const text of ["The tests are passing.", "Christmas plans", "crisis", "", null]) {
    assert.equal(Wake.mentionsWakeName(text), false, String(text));
  }
});

test("the interrupted reply's late events never resume it, and a pending confirmation stays safe", () => {
  // The page handles "interrupt" like a tap on "Interrupt and talk": start, then listen.
  let loop = Loop.createVoiceLoop();
  for (const event of [{ type: "start" }, { type: "recording_uploaded" }, { type: "transcript_handled", outcome: "submitted" },
                       { type: "turn_finished", turn: 1, outcome: "pending", spoken: true }]) {
    loop = Loop.reduce(loop, event).loop;
  }
  assert.equal(loop.state, "speaking");
  assert.equal(loop.confirmationPending, true);
  loop = Loop.reduce(loop, { type: "start", maxSilences: 1 }).loop; // the name was heard
  assert.equal(loop.state, "listening");
  assert.equal(loop.confirmationPending, true); // the answer is still judged as a confirmation answer
  const late = Loop.reduce(loop, { type: "playback_finished", turn: 1, ok: true });
  assert.equal(late.loop.state, "listening");
  assert.deepEqual(late.actions, []);
  // The name is never the request: an utterance after it that is not "confirm" approves nothing.
  const decision = Loop.confirmationDecision({ kind: null, pendingId: "a", idAtStart: "a", resolving: false, handsFree: true });
  assert.notEqual(decision.action, "approve");
});

test("the indicator says the reply can be interrupted, without changing the state", () => {
  const speaking = { ...Loop.createVoiceLoop(), state: "speaking" };
  const plain = Loop.viewState(speaking, { speaking: true });
  const interruptible = Loop.viewState(speaking, { speaking: true, interruptible: true });
  assert.equal(plain.label, "Speaking");
  assert.equal(interruptible.state, "speaking");
  assert.equal(interruptible.label, "Speaking · say “Kris” to interrupt");
});

test("during an automation a lone 'stop' cancels it and listening continues", () => {
  // Hands-free conversation: the barge detector is open while the run executes.
  const barge = play([...activeConversation, { type: "speech_started" }, { type: "capture_started" },
    { type: "burst", clip: "stop" }, { type: "check_result", wake: false, stop: true }]);
  assert.equal(barge.wake.state, "barge_standby");
  assert.deepEqual(barge.types.slice(-1), ["cancel_automation"]);
  // Typed request with the wake word in standby: the same.
  const standby = play([{ type: "enable", allowed: true }, { type: "capture_started" }, { type: "burst", clip: "stop" },
    { type: "check_result", wake: false, stop: true }]);
  assert.equal(standby.wake.state, "standby");
  assert.deepEqual(standby.types.slice(-1), ["cancel_automation"]);
  // A plain "Kris" still behaves as before, and nothing cancels without "stop".
  const plain = play([{ type: "enable", allowed: true }, { type: "capture_started" }, { type: "burst", clip: "x" },
    { type: "check_result", wake: false, stop: false }]);
  assert.ok(!plain.types.includes("cancel_automation"));
});
