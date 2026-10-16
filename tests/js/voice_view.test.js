// Run with: node --test tests/js/
// Voice indicator states (viewState) and the Stop Speaking control (stop_speaking).
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { createVoiceLoop, reduce, viewState, VIEW_LABELS, MESSAGES } = require(
  path.join(__dirname, "../../static/voice_loop.js"),
);

function play(events, loop = createVoiceLoop()) {
  const actions = [];
  for (const event of events) {
    const result = reduce(loop, event);
    loop = result.loop;
    actions.push(...result.actions.map((action) => ({ ...action, after: event.type })));
  }
  const count = (type) => actions.filter((action) => action.type === type).length;
  return { loop, actions, starts: count("start_listening"), stops: count("stop_playback") };
}

const toSpeaking = [
  { type: "start" },
  { type: "recording_uploaded" },
  { type: "transcript_handled", outcome: "submitted" },
  { type: "turn_finished", turn: 1, outcome: "response", spoken: true },
];

test("every visible state has a label", () => {
  for (const state of ["listening", "transcribing", "thinking", "speaking", "paused", "error"]) {
    assert.ok(VIEW_LABELS[state], state);
  }
  assert.equal(VIEW_LABELS.paused, "Hands-free paused");
  assert.equal(VIEW_LABELS.idle, "");
});

test("each state is shown from the loop and page facts", () => {
  const off = createVoiceLoop();
  const cases = [
    [off, {}, "idle"],
    [off, { recording: true }, "listening"], // review-mode recording
    [off, { transcribing: true }, "transcribing"],
    [off, { busy: true }, "thinking"],
    [off, { speaking: true }, "speaking"], // review mode with Speak replies on
    [off, { error: true }, "error"],
    [play([{ type: "start" }]).loop, {}, "listening"], // hands-free, microphone still opening
    [play(toSpeaking.slice(0, 2)).loop, {}, "transcribing"],
    [play(toSpeaking.slice(0, 3)).loop, {}, "thinking"],
    [play(toSpeaking).loop, {}, "speaking"],
    [play([...toSpeaking.slice(0, 3), { type: "turn_finished", turn: 1, outcome: "pending" }]).loop, {}, "awaiting_confirmation"],
    [play([{ type: "start" }, { type: "recording_discarded", reason: "no_speech" }, { type: "recording_discarded", reason: "no_speech" }]).loop, {}, "paused"],
    [play([...toSpeaking.slice(0, 3), { type: "turn_finished", turn: 1, outcome: "error" }]).loop, {}, "error"],
    [play([{ type: "start" }, { type: "listen_failed" }]).loop, {}, "error"],
  ];
  for (const [loop, facts, expected] of cases) {
    const view = viewState(loop, facts);
    assert.equal(view.state, expected, `${loop.state} ${JSON.stringify(facts)}`);
    assert.equal(view.label, VIEW_LABELS[expected]);
  }
});

test("a spoken prompt or error shows Speaking and allows Stop Speaking in any loop state", () => {
  const awaiting = play([...toSpeaking.slice(0, 3), { type: "turn_finished", turn: 1, outcome: "pending", spoken: true }]).loop;
  for (const loop of [createVoiceLoop(), awaiting, play(toSpeaking).loop]) {
    const view = viewState(loop, { speaking: true });
    assert.equal(view.state, "speaking");
    assert.equal(view.canStopSpeaking, true);
  }
  assert.equal(viewState(play(toSpeaking).loop, {}).canStopSpeaking, false); // nothing playing yet/anymore
});

test("a normal hands-free turn moves through the visible states in order", () => {
  let loop = createVoiceLoop();
  const seen = [viewState(loop).state];
  for (const event of [...toSpeaking, { type: "playback_finished", turn: 1, ok: true }]) {
    loop = reduce(loop, event).loop;
    seen.push(viewState(loop).state);
  }
  assert.deepEqual(seen, ["idle", "listening", "transcribing", "thinking", "speaking", "listening"]);
});

