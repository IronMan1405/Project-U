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
- Long replies are spoken only up to about 1,500 characters.
- The terminal client has no voice support.

### Model behaviour
- Tool results state exactly what a search covered, but Gemma may still omit
  details such as an incomplete search when it summarizes them.

### Application
- The web UI runs on Flask's built-in development server, which is adequate for
  a single local user on `127.0.0.1` but not for any other deployment.
- Math in assistant replies is not rendered; delimiters stay as plain text.

## Possible future work (not committed)
- A `find_files` option to search a subfolder inside an approved root, with the
  same path checks as `open_file`, to reach files beyond the entry limit.
- Sorting or filtering tasks by due date, and a "due today" state.
- Math rendering for assistant replies, if it can be added without large assets.
