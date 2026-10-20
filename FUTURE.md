# Project U — Future Work and Known Limitations

Items here are deliberately out of scope for v1.0. Listing an idea is not a
commitment to build it; each would need its own approval under the scope rules
in `TODO.md`.

## Known limitations in v1.0

### Tasks and due dates
- Assistant due times are interpreted in this Mac's current system time zone.
  Times that a daylight-saving change skips or repeats are rejected; the user
  must give an explicit UTC offset for those.
- Date-only due dates are local calendar dates with no time; "Overdue" is
  computed from the browser's clock.
- Editing a due time in the web UI works in minutes, so seconds are dropped.
- Tasks are not sorted or filtered by due date, and there is no "due today" state.

### Computer utilities
- `find_files` examines at most 5,000 entries per search (shared across
  approved roots) and four levels of depth. Large roots such as a full Desktop
  or Projects folder can be cut short; results say which roots were incomplete.
- Matching is word-based substring matching, so a query can match across word
  boundaries (for example `cesh` matches `PerformanceSheet`).
- `open_file` re-checks the path immediately before launching, but a very short
  window remains between that check and macOS opening the file.
- `get_system_info` finds `pmset`, `sysctl`, and `vm_stat` through `PATH`,
  unlike the fixed executable paths used by the open tools and voice.
- Computer utilities and voice are macOS-only.

### Voice
- The `ggml-base.en` model is English-only; accuracy depends on the speaker and
  room noise.
- Safari may block autoplay of spoken replies; the page then reports that the
  spoken reply is unavailable.
- Long replies are spoken only up to about 4,000 characters.
- Spoken replies start after the whole model reply is ready (the chat request is
  not streamed); streaming the model's text into the sentence pipeline would cut
  the wait further.
- Kokoro: one voice per server (`kokoro_voice`); English only as configured;
  pronunciation comes from espeak-ng, so unusual names (for example "Siobhan")
  and acronyms can be read oddly (a small pronunciation list could fix specific
  words); "K.R.I.S." is spelled out; dates such as "12 March" are read as
  written; a sentence being synthesized when you stop finishes on the server (well
  under a second) before it is discarded; a short first sentence followed by a
  long one can leave a brief extra pause. The bundled espeak-ng and phonemizer
  are GPL-3.0 (fine for personal use; matters if K.R.I.S. is ever distributed).
- MLX Kokoro was evaluated and not used: its text front end needs spaCy, which
  does not install on the project's Python 3.14 (nor 3.13 here).
- The terminal client has no voice support.
- The silence check uses a fixed threshold (`SPEECH_LEVEL_DBFS`, −57 dBFS for at
  least 0.12 s) chosen from one quiet room; a noisier room or a very quiet voice
  may need it adjusted. Brief noises can pass it, after which whisper.cpp may
  still occasionally return a stray word.
- Short spoken yes/no replies during a confirmation are recognised from a fixed
  phrase list, so unusual phrasings are treated as ordinary messages (which
  cancel the waiting action rather than approve it).
- Speech is not streamed: a spoken reply starts only after the whole reply is
  ready, and you cannot speak while U is generating a reply.
- Hands-free end-of-speech detection is an energy check against each recording's
  starting noise level, not speech recognition: steady background sound can keep
  a recording open until `max_seconds`, speech not clearly louder than steady
  noise (about 6 dB) or very close to the room's noise level can be discarded as
  "no speech", and pauses longer than `end_silence_ms` end a recording early.
- The Hands-free loop pauses after two silences or any failure and needs a tap
  to continue; it does not listen by itself while a confirmation is pending.
  Speech can be stopped with the Stop speaking button or interrupted by tapping
  the mic, but not by simply talking over it (there is no always-on listening
  during playback), and U cannot be interrupted while it is generating a reply
  (the mic is disabled until the reply is ready).
- Wake word ("Kris"): "Chris" said alone also wakes it (the names sound the
  same); the name must be said on its own with a pause; it runs only while the
  page is visible and starts off on each page load; the energy gate is not speech
  detection, so short non-name sounds in a noisy room cause extra local checks;
  each check reloads the whisper model (a resident model or a dedicated keyword
  model, e.g. a custom-trained one, could cut latency and false activations
  later). Not yet verified with a real microphone across rooms and voices.
- Resuming listening without a tap relies on the browser keeping microphone
  permission for the page (verified in Chrome and Safari); a browser that asks
  again, or refuses without a fresh tap, stops the loop with "The microphone could
  not start".

### Model behaviour
- Tool results state exactly what a search covered, but Gemma may still omit
  details such as an incomplete search when it summarizes them.

### Command-center UI
- System telemetry is macOS-only. Thermals are a pressure level, not °C, and fan
  speeds are not shown: both need administrator tools (`powermetrics`) or
  private interfaces. GPU utilization uses an undocumented `ioreg` field that a
  macOS update could remove (it then reads "Unavailable"). CPU reads "Measuring"
  for about two seconds after the server starts.