test("Stop Speaking while a hands-free reply plays resumes listening exactly once", () => {
  const { loop, actions, starts, stops } = play([...toSpeaking, { type: "stop_speaking" }]);
  assert.equal(loop.state, "listening");
  assert.equal(starts, 1);
  assert.equal(stops, 1);
  assert.deepEqual(actions.map((a) => a.type), ["stop_playback", "start_listening"]);
});

test("the stale playback-ended callback after Stop Speaking is ignored", () => {
  const { loop, starts } = play([
    ...toSpeaking,
    { type: "stop_speaking" },
    { type: "playback_finished", turn: 1, ok: true }, // the cancelled audio's promise resolves late
    { type: "playback_finished", turn: 1, ok: false },
  ]);
  assert.equal(loop.state, "listening");
  assert.equal(starts, 1);
  assert.equal(loop.notice, null);
});

test("pressing Stop Speaking twice, or when nothing is speaking, never starts listening", () => {
  const twice = play([...toSpeaking, { type: "stop_speaking" }, { type: "stop_speaking" }]);
  assert.equal(twice.starts, 1);
  for (const prefix of [[], [{ type: "start" }], toSpeaking.slice(0, 3)]) {
    const { starts } = play([...prefix, { type: "stop_speaking" }]);
    assert.equal(starts, 0, JSON.stringify(prefix));
  }
});

test("in Review mode Stop Speaking only stops playback", () => {
  const { loop, actions } = play([{ type: "stop_speaking" }]); // loop off: Review mode
  assert.equal(loop.state, "off");
  assert.deepEqual(actions.map((a) => a.type), ["stop_playback"]);
});

test("Stop Speaking during a spoken confirmation prompt does not start listening", () => {
  const { loop, starts } = play([
    ...toSpeaking.slice(0, 3),
    { type: "turn_finished", turn: 1, outcome: "pending", spoken: true },
    { type: "stop_speaking" },
    { type: "playback_finished", turn: 1, ok: true },
  ]);
  assert.equal(loop.state, "awaiting_confirmation");
  assert.equal(starts, 0);
});

test("confirmation flows still resume only after the reply (unchanged)", () => {
  const { loop, starts } = play([
    ...toSpeaking.slice(0, 3),
    { type: "turn_finished", turn: 1, outcome: "pending", spoken: true },
    { type: "confirmation_resolving" },
    { type: "turn_finished", turn: 2, outcome: "response", spoken: true },
    { type: "stop_speaking" }, // the user stops the cancellation reply
    { type: "playback_finished", turn: 2, ok: true },
  ]);
  assert.equal(loop.state, "listening");
  assert.equal(starts, 1);
});

test("switching Hands-free off or hiding the page while speaking stops everything", () => {
  for (const type of ["disable", "page_hidden"]) {
    const { loop, actions, starts } = play([
      ...toSpeaking,
      { type },
      { type: "stop_speaking" },
      { type: "playback_finished", turn: 1, ok: true },
    ]);
    assert.equal(loop.state, "off");
    assert.equal(starts, 0);
    assert.deepEqual(actions.filter((a) => a.after === type).map((a) => a.type), ["stop_listening", "stop_playback"]);
    assert.equal(viewState(loop).state, "idle");
  }
});

test("paused and error notices clear when the user starts again or turns Hands-free off", () => {
  const paused = play([{ type: "start" }, { type: "listen_failed" }]).loop;
  assert.equal(paused.notice.kind, "error");
  assert.equal(paused.notice.message, MESSAGES.listen_failed);
  assert.equal(reduce(paused, { type: "start" }).loop.notice, null);
  assert.equal(reduce(paused, { type: "disable" }).loop.notice, null);
  assert.deepEqual(reduce(paused, { type: "disable" }).actions, []);
  const kept = play([...toSpeaking.slice(0, 2), { type: "transcript_handled", outcome: "kept_for_review" }]).loop;
  assert.equal(kept.notice.kind, "paused");
});
