// Run with: node --test tests/js/
// Barge-in: tapping the mic while a spoken reply or prompt is playing (Hands-free).
// In static/app.js the tap dispatches "start" first and then startRecording() stops the
// audio, so the interrupted playback's promise resolves afterwards as a stale
// playback_finished. These tests drive the same event order through the loop.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { createVoiceLoop, reduce, viewState, MESSAGES } = require(path.join(__dirname, "../../static/voice_loop.js"));

function play(events, loop = createVoiceLoop()) {
  const actions = [];
  for (const event of events) {
    const result = reduce(loop, event);
    loop = result.loop;
    actions.push(...result.actions.map((action) => ({ ...action, after: event.type })));
  }
  return { loop, actions, starts: actions.filter((a) => a.type === "start_listening").length };
}

const speakingReply = [
  { type: "start" },
  { type: "recording_uploaded" },
  { type: "transcript_handled", outcome: "submitted" },
  { type: "turn_finished", turn: 1, outcome: "response", spoken: true },
];
const speakingPrompt = [
  { type: "start" },
  { type: "recording_uploaded" },
  { type: "transcript_handled", outcome: "submitted" },
  { type: "turn_finished", turn: 1, outcome: "pending", spoken: true },
];
const bargeIn = [{ type: "start" }, { type: "playback_finished", turn: 1, ok: true }]; // tap, then stale audio end

test("barging in on a spoken reply listens once and ignores the interrupted playback", () => {
  const { loop, starts } = play([...speakingReply, ...bargeIn, { type: "playback_finished", turn: 1, ok: false }]);
  assert.equal(loop.state, "listening");
  assert.equal(starts, 0); // the tap itself starts the recording; nothing else asks to listen
  assert.equal(viewState(loop).state, "listening");
});

test("a barge-in follow-up is submitted once and the loop continues normally", () => {
  const { loop, starts } = play([
    ...speakingReply,
    ...bargeIn,
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "submitted" },
    { type: "playback_finished", turn: 1, ok: true }, // very late stale callback from turn 1
    { type: "turn_finished", turn: 2, outcome: "response", spoken: true },
    { type: "playback_finished", turn: 2, ok: true },
  ]);
  assert.equal(loop.turn, 2); // exactly one new submission
  assert.equal(loop.state, "listening");
  assert.equal(starts, 1); // one automatic resume, after turn 2's reply
});

test("a microphone that cannot start after barge-in pauses with an error and never retries", () => {
  const { loop, actions, starts } = play([
    ...speakingReply,
    { type: "start" },
    { type: "listen_failed" },
    { type: "playback_finished", turn: 1, ok: true },
    { type: "recording_discarded", reason: "no_speech" },
  ]);
  assert.equal(loop.state, "off");
  assert.equal(starts, 0);
  assert.equal(loop.notice.kind, "error");
  assert.equal(actions.at(-1).message, MESSAGES.listen_failed);
  assert.equal(viewState(loop).state, "error");
});

test("interrupting a spoken confirmation prompt keeps the action pending", () => {
  const { loop, actions } = play([...speakingPrompt, ...bargeIn]);
  assert.equal(loop.state, "listening");
  assert.equal(loop.confirmationPending, true);
  assert.deepEqual(actions, []); // nothing approves, cancels, or resolves anything
});

test("saying nothing after interrupting a confirmation prompt returns to waiting, not listening", () => {
  const { loop, starts, actions } = play([...speakingPrompt, ...bargeIn, { type: "recording_discarded", reason: "no_speech" }]);
  assert.equal(loop.state, "awaiting_confirmation");
  assert.equal(starts, 0);
  assert.deepEqual(actions, []);
  assert.equal(viewState(loop).state, "awaiting_confirmation");
  // and Confirm/Cancel still resolve it normally afterwards
  const resolved = play([{ type: "confirmation_resolving" }, { type: "turn_finished", turn: 2, outcome: "response", spoken: false }], loop);
  assert.equal(resolved.loop.state, "listening");
  assert.equal(resolved.starts, 1);
});

test("a spoken yes after interrupting the prompt is guarded and the action stays pending", () => {
  const { loop, starts } = play([
    ...speakingPrompt,
    ...bargeIn,
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "guarded" },
  ]);
  assert.equal(loop.state, "awaiting_confirmation");
  assert.equal(starts, 0);
});

