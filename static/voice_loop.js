// Hands-free listening loop: the single owner of "what happens next" in a hands-free
// conversation. A small, deterministic, DOM-free state machine; static/app.js reports
// events to it and carries out the actions it returns. Works in the browser
// (window.VoiceLoop) and in Node for tests (module.exports).
//
// States:
//   off                    no loop; nothing starts listening by itself
//   listening              a hands-free recording is open (end-of-speech detection decides)
//   transcribing           the recording is being transcribed
//   thinking               a chat request or confirmation is being processed
//   speaking               the reply is being spoken
//   awaiting_confirmation  an action waits for Confirm/Cancel; no automatic listening
//
// The loop only ever starts from a user tap ("start"). It resumes listening only after a
// completed reply (when its playback ends, when the user stops it with Stop Speaking, or at
// once if nothing is spoken), and stops on any failure, on Hands-free being switched off, on
// the page being hidden, or after repeated silence, so it can never retry by itself in a loop.
//
// viewState() derives what the voice indicator shows from the loop plus a few page facts; it
// is read-only, so the UI cannot move the loop into an invalid state.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VoiceLoop = api;
})(typeof self !== "undefined" ? self : this, () => {
  const MAX_CONSECUTIVE_SILENCES = 2;
  const PAUSED = "Hands-free paused. Tap the mic to continue.";
  const MESSAGES = {
    silence: `Didn't hear anything. ${PAUSED}`,
    listen_failed: `The microphone could not start. ${PAUSED}`,
    transcription_failed: `The recording could not be transcribed. ${PAUSED}`,
    chat_failed: `U could not answer. ${PAUSED}`,
    playback_failed: `The reply could not be spoken. ${PAUSED}`,
    recording_lost: `The recording stopped. ${PAUSED}`,
    kept_for_review: "U was still busy, so your words are in the message box. Press Enter to send them.",
  };
  const ACTIVE = new Set(["listening", "transcribing", "thinking", "speaking", "awaiting_confirmation"]);
  // Why the loop last stopped by itself: silence or a busy page is a pause, anything else an error.
  const PAUSE_KINDS = { silence: "paused", kept_for_review: "paused" };

  function createVoiceLoop() {
    return { state: "off", silences: 0, turn: 0, notice: null };
  }

  const off = (loop, actions = [], notice = null) => ({
    loop: { ...loop, state: "off", silences: 0, notice },
    actions,
  });
  const go = (loop, state, actions = [], changes = {}) => ({
    loop: { ...loop, ...changes, state, notice: null },
    actions,
  });
  const ignore = (loop) => ({ loop, actions: [] });
  const pause = (loop, key, extra = []) =>
    off(loop, [...extra, { type: "notify", message: MESSAGES[key] }], {
      kind: PAUSE_KINDS[key] || "error",
      message: MESSAGES[key],
    });

  // Returns { loop, actions }. Actions: start_listening, stop_listening (discard the open
  // recording), stop_playback, notify { message }. Events that do not fit the current state
  // (late callbacks from an earlier turn) are ignored.
  function reduce(loop, event) {
    const active = ACTIVE.has(loop.state);
    switch (event.type) {
      case "start": // the user tapped the mic in Hands-free mode
        return go(loop, "listening", [], { silences: 0 });

      case "disable": // Hands-free switched off (also clears any paused/error notice)
      case "page_hidden":
        if (active) return off(loop, [{ type: "stop_listening" }, { type: "stop_playback" }]);
        return event.type === "disable" && loop.notice ? off(loop) : ignore(loop);

      case "stop_speaking": // the user pressed Stop Speaking
        // Always stop the audio. Only a hands-free reply being spoken moves on to listening,
        // exactly once: the playback promise that resolves afterwards finds the loop no
        // longer "speaking" and is ignored.
        if (loop.state === "speaking") {
          return go(loop, "listening", [{ type: "stop_playback" }, { type: "start_listening" }]);
        }
        return { loop, actions: [{ type: "stop_playback" }] };

      case "typed_message": // the user typed and sent a message: typing takes over
        return active ? off(loop, [{ type: "stop_listening" }]) : ignore(loop);

      case "stopped_by_user": // the user cancelled a recording while the microphone was opening
        return loop.state === "listening" ? off(loop) : ignore(loop);

      case "listen_failed":
        return loop.state === "listening" ? pause(loop, "listen_failed") : ignore(loop);

      case "recording_discarded": {
        if (loop.state !== "listening") return ignore(loop);
        if (event.reason !== "no_speech") return pause(loop, "recording_lost");
        const silences = loop.silences + 1;
        if (silences >= MAX_CONSECUTIVE_SILENCES) return pause(loop, "silence");
        return go(loop, "listening", [{ type: "start_listening" }], { silences });
      }

      case "recording_uploaded":
        return loop.state === "listening" ? go(loop, "transcribing") : ignore(loop);

      case "transcription_failed":
        return loop.state === "transcribing" ? pause(loop, "transcription_failed") : ignore(loop);

      case "transcript_handled": // outcome: submitted | cancel_requested | guarded | kept_for_review
        if (loop.state !== "transcribing") return ignore(loop);
        if (event.outcome === "submitted" || event.outcome === "cancel_requested") {
          return go(loop, "thinking", [], { silences: 0, turn: loop.turn + 1 });
        }
        if (event.outcome === "guarded") return go(loop, "awaiting_confirmation", [], { silences: 0 });
        return pause(loop, "kept_for_review");

      case "confirmation_resolving": // Confirm/Cancel clicked, or a spoken decline
        if (loop.state === "thinking") return ignore(loop); // a spoken decline already moved here
        if (loop.state === "awaiting_confirmation") return go(loop, "thinking", [], { turn: loop.turn + 1 });
        if (loop.state === "listening") {
          // Confirm/Cancel clicked while a hands-free recording was open: drop the recording.
          return go(loop, "thinking", [{ type: "stop_listening" }], { turn: loop.turn + 1 });
        }
        return ignore(loop);

      case "turn_finished": // outcome: response | pending | error; spoken: is it being spoken
        if (loop.state !== "thinking" || event.turn !== loop.turn) return ignore(loop);
        if (event.outcome === "error") return pause(loop, "chat_failed");
        if (event.outcome === "pending") return go(loop, "awaiting_confirmation");
        if (event.spoken) return go(loop, "speaking");
        return go(loop, "listening", [{ type: "start_listening" }]);

      case "playback_finished": // ok: playback ended normally (false: it failed)
        if (loop.state !== "speaking" || event.turn !== loop.turn) return ignore(loop);
        return event.ok ? go(loop, "listening", [{ type: "start_listening" }]) : pause(loop, "playback_failed");

      default:
        return ignore(loop);
    }
  }

  // What the voice indicator shows. facts (all booleans, from the page):
  //   speaking      a spoken reply is being prepared or played (Stop Speaking applies)
  //   recording     a recording is open (or the microphone is opening)
  //   transcribing  a recording is being transcribed
  //   busy          a chat or confirmation request is in flight
  //   error         an error message is showing
  // While the hands-free loop runs, its state wins; otherwise (Review mode, or the loop is
  // off) the page facts decide. Returns { state, label, canStopSpeaking }.
  const VIEW_LABELS = {
    idle: "",
    listening: "Listening",
    transcribing: "Transcribing",
    thinking: "Thinking",
    speaking: "Speaking",
    awaiting_confirmation: "Waiting for confirmation",
    paused: "Hands-free paused",
    error: "Error",
  };

  function viewState(loop, facts = {}) {
    let state;
    if (facts.speaking) state = "speaking";
    else if (ACTIVE.has(loop.state)) state = loop.state;
    else if (facts.recording) state = "listening";
    else if (facts.transcribing) state = "transcribing";
    else if (facts.busy) state = "thinking";
    else if (loop.notice) state = loop.notice.kind;
    else if (facts.error) state = "error";
    else state = "idle";
    return { state, label: VIEW_LABELS[state], canStopSpeaking: Boolean(facts.speaking) };
  }

  return { createVoiceLoop, reduce, viewState, VIEW_LABELS, MAX_CONSECUTIVE_SILENCES, MESSAGES };
});
