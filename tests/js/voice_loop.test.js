// Run with: node --test tests/js/
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { createVoiceLoop, reduce, MAX_CONSECUTIVE_SILENCES, MESSAGES } = require(
  path.join(__dirname, "../../static/voice_loop.js"),
);

// Apply events in order; return the final loop and every action produced.
function play(events, loop = createVoiceLoop()) {
  const actions = [];
  for (const event of events) {
    const result = reduce(loop, event);
    loop = result.loop;
    actions.push(...result.actions.map((action) => ({ ...action, after: event.type })));
  }
  return { loop, actions, starts: actions.filter((action) => action.type === "start_listening").length };
}

const spokenTurn = (turn) => [
  { type: "recording_uploaded" },
  { type: "transcript_handled", outcome: "submitted" },
  { type: "turn_finished", turn, outcome: "response", spoken: true },
  { type: "playback_finished", turn, ok: true },
];

test("never listens without a user tap", () => {
  const { loop, starts } = play([
    { type: "turn_finished", turn: 0, outcome: "response", spoken: false },
    { type: "playback_finished", turn: 0, ok: true },
    { type: "recording_discarded", reason: "no_speech" },
    { type: "transcript_handled", outcome: "submitted" },
  ]);
  assert.equal(loop.state, "off");
  assert.equal(starts, 0);
});

test("a full spoken cycle resumes listening only after playback ends", () => {
  const steps = [{ type: "start" }, ...spokenTurn(1)];
  let loop = createVoiceLoop();
  const statesAndStarts = steps.map((event) => {
    const result = reduce(loop, event);
    loop = result.loop;
    return [loop.state, result.actions.filter((a) => a.type === "start_listening").length];
  });
  assert.deepEqual(statesAndStarts, [
    ["listening", 0],
    ["transcribing", 0],
    ["thinking", 0],
    ["speaking", 0],
    ["listening", 1],
  ]);
});

test("repeats for several turns", () => {
  const { loop, starts } = play([{ type: "start" }, ...spokenTurn(1), ...spokenTurn(2), ...spokenTurn(3)]);
  assert.equal(loop.state, "listening");
  assert.equal(loop.turn, 3);
  assert.equal(starts, 3);
});

test("an unspoken reply resumes listening as soon as it is ready", () => {
  const { loop, actions } = play([
    { type: "start" },
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "submitted" },
    { type: "turn_finished", turn: 1, outcome: "response", spoken: false },
  ]);
  assert.equal(loop.state, "listening");
  assert.deepEqual(actions.map((a) => [a.type, a.after]), [["start_listening", "turn_finished"]]);
});

test("does not resume while a reply is still generating", () => {
  const { loop, starts } = play([
    { type: "start" },
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "submitted" },
    { type: "playback_finished", turn: 1, ok: true }, // e.g. an earlier reply's audio ending
    { type: "recording_discarded", reason: "no_speech" },
  ]);
  assert.equal(loop.state, "thinking");
  assert.equal(starts, 0);
});

test("stale and duplicate callbacks are ignored", () => {
  const base = [{ type: "start" }, ...spokenTurn(1).slice(0, 3)]; // speaking turn 1
  const { loop, starts } = play([
    ...base,
    { type: "turn_finished", turn: 1, outcome: "response", spoken: true }, // duplicate
    { type: "playback_finished", turn: 0, ok: true }, // stale turn
    { type: "playback_finished", turn: 1, ok: true },
    { type: "playback_finished", turn: 1, ok: true }, // duplicate
  ]);
  assert.equal(loop.state, "listening");
  assert.equal(starts, 1);
});

