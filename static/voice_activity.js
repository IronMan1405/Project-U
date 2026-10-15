// End-of-speech detection for hands-free recordings.
//
// A small, deterministic, DOM-free state machine fed one audio level (dBFS) per frame. It is
// energy based, so it cannot tell speech from other sounds: background noise or voices may
// keep a recording open until the maximum duration, and very quiet speech close to the noise
// floor may not be recognised as speech. The server-side silence check still applies to every
// upload. Works in the browser (window.VoiceActivity) and in Node for tests (module.exports).
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.VoiceActivity = api;
})(typeof self !== "undefined" ? self : this, () => {
  const SILENT_DB = -120;
  const TUNING = {
    calibrationMs: 300, // noise floor is measured over the start of each recording
    onsetMarginDb: 6, // speech: this far above the floor...
    onsetFrames: 3, // ...in at least this many of the last onsetWindow frames
    onsetWindow: 5,
    releaseMarginDb: 3, // quiet again: within this of the floor...
    peakDropDb: 30, // ...or this far below the loudest speech so far, whichever is higher
    recalibrateDropDb: 12, // a level this far below the floor means calibration heard speech
  };

  // RMS level in dBFS of float samples in [-1, 1]; SILENT_DB for digital silence.
  function rmsDb(samples) {
    let sum = 0;
    for (let index = 0; index < samples.length; index += 1) sum += samples[index] * samples[index];
    const rms = samples.length ? Math.sqrt(sum / samples.length) : 0;
    return rms > 0 ? Math.max(SILENT_DB, 20 * Math.log10(rms)) : SILENT_DB;
  }

  function percentile(values, fraction) {
    const sorted = [...values].sort((a, b) => a - b);
    return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * fraction))];
  }

  // options: endSilenceMs, minRecordingMs, noSpeechTimeoutMs, maxMs (all required numbers).
  // update(levelDb, elapsedMs) returns null to keep recording, or a final decision:
  //   { action: "stop", reason: "end_of_speech" | "max_duration" }  -> upload the recording
  //   { action: "discard", reason: "no_speech" }                     -> nothing was heard
  function createEndOfSpeechDetector(options) {
    for (const key of ["endSilenceMs", "minRecordingMs", "noSpeechTimeoutMs", "maxMs"]) {
      if (!Number.isFinite(options?.[key]) || options[key] <= 0) throw new Error(`${key} must be a positive number`);
    }
    const settings = { ...TUNING, ...options };
    const calibration = [];
    const recent = [];
    let floor = null;
    let calibrationPeak = SILENT_DB;
    let speechStarted = false;
    let peak = SILENT_DB;
    let quietSince = null;
    let decision = null;

    function decide(result) {
      decision = result;
      return result;
    }

    function update(levelDb, elapsedMs) {
      if (decision) return null; // a decision is final; later frames are ignored
      const level = Number.isFinite(levelDb) ? Math.max(SILENT_DB, levelDb) : SILENT_DB;

      if (floor === null) {
        calibration.push(level);
        if (elapsedMs < settings.calibrationMs) {
          return elapsedMs >= settings.maxMs ? decide({ action: "stop", reason: "max_duration" }) : null;
        }
        // A low percentile, so speech that starts right away does not inflate the floor.
        floor = percentile(calibration, 0.25);
        calibrationPeak = Math.max(...calibration);
        // A short word said entirely within the calibration window still counts as speech.
        if (calibration.filter((value) => value >= floor + settings.onsetMarginDb).length >= settings.onsetFrames) {
          speechStarted = true;
          peak = calibrationPeak;
        }
      }

      if (!speechStarted && level <= floor - settings.recalibrateDropDb) {
        // The calibration window was speech (the user started talking immediately): the
        // real floor is lower. Re-baseline, and count that earlier sound as speech.
        floor = level;
        if (calibrationPeak >= floor + settings.onsetMarginDb) {
          speechStarted = true;
          peak = calibrationPeak;
        }
      }

      const loud = level >= floor + settings.onsetMarginDb;
      if (!speechStarted) {
        recent.push(loud);
        if (recent.length > settings.onsetWindow) recent.shift();
        if (recent.filter(Boolean).length >= settings.onsetFrames) {
          speechStarted = true;
        } else if (!loud) {
          // Track a slowly changing noise floor while waiting for speech.
          floor = level < floor ? floor * 0.8 + level * 0.2 : floor * 0.98 + level * 0.02;
        }
      }

      if (speechStarted) {
        peak = Math.max(peak, level);
        const quietBelow = Math.max(floor + settings.releaseMarginDb, peak - settings.peakDropDb);
        if (level < quietBelow) {
          if (quietSince === null) quietSince = elapsedMs;
        } else {
          quietSince = null;
        }
        if (
          quietSince !== null &&
          elapsedMs - quietSince >= settings.endSilenceMs &&
          elapsedMs >= settings.minRecordingMs
        ) {
          return decide({ action: "stop", reason: "end_of_speech" });
        }
      } else if (elapsedMs >= settings.noSpeechTimeoutMs) {
        return decide({ action: "discard", reason: "no_speech" });
      }

      if (elapsedMs >= settings.maxMs) return decide({ action: "stop", reason: "max_duration" });
      return null;
    }

    return {
      update,
      get speechStarted() {
        return speechStarted;
      },
      get noiseFloorDb() {
        return floor;
      },
    };
  }

  return { createEndOfSpeechDetector, rmsDb, SILENT_DB, TUNING };
});