- Google Workspace shows the unread count, the 10 newest inbox headers, and the
  primary calendar for today plus seven days, and can create plain-text Gmail
  drafts (no attachments, Bcc, display names or formatting; body up to 6,000
  characters). The model can only reply to a message whose ID is in the user's
  message (the Inbox tab's Draft reply action provides it) or one found with
  `search_gmail`. Google's `gmail.compose` and `gmail.modify` scopes would also
  allow sending; K.R.I.S. never calls a send endpoint, but the permissions
  themselves are broader than what K.R.I.S. does.
- Gmail reading returns at most about 4,500 characters of body text and the
  latest six messages of a thread; attachments are named but never opened.
  Search returns at most 10 matches. Mailbox actions apply to one message at a
  time (not whole threads), labels must already exist, and permanent deletion is
  not available. The model can still misread an email: everything it changes is
  confirmed first, but its summaries are not checked.
- Unverified: Google may let `gmail.metadata` restrict a sign-in that also has
  `gmail.readonly`; if so, reading and searching fail with a clear message until
  `gmail.metadata` is dropped from the requested scopes.
- Sending: one email per approval, at most 20 recipients, plain-text bodies only;
  forwards quote the text but do not attach the original's attachments; a draft
  is sent as stored in Gmail (including any formatting or attachments it has).
- Google Keep integration is deferred (planned for v1.3, cancelled): Google's
  Keep API is enterprise-oriented (an admin-approved OAuth client or domain-wide
  delegation in a Workspace organisation), and Google refuses the `keep` scope
  for a personal Gmail account with `Error 400: invalid_scope` before any
  consent screen. Revisit only with a Workspace account and admin approval; no
  browser-automation or unofficial workaround. Keep's API also has no update
  (edit) operation and deletes permanently, which any future design must handle.
- Calendar: only the primary calendar; whole recurring series cannot be changed
  or cancelled (single occurrences can); past events are read-only; times without
  a zone use this Mac's time zone; search returns at most 25 events per request.
  The model can still pick the wrong event if the user's request is vague: the
  approval panel shows exactly which event will change. One account
  at a time; a sign-in started before a server restart must be started again.
  Requires an Internal OAuth app in the organisation's Google Cloud, and a
  Workspace admin may need to allow it. There is no Apple Mail/Calendar provider.
- The Console only lists events seen by the open page; it is cleared on reload
  and runs no commands.
- Only the web UI is named K.R.I.S.; the runtime's prompts and messages and the
  terminal client still call the assistant "U".
- The HUD instrument and readouts around the core only reflect existing page
  state (link, mode, last console event); the glyph itself is still the
  milestone-1 placeholder.
- The energy core shows the page's state; it does not react to the actual
  microphone or speech volume.

### Application
- The web UI runs on Flask's built-in development server, which is adequate for
  a single local user on `127.0.0.1` but not for any other deployment.
- Math in assistant replies is not rendered; delimiters stay as plain text.

## Possible future work (not committed)
- A `find_files` option to search a subfolder inside an approved root, with the
  same path checks as `open_file`, to reach files beyond the entry limit.
- Sorting or filtering tasks by due date, and a "due today" state.
- Math rendering for assistant replies, if it can be added without large assets.

### Native alarms and timers through Shortcuts (evaluated 2026-10-19, not adopted)
Tested on macOS 26.6.2 with three hand-built shortcuts (`KRIS Set Alarm`, `KRIS Find
Alarms`, `KRIS Delete Alarms`) run through `/usr/bin/shortcuts run`:
- Clock declares Add Alarm, Find Alarms, Delete Alarms, Toggle Alarm, Start Timer, Get
  Current Timer and Cancel/Pause/Resume Timer, but the Shortcuts app offers only **Set
  Timer** for timers, so a native timer can be started but never verified or cancelled
  (and Cancel Timer has no target, so it could stop a timer the user set).
- Add Alarm works: the alarm appears in Clock, rings (tested with Clock open), and the
  shortcut returns the alarm's label, which can be checked. But one run hung past 25 s
  although the alarm was created, so a timeout does not mean failure.
- Find Alarms is not trustworthy: returning alarm objects hung; returning text, it
  returned nothing while two alarms existed and stale results after deletions. Delete
  Alarms deleted correctly, but its Find-based "remaining" count was wrong twice. Without
  a reliable Find, K.R.I.S. cannot list, verify or precisely cancel alarms.
- Clock alarms are time-of-day only (the next occurrence), not a specific date.
Not tested: an alarm with the Clock app quit, and across a Mac restart.
Decision: K.R.I.S.-managed timers stay; no Shortcuts integration. Alternatives considered:
a Swift notification helper (needs a signed app bundle for notification permission; too
heavy) and launchd agents (persistent files in ~/Library/LaunchAgents, one-shot jobs need
cleanup; more intrusive than the problem warrants). For an alert that must fire while
K.R.I.S. is closed, an Apple Reminder at the end time is the verified, cancellable native
option (already supported by the reminder tools).

### Apple Music catalogue search (evaluated 2026-10-19, not adopted)

Checked on macOS 26.6.2 for playing any song or album from the Apple Music catalogue by name:

- Music's AppleScript dictionary (`sdef /System/Applications/Music.app`) has `search` for a
  playlist of the user's library ("identical to entering search text in the Search field"),
  `play` for a track or playlist, and `current track`/`current playlist` to read back what plays.
  It has no catalogue search; `open location` only opens a URL that is already known.
  K.R.I.S. uses this for library songs and playlists (`find_music`, `play_music`).
- Music.app declares no App Intents actions. `_MediaPlayer_AppIntents` declares only search
  entities (AudioSearchCriteria/Result) used by the system, and no callable actions.
- Shortcuts lists "Find iTunes Store Items", "Find Music", "Play Music" and "Get Current Song",
  but only as actions inside a shortcut the user builds by hand. Whether "Play Music" plays a
  catalogue result from "Find iTunes Store Items" on macOS was not verified, and running user
  shortcuts was already rejected as unreliable (see the Shortcuts section above).
- Not used: UI scripting, MusicKit (needs a developer token and a signed app), and the iTunes
  Search web API (web scraping/HTTP is out of scope, and it cannot start playback).