test("after a spoken read-back the loop listens once for the answer; a click still resolves it", () => {
  const { loop, actions } = play([
    { type: "start" },
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "submitted" },
    { type: "turn_finished", turn: 1, outcome: "pending", spoken: true },
    { type: "playback_finished", turn: 1, ok: true }, // the read-back ended: listen for confirm/cancel
    { type: "confirmation_resolving" }, // the user clicked instead: the open recording is dropped
    { type: "turn_finished", turn: 2, outcome: "response", spoken: true },
    { type: "playback_finished", turn: 2, ok: true },
  ]);
  assert.equal(loop.state, "listening");
  assert.equal(loop.confirmationPending, false);
  assert.deepEqual(actions.map((a) => [a.type, a.after]), [
    ["start_listening", "playback_finished"], ["stop_listening", "confirmation_resolving"],
    ["start_listening", "playback_finished"],
  ]);
});

test("a pending action that was not read aloud is never listened for", () => {
  const { loop, starts } = play([
    { type: "start" }, { type: "recording_uploaded" }, { type: "transcript_handled", outcome: "submitted" },
    { type: "turn_finished", turn: 1, outcome: "pending", spoken: false },
  ]);
  assert.equal(loop.state, "awaiting_confirmation");
  assert.equal(starts, 0);
});

test("a spoken confirm or cancel resolves the action through the same request as the buttons", () => {
  for (const outcome of ["confirm_requested", "cancel_requested"]) {
    const { loop } = play([
      { type: "start" }, { type: "recording_uploaded" }, { type: "transcript_handled", outcome: "submitted" },
      { type: "turn_finished", turn: 1, outcome: "pending", spoken: true },
      { type: "playback_finished", turn: 1, ok: true },
      { type: "recording_uploaded" },
      { type: "transcript_handled", outcome },
      { type: "confirmation_resolving" }, // the page's resolve request; already thinking
    ]);
    assert.equal(loop.state, "thinking", outcome);
    assert.equal(loop.turn, 2);
    assert.equal(loop.confirmationPending, false);
  }
});

test("unclear answers are asked about once more, then the action waits for the buttons", () => {
  const { loop, starts } = play([
    { type: "start" }, { type: "recording_uploaded" }, { type: "transcript_handled", outcome: "submitted" },
    { type: "turn_finished", turn: 1, outcome: "pending", spoken: true },
    { type: "playback_finished", turn: 1, ok: true }, // listen (1)
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "guarded", spoken: true }, // "yes" -> "say confirm"
    { type: "playback_finished", turn: 1, ok: true }, // listen (2)
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "guarded", spoken: true }, // still not confirm/cancel
    { type: "playback_finished", turn: 1, ok: true },
  ]);
  assert.equal(loop.state, "awaiting_confirmation");
  assert.equal(loop.confirmationPending, true);
  assert.equal(starts, 2);
});

test("silence after the read-back keeps the action waiting without listening again", () => {
  const { loop, starts } = play([
    { type: "start" }, { type: "recording_uploaded" }, { type: "transcript_handled", outcome: "submitted" },
    { type: "turn_finished", turn: 1, outcome: "pending", spoken: true },
    { type: "playback_finished", turn: 1, ok: true },
    { type: "recording_discarded", reason: "no_speech" },
  ]);
  assert.equal(loop.state, "awaiting_confirmation");
  assert.equal(starts, 1);
});

test("a spoken yes during a confirmation never resumes or approves anything", () => {
  const awaiting = [
    { type: "start" },
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "submitted" },
    { type: "turn_finished", turn: 1, outcome: "pending", spoken: false },
  ];
  const { loop, starts } = play([
    ...awaiting,
    { type: "start" }, // user taps and says "yes"
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "guarded" },
  ]);
  assert.equal(loop.state, "awaiting_confirmation");
  assert.equal(starts, 0);
});

test("a spoken no cancels through the normal path and then resumes", () => {
  const { loop, starts } = play([
    { type: "start" },
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "submitted" },
    { type: "turn_finished", turn: 1, outcome: "pending", spoken: false },
    { type: "start" },
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "cancel_requested" },
    { type: "confirmation_resolving" },
    { type: "turn_finished", turn: 2, outcome: "response", spoken: false },
  ]);
  assert.equal(loop.state, "listening");
  assert.equal(starts, 1);
});