test("a spoken no after interrupting the prompt follows the Cancel path, then resumes once", () => {
  const { loop, starts } = play([
    ...speakingPrompt,
    ...bargeIn,
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "cancel_requested" },
    { type: "confirmation_resolving" },
    { type: "turn_finished", turn: 2, outcome: "response", spoken: true },
    { type: "playback_finished", turn: 2, ok: true },
  ]);
  assert.equal(loop.state, "listening");
  assert.equal(loop.confirmationPending, false);
  assert.equal(starts, 1);
});

test("saying something else after interrupting the prompt is an ordinary new message", () => {
  const { loop } = play([...speakingPrompt, ...bargeIn, { type: "recording_uploaded" }, { type: "transcript_handled", outcome: "submitted" }]);
  assert.equal(loop.state, "thinking"); // the server cancels the waiting action (existing rule)
  assert.equal(loop.confirmationPending, false);
});

test("two silences after barging in on a normal reply still pause the loop (unchanged)", () => {
  const silence = { type: "recording_discarded", reason: "no_speech" };
  const { loop, starts } = play([...speakingReply, ...bargeIn, silence, silence]);
  assert.equal(loop.state, "off");
  assert.equal(starts, 1);
  assert.equal(loop.notice.kind, "paused");
});

test("Hands-free off, a hidden page, or a new conversation during a barge-in stop everything", () => {
  for (const stop of ["disable", "page_hidden"]) {
    for (const before of [speakingReply, speakingPrompt]) {
      const { loop, actions, starts } = play([
        ...before,
        { type: "start" },
        { type: stop }, // new conversation dispatches "disable"
        { type: "playback_finished", turn: 1, ok: true },
        { type: "stopped_by_user" }, // the cancelled microphone start reports late
        { type: "recording_discarded", reason: "no_speech" },
      ]);
      assert.equal(loop.state, "off", stop);
      assert.equal(loop.confirmationPending, false);
      assert.equal(starts, 0);
      assert.deepEqual(actions.filter((a) => a.after === stop).map((a) => a.type), ["stop_listening", "stop_playback"]);
    }
  }
});

test("Review mode: interrupting or stopping audio never starts listening", () => {
  // In Review mode the loop is off; recording starts only from the user's own press.
  const { loop, starts, actions } = play([
    { type: "stop_speaking" },
    { type: "playback_finished", turn: 0, ok: true },
    { type: "recording_discarded", reason: "no_speech" },
    { type: "turn_finished", turn: 0, outcome: "response", spoken: true },
  ]);
  assert.equal(loop.state, "off");
  assert.equal(starts, 0);
  assert.deepEqual(actions.map((a) => a.type), ["stop_playback"]);
});

test("the pending-confirmation flag only lives through listening, transcribing, and waiting", () => {
  const events = [
    { type: "start" }, { type: "disable" }, { type: "page_hidden" }, { type: "typed_message" },
    { type: "stopped_by_user" }, { type: "listen_failed" }, { type: "recording_discarded", reason: "no_speech" },
    { type: "recording_discarded", reason: "track_ended" }, { type: "recording_uploaded" },
    { type: "transcription_failed" }, { type: "transcript_handled", outcome: "submitted" },
    { type: "transcript_handled", outcome: "guarded" }, { type: "transcript_handled", outcome: "cancel_requested" },
    { type: "confirmation_resolving" }, { type: "turn_finished", outcome: "pending", spoken: true },
    { type: "turn_finished", outcome: "response", spoken: true }, { type: "playback_finished", ok: true },
    { type: "stop_speaking" },
  ];
  let seed = 7;
  const random = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  for (let run = 0; run < 2000; run += 1) {
    let loop = createVoiceLoop();
    for (let step = 0; step < 30; step += 1) {
      const event = { ...events[Math.floor(random() * events.length)], turn: loop.turn };
      loop = reduce(loop, event).loop;
      if (loop.confirmationPending) {
        assert.ok(["listening", "transcribing", "awaiting_confirmation"].includes(loop.state), loop.state);
      }
      if (loop.state === "awaiting_confirmation") assert.equal(loop.confirmationPending, true);
    }
  }
});
