// Wake word ("Kris", said like "Chris"): the DOM-free parts. static/app.js owns the microphone
// and carries out the actions returned here. Works in the browser (window.WakeWord) and in
// Node for tests (module.exports).
//
// Detection has two stages. In standby, createBurstDetector() watches the microphone level and
// reports only short, isolated sounds (a single word with silence around it); longer speech is
// ignored and never leaves the page. Each short sound is sent as a WAV clip to the local
// server (/api/voice/wake), where whisper.cpp decides whether it was just the name. Both stages
// are local; nothing is sent anywhere else.
//
// States (reduce):
//   off         wake word switched off; the microphone is not opened for it
//   starting    opening the microphone for standby
//   standby     listening for the wake word (level only; short sounds are checked)
//   checking    a short sound is being checked by the local server
//   activating  the wake word was heard: standby has released the microphone and the
//               acknowledgment is playing
//   active      the hands-free conversation owns the microphone
//   paused      standby is not allowed right now (page hidden, a recording or a spoken
//               reply is in progress); it reopens by itself when allowed again
//   denied      microphone permission was refused (switch it on again to retry)
//   error       the microphone failed or disconnected, or checks kept failing
// Only "enable" (the user switching it on) leaves off, denied, or error, so the page never
// re-enables wake listening by itself.
//
// Interrupting a spoken reply ("barge" states). While a hands-free reply is being spoken, the
// page sends "speech_started"; from active (a wake-word conversation) or paused (a conversation
// started by a tap) the same level detector listens again (barge_starting, barge_standby,
// barge_checking). Only short isolated sounds are checked, exactly as in standby; nothing is
// transcribed continuously. A confirmed name returns "interrupt" once and goes to active; the
// page then stops the reply and listens through the existing barge-in path. "speech_ended"
// (the reply finished or was stopped) returns to where it came from. A failed or busy check
// never touches the reply. Protection against K.R.I.S. hearing itself: the browser's echo
// cancellation on this microphone, no checks while the sentence being spoken mentions the name
// (mentionsWakeName), and the server accepting only a clip that is just the name.
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.WakeWord = api;
})(typeof self !== "undefined" ? self : this, () => {
  const SILENT_DB = -120;
  const BURST_TUNING = {
    calibrationMs: 500, // the noise floor is measured first
    onsetMarginDb: 8, // a sound: this far above the floor...
    onsetFrames: 2, // ...in this many of the last onsetWindow frames
    onsetWindow: 3,
    releaseMarginDb: 4, // quiet again: within this of the floor, or peakDropDb below the peak
    peakDropDb: 25,
    minPeakAboveFloorDb: 12, // quieter sounds are ignored
    minSoundMs: 150, // shorter: a click or a tap
    maxSoundMs: 1400, // longer: ordinary speech, not a lone name
    endSilenceMs: 400, // the silence after the name
    preRollMs: 300, // audio kept before the onset and after the end, so the word is whole
    postRollMs: 200,
  };

  function percentile(values, fraction) {
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * fraction))];
  }

  // update(levelDb, nowMs) returns null, or { startMs, endMs } for a short isolated sound
  // (times on the caller's clock, padded by the pre/post roll) that should be checked.
  // Sounds longer than maxSoundMs are skipped until the next silence.
  function createBurstDetector(options = {}) {
    const settings = { ...BURST_TUNING, ...options };
    const calibration = [];
    const recent = [];
    let startedAt = null;
    let floor = null;
    let onsetAt = null;
    let peak = SILENT_DB;
    let quietSince = null;

    function reset() {
      onsetAt = null;
      peak = SILENT_DB;
      quietSince = null;
      recent.length = 0;
    }

    function update(levelDb, nowMs) {
      const level = Number.isFinite(levelDb) ? Math.max(SILENT_DB, levelDb) : SILENT_DB;
      if (startedAt === null) startedAt = nowMs;
      if (floor === null) {
        calibration.push(level);
        if (nowMs - startedAt < settings.calibrationMs) return null;
        floor = percentile(calibration, 0.25);
        return null;
      }
      const loud = level >= floor + settings.onsetMarginDb;
      if (onsetAt === null) {
        recent.push({ loud, at: nowMs });
        if (recent.length > settings.onsetWindow) recent.shift();
        if (recent.filter((frame) => frame.loud).length >= settings.onsetFrames) {
          onsetAt = recent.find((frame) => frame.loud).at;
          peak = level;
        } else if (!loud) {
          // Follow a slowly changing room level while nothing is being said.
          floor = level < floor ? floor * 0.8 + level * 0.2 : floor * 0.98 + level * 0.02;
        }
        return null;
      }
      peak = Math.max(peak, level);
      const quietBelow = Math.max(floor + settings.releaseMarginDb, peak - settings.peakDropDb);
      if (level < quietBelow) {
        if (quietSince === null) quietSince = nowMs;
      } else {
        quietSince = null;
      }
      if (quietSince === null || nowMs - quietSince < settings.endSilenceMs) return null;
      const soundMs = quietSince - onsetAt;
      const result =
        soundMs >= settings.minSoundMs && soundMs <= settings.maxSoundMs &&
        peak >= floor + settings.minPeakAboveFloorDb
          ? { startMs: onsetAt - settings.preRollMs, endMs: quietSince + settings.postRollMs }
          : null;
      reset();
      return result;
    }

    return {
      update,
      reset,
      get noiseFloorDb() {
        return floor;
      },
      get hearing() {
        return onsetAt !== null;
      },
    };
  }

  // A fixed-length ring of recent samples, so a clip can include audio from before the onset.
  function createSampleRing(sampleRate, seconds) {
    const buffer = new Float32Array(Math.ceil(sampleRate * seconds));
    let written = 0; // total samples ever written
    return {
      sampleRate,
      push(samples) {
        for (let index = 0; index < samples.length; index += 1) {
          buffer[(written + index) % buffer.length] = samples[index];
        }
        written += samples.length;
      },
      get written() {
        return written;
      },
      // Samples [fromSample, toSample) by absolute index, clamped to what is still held.
      slice(fromSample, toSample) {
        const from = Math.max(fromSample, written - buffer.length, 0);
        const to = Math.min(toSample, written);
        const out = new Float32Array(Math.max(0, to - from));
        for (let index = 0; index < out.length; index += 1) out[index] = buffer[(from + index) % buffer.length];
        return out;
      },
    };
  }

  // Averages blocks of samples down to targetRate (16 kHz for whisper.cpp).
  function downsample(samples, fromRate, targetRate = 16000) {
    if (fromRate <= targetRate) return samples;
    const ratio = fromRate / targetRate;
    const out = new Float32Array(Math.floor(samples.length / ratio));
    for (let index = 0; index < out.length; index += 1) {
      const start = Math.floor(index * ratio);
      const end = Math.min(samples.length, Math.floor((index + 1) * ratio));
      let sum = 0;
      for (let at = start; at < end; at += 1) sum += samples[at];
      out[index] = end > start ? sum / (end - start) : 0;
    }
    return out;
  }

  // 16-bit PCM mono WAV bytes.
  function encodeWav(samples, sampleRate) {
    const bytes = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(bytes);
    const text = (offset, value) => [...value].forEach((char, index) => view.setUint8(offset + index, char.charCodeAt(0)));
    text(0, "RIFF");
    view.setUint32(4, 36 + samples.length * 2, true);
    text(8, "WAVE");
    text(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    text(36, "data");
    view.setUint32(40, samples.length * 2, true);
    for (let index = 0; index < samples.length; index += 1) {
      const value = Math.max(-1, Math.min(1, samples[index]));
      view.setInt16(44 + index * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
    }
    return new Uint8Array(bytes);
  }

  const MAX_CHECK_FAILURES = 3;
  const MESSAGES = {
    standby: "Standby: say “Kris” to start talking.",
    denied: "Wake word is off: microphone access was denied. Allow the microphone for this page, then switch Wake word on again.",
    unavailable: "Wake word is off: the microphone could not start.",
    lost: "Wake word is off: the microphone stopped or was disconnected. Switch it on again to resume.",
    checks_failed: "Wake word is off: local speech recognition is not responding.",
    unsupported: "Wake word needs audio processing this browser does not provide.",
  };
  const CAPTURE_STATES = new Set(["starting", "standby", "checking"]);
  const BARGE_STATES = new Set(["barge_starting", "barge_standby", "barge_checking"]);
  const ENABLED = new Set(["starting", "standby", "checking", "activating", "active", "paused", ...BARGE_STATES]);

  function createWakeState() {
    return { state: "off", failures: 0, message: null };
  }

  const to = (wake, state, actions = [], changes = {}) => ({ wake: { ...wake, message: null, ...changes, state }, actions });
  // Leave the barge states for where the reply started (active or paused), releasing the microphone.
  const back = (wake) => to(wake, wake.returnTo || "paused", [{ type: "stop_capture" }], { returnTo: null });
  const ignore = (wake) => ({ wake, actions: [] });
  const fail = (wake, state, key) => ({
    wake: { ...wake, state, failures: 0, message: MESSAGES[key] },
    actions: [{ type: "stop_capture" }, { type: "notify", message: MESSAGES[key] }],
  });

  // Returns { wake, actions }. Actions: open_capture, stop_capture, check_clip, acknowledge,
  // start_conversation, notify { message }. Events that do not fit the state are ignored.
  function reduce(wake, event) {
    const state = wake.state;
    if (BARGE_STATES.has(state)) {
      switch (event.type) {
        case "capture_started":
          return state === "barge_starting" ? to(wake, "barge_standby") : ignore(wake);
        case "capture_failed": // cannot listen during this reply: the reply simply continues
        case "capture_lost":
        case "suspend":
        case "speech_ended":
          return back(wake);
        case "burst":
          return state === "barge_standby" ? to(wake, "barge_checking", [{ type: "check_clip", clip: event.clip }]) : ignore(wake);
        case "check_result":
          if (state !== "barge_checking") return ignore(wake);
          if (event.wake) return to(wake, "active", [{ type: "stop_capture" }, { type: "interrupt" }], { returnTo: null, failures: 0 });
          return to(wake, "barge_standby");
        case "check_failed": // a failed check never stops the reply or disables the wake word
          return state === "barge_checking" ? to(wake, "barge_standby") : ignore(wake);
        case "disable":
          return to(wake, "off", [{ type: "stop_capture" }], { failures: 0, returnTo: null });
        default:
          return ignore(wake);
      }
    }
    switch (event.type) {
      case "speech_started": // a hands-free reply began; listen for the name while it plays
        return state === "active" || state === "paused"
          ? to(wake, "barge_starting", [{ type: "open_capture" }], { returnTo: state })
          : ignore(wake);
      case "enable": // the user switched Wake word on
        if (ENABLED.has(state)) return ignore(wake);
        return to(wake, event.allowed ? "starting" : "paused", event.allowed ? [{ type: "open_capture" }] : [], { failures: 0 });
      case "disable": // the user switched it off (or Hands-free off)
        if (!ENABLED.has(state) && !wake.message) return ignore(wake);
        return to(wake, "off", ENABLED.has(state) ? [{ type: "stop_capture" }] : [], { failures: 0 });
      case "unsupported":
        return fail(wake, "error", "unsupported");
      case "capture_started":
        return state === "starting" ? to(wake, "standby", [{ type: "notify", message: MESSAGES.standby }]) : ignore(wake);
      case "capture_failed": // event.denied: permission refused
        return state === "starting" ? fail(wake, event.denied ? "denied" : "error", event.denied ? "denied" : "unavailable") : ignore(wake);
      case "capture_lost": // the track ended (unplugged, sleep, permission revoked)
        return CAPTURE_STATES.has(state) ? fail(wake, "error", "lost") : ignore(wake);
      case "suspend": // standby is not allowed right now
        return CAPTURE_STATES.has(state) ? to(wake, "paused", [{ type: "stop_capture" }]) : ignore(wake);
      case "resume": // allowed again
        return state === "paused" ? to(wake, "starting", [{ type: "open_capture" }]) : ignore(wake);
      case "burst": // a short isolated sound in standby
        return state === "standby" ? to(wake, "checking", [{ type: "check_clip", clip: event.clip }]) : ignore(wake);
      case "check_result": // event.wake: the server heard just the name
        if (state !== "checking") return ignore(wake);
        if (event.wake) return to(wake, "activating", [{ type: "stop_capture" }, { type: "acknowledge" }], { failures: 0 });
        return to(wake, "standby", [], { failures: 0 });
      case "check_failed": {
        if (state !== "checking") return ignore(wake);
        const failures = wake.failures + 1;
        if (failures >= MAX_CHECK_FAILURES) return fail(wake, "error", "checks_failed");
        return to(wake, "standby", [], { failures });
      }
      case "acknowledged": // the acknowledgment finished (or could not be spoken)
        return state === "activating" ? to(wake, "active", [{ type: "start_conversation" }]) : ignore(wake);
      case "conversation_ended": // the hands-free loop stopped; go back to standby
        if (state !== "active" && state !== "activating") return ignore(wake);
        return event.allowed ? to(wake, "starting", [{ type: "open_capture" }]) : to(wake, "paused");
      default:
        return ignore(wake);
    }
  }

  // Whether standby may hold the microphone, from page facts (all booleans):
  //   visible, recording (a conversation recording is open or opening), transcribing,
  //   speaking (any spoken reply), conversation (the hands-free loop is running).
  function standbyAllowed(facts) {
    return Boolean(facts.visible) && !facts.recording && !facts.transcribing && !facts.speaking && !facts.conversation;
  }

  // What K.R.I.S. says when it hears its name: short (quick to synthesize and over before the
  // microphone opens), composed, and varied so repeated use does not sound canned. None
  // contains the name, and standby has already released the microphone while it plays, so an
  // acknowledgement can never wake K.R.I.S. again. The catchphrase is never used here.
  const ACKNOWLEDGEMENTS = Object.freeze([
    "Yes?", "I'm listening.", "Go ahead.", "I'm here.", "What do you need?",
    "Ready when you are.", "How can I help?", "Listening.",
  ]);
  const ACK_HISTORY = 3; // the last few are not repeated

  // Pick an acknowledgement that is not among the recent ones. random() returns [0, 1).
  // Returns { text, recent } with recent updated, for the next call.
  function pickAcknowledgement(recent = [], random = Math.random) {
    const fresh = ACKNOWLEDGEMENTS.filter((text) => !recent.includes(text));
    const choices = fresh.length ? fresh : ACKNOWLEDGEMENTS;
    const text = choices[Math.min(choices.length - 1, Math.floor(random() * choices.length))];
    return { text, recent: [...recent, text].slice(-ACK_HISTORY) };
  }

  // Whether a spoken reply may be interrupted by the name, from page facts (all booleans):
  //   visible, handsFree, loopSpeaking (the hands-free loop is speaking a reply), recording,
  //   transcribing.
  function bargeAllowed(facts) {
    return Boolean(facts.visible && facts.handsFree && facts.loopSpeaking) && !facts.recording && !facts.transcribing;
  }

  // True when text being spoken contains the name (as the server recognises it), so a check
  // during it could be K.R.I.S. hearing itself.
  const NAME_PATTERN = /\b(?:k\.?\s?r\.?\s?i\.?\s?s\.?|kris|chris|cris|kriss|chriss|krys)\b/i;
  function mentionsWakeName(text) {
    return NAME_PATTERN.test(String(text || ""));
  }

  const capturing = (wake) => CAPTURE_STATES.has(wake.state);
  const barging = (wake) => BARGE_STATES.has(wake.state);
  const enabled = (wake) => ENABLED.has(wake.state);

  return {
    createBurstDetector, createSampleRing, downsample, encodeWav,
    createWakeState, reduce, standbyAllowed, bargeAllowed, mentionsWakeName, capturing, barging, enabled,
    pickAcknowledgement,
    BURST_TUNING, MESSAGES, MAX_CHECK_FAILURES, ACKNOWLEDGEMENTS, ACK_HISTORY,
  };
});