test("clicking Confirm/Cancel while listening drops that recording and follows the confirmation", () => {
  const { loop, actions, starts } = play([
    { type: "start" },
    { type: "recording_uploaded" },
    { type: "transcript_handled", outcome: "submitted" },
    { type: "turn_finished", turn: 1, outcome: "pending", spoken: false },
    { type: "start" }, // the user tapped the mic...
    { type: "confirmation_resolving" }, // ...then clicked Cancel instead
    { type: "recording_discarded", reason: "loop_stopped" }, // the dropped recording reports late
    { type: "turn_finished", turn: 2, outcome: "response", spoken: false },
  ]);
  assert.equal(loop.state, "listening");
  assert.equal(actions[0].type, "stop_listening");
  assert.equal(starts, 1);
});

test(`stops after ${MAX_CONSECUTIVE_SILENCES} silences in a row instead of retrying forever`, () => {
  const silence = { type: "recording_discarded", reason: "no_speech" };
  const { loop, actions } = play([{ type: "start" }, silence, silence, silence, silence]);
  assert.equal(loop.state, "off");
  assert.equal(actions.filter((a) => a.type === "start_listening").length, MAX_CONSECUTIVE_SILENCES - 1);
  assert.deepEqual(actions.at(-1), { type: "notify", message: MESSAGES.silence, after: "recording_discarded" });
});

test("a heard utterance resets the silence count", () => {
  const silence = { type: "recording_discarded", reason: "no_speech" };
  const { loop } = play([{ type: "start" }, silence, ...spokenTurn(1), silence]);
  assert.equal(loop.state, "listening");
});

test("every failure stops the loop with a message and no automatic retry", () => {
  const toListening = [{ type: "start" }];
  const toTranscribing = [...toListening, { type: "recording_uploaded" }];
  const toThinking = [...toTranscribing, { type: "transcript_handled", outcome: "submitted" }];
  const toSpeaking = [...toThinking, { type: "turn_finished", turn: 1, outcome: "response", spoken: true }];
  const cases = [
    [[...toListening, { type: "listen_failed" }], MESSAGES.listen_failed],
    [[...toListening, { type: "recording_discarded", reason: "track_ended" }], MESSAGES.recording_lost],
    [[...toTranscribing, { type: "transcription_failed" }], MESSAGES.transcription_failed],
    [[...toThinking, { type: "turn_finished", turn: 1, outcome: "error" }], MESSAGES.chat_failed],
    [[...toSpeaking, { type: "playback_finished", turn: 1, ok: false }], MESSAGES.playback_failed],
    [[...toTranscribing, { type: "transcript_handled", outcome: "kept_for_review" }], MESSAGES.kept_for_review],
  ];
  for (const [events, message] of cases) {
    const { loop, actions, starts } = play(events);
    assert.equal(loop.state, "off", message);
    assert.equal(starts, 0, message);
    assert.equal(actions.at(-1).message, message);
  }
});

test("switching Hands-free off or hiding the page stops everything; late events are ignored", () => {
  for (const stop of ["disable", "page_hidden"]) {
    for (const prefix of [[{ type: "start" }], [{ type: "start" }, ...spokenTurn(1).slice(0, 3)]]) {
      const { loop, actions, starts } = play([
        ...prefix,
        { type: stop },
        { type: "turn_finished", turn: 1, outcome: "response", spoken: false }, // reply arrives late
        { type: "playback_finished", turn: 1, ok: true },
        { type: "recording_discarded", reason: "no_speech" },
      ]);
      assert.equal(loop.state, "off");
      assert.equal(starts, 0);
      const stopActions = actions.filter((a) => a.after === stop).map((a) => a.type);
      assert.deepEqual(stopActions, ["stop_listening", "stop_playback"]);
    }
  }
  assert.deepEqual(reduce(createVoiceLoop(), { type: "disable" }).actions, []);
});

test("a recording the user cancels while the microphone opens ends the loop quietly", () => {
  const { loop, actions } = play([{ type: "start" }, { type: "stopped_by_user" }]);
  assert.equal(loop.state, "off");
  assert.deepEqual(actions, []);
});

test("typing and sending a message takes over from the loop", () => {
  const { loop, actions } = play([{ type: "start" }, ...spokenTurn(1), { type: "typed_message" }]);
  assert.equal(loop.state, "off");
  assert.deepEqual(actions.at(-1).type, "stop_listening");
});

test("random event sequences never listen unless the user started the loop", () => {
  const events = [
    { type: "start" }, { type: "disable" }, { type: "page_hidden" }, { type: "typed_message" },
    { type: "stopped_by_user" },
    { type: "listen_failed" }, { type: "recording_discarded", reason: "no_speech" },
    { type: "recording_discarded", reason: "page_hidden" }, { type: "recording_uploaded" },
    { type: "transcription_failed" }, { type: "transcript_handled", outcome: "submitted" },
    { type: "transcript_handled", outcome: "guarded" }, { type: "transcript_handled", outcome: "cancel_requested" },
    { type: "transcript_handled", outcome: "kept_for_review" }, { type: "confirmation_resolving" },
    { type: "turn_finished", outcome: "response", spoken: true }, { type: "turn_finished", outcome: "response", spoken: false },
    { type: "turn_finished", outcome: "pending", spoken: true }, { type: "turn_finished", outcome: "error" },
    { type: "playback_finished", ok: true }, { type: "playback_finished", ok: false },
  ];
  let seed = 42;
  const random = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
  for (let run = 0; run < 2000; run += 1) {
    let loop = createVoiceLoop();
    let startedSinceOff = false;
    for (let step = 0; step < 30; step += 1) {
      const template = events[Math.floor(random() * events.length)];
      const event = { ...template, turn: random() < 0.8 ? loop.turn : loop.turn - 1 };
      const before = loop.state;
      const result = reduce(loop, event);
      loop = result.loop;
      if (event.type === "start") startedSinceOff = true;
      for (const action of result.actions) {
        if (action.type === "start_listening") {
          assert.ok(startedSinceOff, "listening resumed without a user tap");
          assert.notEqual(before, "off");
          assert.ok(["speaking", "thinking", "listening"].includes(before), `resumed from ${before}`);
        }
      }
      if (loop.state === "off") startedSinceOff = false;
      assert.ok(loop.silences < MAX_CONSECUTIVE_SILENCES);
    }
  }
});

// --- Progress cue (createProgressCue): one short spoken line while a spoken request runs ---
const Loop = require(path.join(__dirname, "../../static/voice_loop.js"));

// Fake clock: schedule/cancel like setTimeout/clearTimeout; advance(ms) fires what is due.
function fakeTimers() {
  let now = 0;
  let next = 1;
  const pending = new Map();
  return {
    schedule: (callback, delay) => {
      const id = next++;
      pending.set(id, { at: now + delay, callback });
      return id;
    },
    cancel: (id) => pending.delete(id),
    advance(ms) {
      now += ms;
      for (const [id, timer] of [...pending].sort((a, b) => a[1].at - b[1].at)) {
        if (timer.at <= now && pending.delete(id)) timer.callback();
      }
    },
    get now() {
      return now;
    },
  };
}

// The page as the cue sees it: a spoken request in flight in a hands-free conversation.
function cuePage(overrides = {}) {
  const page = {
    facts: { sending: true, voiceTurn: true, speechOutput: true, speaking: false, recording: false,
      confirmationPending: false, loopState: "thinking", ...overrides },
    activity: { status: "thinking", tool: null },
    spoken: [],
  };
  const timers = fakeTimers();
  const cue = Loop.createProgressCue({
    schedule: timers.schedule, cancel: timers.cancel,
    check: () => (Loop.progressCueAllowed(page.facts) ? Loop.progressCueText(page.activity) : null),
    speak: (text) => {
      page.spoken.push({ text, at: timers.now });
      page.facts.speaking = true; // speakReply() owns the audio from here
    },
  });
  return { page, timers, cue };
}

test("a slow spoken request gets exactly one cue, at the threshold", () => {
  const { page, timers, cue } = cuePage();
  cue.start();
  timers.advance(Loop.PROGRESS_CUE_MS - 1);
  assert.deepEqual(page.spoken, []);
  timers.advance(1);
  assert.deepEqual(page.spoken, [{ text: "One moment.", at: 3000 }]);
  page.facts.speaking = false; // the cue ended; the request is still running
  timers.advance(20000);
  assert.equal(page.spoken.length, 1);
});

test("the cue names a read only while that read is running; otherwise it is neutral", () => {
  const { page, timers, cue } = cuePage();
  page.activity = { status: "running", tool: "get_briefing" };
  cue.start();
  timers.advance(3000);
  assert.deepEqual(page.spoken.map((s) => s.text), ["Checking your schedule."]);
  assert.equal(Loop.progressCueText({ status: "running", tool: "create_reminder" }), "One moment."); // writes are never announced
  assert.equal(Loop.progressCueText({ status: "thinking", tool: "get_briefing" }), "One moment.");
  assert.equal(Loop.progressCueText(null), "One moment.");
});

test("a request that ends before the threshold, or exactly at it, gets no cue", () => {
  const early = cuePage();
  early.cue.start();
  early.timers.advance(2000);
  early.cue.stop(); // the answer arrived
  early.timers.advance(5000);
  assert.deepEqual(early.page.spoken, []);

  // The timer fires in the same moment the request finishes: the fire-time check sees it ended.
  const late = cuePage();
  late.cue.start();
  late.page.facts.sending = false;
  late.timers.advance(3000);
  assert.deepEqual(late.page.spoken, []);
  assert.equal(late.cue.pending, false);
});

test("a cancelled or interrupted request gets no late cue", () => {
  for (const interruption of [
    (page, cue) => cue.stop(), // Stop / automation cancelled / hands-free off
    (page) => { page.facts.recording = true; }, // the user started talking again
    (page) => { page.facts.loopState = "off"; }, // the hands-free loop ended
    (page) => { page.facts.confirmationPending = true; }, // an approval is now waiting
  ]) {
    const { page, timers, cue } = cuePage();
    cue.start();
    timers.advance(1500);
    interruption(page, cue);
    timers.advance(5000);
    assert.deepEqual(page.spoken, []);
  }
});

test("the cue never starts over speech, so it cannot overlap the answer", () => {
  const { page, timers, cue } = cuePage({ speaking: true }); // something is already being spoken
  cue.start();
  timers.advance(5000);
  assert.deepEqual(page.spoken, []);
  // A new start (the next request) replaces the old timer: still one cue for the new request only.
  page.facts.speaking = false;
  cue.start();
  cue.start();
  timers.advance(3000);
  assert.equal(page.spoken.length, 1);
});

test("typed requests never get a cue", () => {
  const { page, timers, cue } = cuePage({ voiceTurn: false, loopState: "off" });
  cue.start(); // even if started, the check refuses a typed request
  timers.advance(10000);
  assert.deepEqual(page.spoken, []);
  assert.equal(Loop.progressCueAllowed({ sending: true, voiceTurn: true, speechOutput: false, loopState: "thinking" }), false);
});

test("a multi-step request with several tools still gets at most one cue", () => {
  const { page, timers, cue } = cuePage();
  cue.start();
  for (const tool of ["search_gmail", "read_gmail_message", "create_task", "get_briefing"]) {
    page.activity = { status: "running", tool };
    timers.advance(2500);
    page.facts.speaking = false;
  }
  assert.deepEqual(page.spoken.map((s) => s.text), ["Reading the email."]);
});
