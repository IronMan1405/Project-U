# Project U

A local assistant with a terminal client and a lightweight web chat interface, powered by Ollama.

## Setup

Requires macOS, Python 3.11 or newer (the test suite is run with Python 3.14),
and Ollama with a model already installed. From the project directory, create
the virtual environment, install the dependencies, and create `config.ini` from
the example:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.ini.example config.ini
```

Set `model` in `config.ini` to a model shown by `ollama list`. The default
configuration uses `gemma4:e4b` and the local Ollama endpoint
`http://127.0.0.1:11434`. Make sure Ollama is running before starting U.

The example configuration also turns on the approved computer utilities (for
`~/Documents` and the Calendar and Notes apps) and voice; see those sections
below to adjust or remove them. Its tool paths assume Homebrew on Apple Silicon
(`/opt/homebrew`); on an Intel Mac, Homebrew installs to `/usr/local`, so check
`command -v ffmpeg whisper-cli` and update the `[voice]` paths.

Run the automated tests with:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The browser's end-of-speech detector and Hands-free listening loop have their
own tests, run with Node's built-in test runner (no packages to install):

```sh
node --test tests/js/
```

## The command center

The web UI is a K.R.I.S. (Knowledgeable & Reasonably Intelligent System) command
center. The K.R.I.S. wordmark is centred at the top, with the local model status
on the left and New conversation on the right. Below it, on a desktop-width
window, are three columns:
- **Left:** two boxes. **System** is always shown: live CPU, memory, GPU and
  thermal readings (see "System telemetry" below), and beneath them a large live
  clock (HH:MM, ice blue) with the date. Below it, **Inbox / Calendar** switches between two tabs that
  show the organisation's Google Workspace mail and calendar once connected
  (see "Google Workspace" below), and say plainly when they are not.
- **Centre:** the K.R.I.S. energy core, the main visual element, with its status
  beneath it. Its look follows the real state of the page: Standing by,
  Listening, Transcribing, Thinking, Speaking, Waiting for confirmation (amber),
  Hands-free paused, Error, and Offline when the model is unreachable. Only the
  core lives here, so switching views never replaces it.
- **Right:** the workspace with **Chat** (the transcript and any Confirm/Cancel
  prompt), **Tasks**, **Notes** and **Files** (the document picker). Below it,
  the **Console** is a read-only, terminal-style log of what happened on this page
  (tool use, voice states, errors, model status); it runs no commands and is not
  saved. At the bottom, a switchable panel shows **Activity** (the tools used in the
  latest request) and **Alerts** (current problems: model unavailable, errors, an
  action waiting for approval, Hands-free paused; the tab shows a count).

The command bar along the bottom (message box, microphone, Stop speaking, and the
Hands-free and Speak replies toggles) is always available. Sending a message, or a
pending confirmation, switches the workspace back to Chat. The switchable panels
are standard tabs: click them, or use the arrow keys, Home and End.

The visual layer is a restrained engineering HUD: hairline panel frames with
corner brackets and small section identifiers (SYS·01 and so on), a thin frame
around the whole window, technical uppercase labels, and a quiet colour scheme
(ice blue for primary text and active elements, blue-grey for secondary text,
amber for attention and confirmations, red only for errors; only the console's
output keeps pale text).
The core sits inside a ring of tick marks and segmented arcs that follows its
state. Under the core, LINK (model online or offline), MODE (Review or
Hands-free) and LAST EVENT (time of the newest console line) are read-only
readouts of existing state. The model's status is shown in the header (LINK).

The two side columns always share one width, so the core sits on the page's
centre line. Below 1180 px wide, the core and the three left boxes share the left
column. At 820 px and below, everything stacks into one scrolling column (core,
workspace, console, activity/utilities, system, inbox/calendar, alerts) with the
command bar pinned to the bottom. Animations stop when
the system asks for reduced motion. The server, terminal client and model prompts
still call the assistant "U"; only the web UI has been renamed so far.

## Who K.R.I.S. is

K.R.I.S. stands for **Knowledge & Reasonably Intelligent System**; its name is
spoken "Kris", like "Chris". Ask "Who are you?" or "What does K.R.I.S. stand
for?" and it answers as K.R.I.S. Ask what it really is and it says honestly that
it is an assistant application running the local model set in `config.ini`
(`gemma4:e4b` by default) through its tools, and that it is not conscious.

Its manner is composed, practical and concise, with occasional dry humour. It
corrects mistaken claims plainly, admits its own mistakes without fuss, and says
when it does not know. Now and then, in light banter where you are confidently
wrong about something minor, it may add its signature line, "Kindly Remember,
I'm Smart." Nothing inserts that line automatically, and it is told never to use
it after its own mistake, in serious topics, or in approvals and error messages.
The same identity applies to typed and spoken conversations. It is defined in
`identity_prompt` in `assistant_runtime.py` and never overrides the tool,
approval and data rules.

## Profile and preferences

U reads `profile.json` from the project directory before each model request, so
edits take effect on the next response in either client. Create it from
`profile.example.json` and edit the JSON values:

```sh
cp profile.example.json profile.json
```

The supported fields are `response_style` and `explanation_detail` (short text
values), plus `interests` (a list of short strings). Two optional fields tell K.R.I.S.
who you are: `name` (what it should call you) and `about` (up to eight short facts you
choose to share, such as "Computer engineering student"). K.R.I.S. knows only what is
written here and in the current conversation: asked who you are, it answers from the
profile, says plainly when something is not there, and never invents details. It uses
your name sparingly, not in every reply. K.R.I.S.'s own identity and personality are
separate (`identity_prompt` in `assistant_runtime.py`). If the file is missing,
malformed, unreadable, or contains invalid values, U logs a warning when
applicable and uses generic defaults. The local `profile.json` is excluded from
Git. Profile values are treated as preferences, not instructions or
authorization.

## Terminal

From the project directory, run:

```sh
.venv/bin/python main.py
```

Enter a message to chat. Type `/quit` or `/exit` to leave.

## Web interface

After the setup above, start the local server:

```sh
.venv/bin/python web_app.py
```

Open <http://127.0.0.1:5000> (or <http://localhost:5000>). The server binds to
127.0.0.1 and rejects requests addressed to any other host name with HTTP 400, so
web pages on other sites cannot use its API. It uses the same
`config.ini` and Ollama model as the terminal client. Conversation history is
kept in server memory for each open conversation and cleared when a new one is
started or the server stops. The status indicator checks Ollama and the
configured model. Stop the server with `Ctrl+C` in its terminal.

## System telemetry

On macOS the System panel shows live readings, refreshed about every 3 seconds
while the page is visible (polling pauses when the tab is hidden). They come from
`GET /api/system/telemetry`, which reads macOS directly with the standard library:
no extra packages, no administrator rights, and no shell.

- **CPU:** utilization over the last ~2 seconds, from the kernel's CPU counters.
  It shows "Measuring" for the first couple of seconds after the server starts.
- **Memory:** used / total. "Used" is app memory + wired + compressed memory, the
  parts Activity Monitor adds up as "Memory Used". This is not the same as memory
  *pressure*, which is shown separately in the tooltip (normal, warning,
  critical) and colours the reading amber or red when it rises.
- **GPU:** utilization from macOS's accelerator statistics (`ioreg`). Apple does
  not document this value, so it may read "Unavailable" after a macOS update.
- **Thermals:** macOS's thermal pressure level (Nominal, Fair, Serious,
  Critical). Temperatures in °C are not shown: macOS only exposes them to
  administrator tools or private interfaces.

A reading that cannot be taken shows "Unavailable" (hover for the reason); it is
never shown as zero. On other platforms every reading is unavailable. The
assistant's `get_system_info` tool reports the same four readings when you ask
how busy the Mac is.

## Google Workspace (Gmail and Calendar)

K.R.I.S. can show the organisation's Google Workspace inbox and calendar in the
Inbox / Calendar box: the unread count, the 10 newest email headers (sender,
subject, time; never message bodies), and primary-calendar events for the next
two weeks (switch to the next month with **Show next month**). That window is only
what the panel shows: the assistant can search and read your calendar for any
past or future date. It talks to Google's Gmail and Calendar APIs directly, with
no extra packages. This is a separate provider from any Apple Mail or Apple
Calendar integration (there is none yet). It is off unless you set it up, and it
is the only feature that contacts the internet, and then only Google.

**What K.R.I.S. can do with it:** show the inbox and calendar, create **Gmail
drafts** (stage 2), **search, read and organise Gmail** (stage 3: answer questions
about and summarise your email, mark messages read or unread, star or unstar
them, archive them, move them to Trash, and add or remove your existing labels),
and (stage 4) **send email** (a new message, a reply, a forward, or an existing
draft) and **search, read, create, change and cancel Calendar events**. Every
send and every change asks you first. It cannot permanently delete email, send in
bulk, create labels, or change past events.

### Workflows across tools
K.R.I.S. can combine its tools in one request: "What do I have tomorrow?"
(Calendar and Tasks), "Find the email about the meeting and tell me when it is"
(Gmail and Calendar), or "Prepare my morning briefing" (Calendar, Tasks, email and
notes; up to six tool uses per request). Follow-ups such as "open the second
one", "move it to Friday" or "email him about the change" use what was found
earlier in the conversation. Reading needs no approval; every send or change
gets its **own** approval, one at a time, and declining one cancels the rest of
that request. If a step fails, K.R.I.S. says exactly what succeeded, what failed
and what was not attempted, and keeps the prepared content so you can retry.
When "it" or "that" could mean more than one thing, it asks.

### Sending email
Ask in chat, for example "Email john@example.org that I'll be 10 minutes late",
"Reply to Ada's email and say Thursday works", "Forward the maintenance email to
Grace", or "Send my draft about the lab booking". K.R.I.S. prepares the message
and shows the **exact** email in the approval panel: To, Cc, Bcc, subject and
the complete body, and for a reply or forward the original message (sender,
subject, date, message and thread). It is sent only when you press **Confirm**;
saying "I confirm" or "just send it" in chat does not send anything, and Cancel
sends nothing. Pressing Confirm sends at once and cannot be undone.

What is sent is exactly what was shown: K.R.I.S. records a fingerprint of the
previewed message and refuses to send if anything differs, and before sending a
draft it re-reads the draft and stops if it was edited since. A reply goes to the
original sender (or Reply-To) with "Re:" and stays in the thread. A forward
quotes the original text under your note ("Fwd:"); attachments are **not**
forwarded, and the panel says so. To send an existing draft, K.R.I.S. first
lists your drafts and can only send one it found that way. Creating a draft never
sends it. Recipients are plain addresses, at most 20 per email (To, Cc and Bcc
together), and there is no bulk sending. A failed send is reported as not sent.

### Calendar
Ask about any date, for example "What meetings did I have last month?", "What
do I have on March 15 next year?", or "When is my next meeting with John?".
Reading and searching need no approval and are not limited to the panel's two
weeks or month. K.R.I.S. can also **create** events at any future date, and
**change** or **cancel** events from now on, however far ahead; past events can
be read but not changed, and a whole recurring series cannot be changed (one
occurrence can). Every change asks first, in the approval panel:
- **Create** shows the title, date and times, time zone, location, description,
  attendees and recurrence. If there are attendees, Google emails them
  invitations, and the panel says so.
- **Change** shows the event **Before** (as it is in Google Calendar right now)
  and **After**, field by field. Moving the start keeps the length.
- **Cancel** shows the event, its calendar and ID, and that it will be removed.

To change or cancel an event, K.R.I.S. must first find it with a search (it can
only act on event IDs Google returned in the same conversation); if several
events could match, it should ask which one. The event is fetched fresh to build
the approval panel and again just before the change, and if it changed in the
meantime (or was deleted) nothing is overwritten: you are told, and asked again.
Google Calendar also refuses the change if the event changed (K.R.I.S. sends the
event's version tag). Times without a time zone use this Mac's time zone. Only
the primary calendar is used.

### Reading and searching Gmail
Ask in chat, for example "What did Ada say about the quarterly numbers?" or
"Summarise my unread email from this week". K.R.I.S. searches with Gmail's own
search (the same syntax as Gmail's search box, run on Google's servers), sees
only the sender, recipients, subject, date and read/star state of at most 10
matches, and then reads the one message or conversation it needs. Reading needs
no confirmation, because it changes nothing (it does not even mark the email as
read). Long messages and conversations are shortened (about 4,500 characters of
text, the latest six messages of a thread); attachments are listed by name but
not opened. Email text is treated as untrusted: instructions inside an email are
never followed, and every change still needs your approval.

### Organising Gmail
K.R.I.S. can mark a message read or unread, star or unstar it, archive it,
move it to **Trash** (reversible: restore it from Gmail's Trash within 30 days),
or add or remove one of your existing labels. Each acts on **one message**, and
each always asks first: the approval panel names the exact action and the exact
message (sender, subject, date, message and thread ID). Nothing changes until you
press **Confirm**; Cancel changes nothing. Permanent deletion is not available at
all, and labels must already exist (system labels such as Spam cannot be added).

### Gmail drafts
Ask in chat, for example "Draft an email to grace@example.org about booking the
lab on Thursday". To reply, choose **Draft reply** under an email in the Inbox
tab: it puts a request (with that email's message ID) in the message box for you
to finish and send. A reply draft is addressed to the original sender (or their
Reply-To), uses "Re: <subject>", and joins the original Gmail thread.

Before anything is written, K.R.I.S. shows the **exact** draft in the approval
panel: new email or reply, every recipient (To and Cc), the subject, and the
complete body. Nothing is created until you press **Confirm**; **Cancel**, or
sending another message, discards it. Approval is never taken from the wording
of your request ("I confirm" in a chat message does not count); a spoken
"confirm" works like the button (see "Spoken confirmation"), a bare "yes" does not. The draft then waits in Gmail's Drafts folder; K.R.I.S.
never sends it. Recipients must be plain addresses (no display names), at most
20 in all; the subject is one line; the body is plain text up to 6,000
characters. Attachments, Bcc and formatting are not supported.

### Setup (once, by someone with access to the organisation's Google Cloud)
1. In a Google Cloud project owned by the organisation, enable the **Gmail API**
   and the **Google Calendar API**.
2. Configure the OAuth consent screen with user type **Internal** (Gmail scopes
   are restricted; an Internal app avoids Google's external verification). A
   Workspace admin may also need to allow the app under *Security → API controls*.
3. Create an OAuth client of type **Desktop app** and download its JSON.
4. Save it outside this repository, readable only by you:
   ```sh
   mkdir -p "$HOME/Library/Application Support/KRIS"
   mv ~/Downloads/client_secret_*.json "$HOME/Library/Application Support/KRIS/google-oauth-client.json"
   chmod 600 "$HOME/Library/Application Support/KRIS/google-oauth-client.json"
   ```
5. Add a `[google]` section to `config.ini` (see `config.ini.example`) and restart:
   ```ini
   [google]
   client_secrets_file = ~/Library/Application Support/KRIS/google-oauth-client.json
   workspace_domain = example.org
   ```
   `workspace_domain` is optional but recommended: K.R.I.S. refuses (and revokes)
   any account outside that Workspace domain.
6. Open K.R.I.S. at `http://127.0.0.1:5000`, choose **Connect Google** in the
   Inbox or Calendar tab, and approve access in the tab Google opens. When the
   page says "Connected", close it; K.R.I.S. picks up the connection itself.

**Permissions requested** (nothing else):
`openid` and `email` (which account is connected), `gmail.metadata` (labels and
message headers, not bodies), `gmail.compose` (create drafts), `gmail.readonly`
(search and read full messages), `gmail.modify` (read state, stars, archive,
Trash, labels), `gmail.send` (send one confirmed email at a time), and
`calendar.events` (read, create, change and cancel events; it replaces the
read-only Calendar scope used before stage 4). There is no permanent-delete,
bulk, or general-purpose Gmail or Calendar operation anywhere. Google Keep is
not supported (deferred): Google's Keep API is meant for Google Workspace
organisations, and a personal Google account cannot be granted its `keep`
scope (Google answers `invalid_scope`).

**Reconnect after upgrading.** A connection made before the current permissions
(stage 4) is treated as disconnected: the Inbox / Calendar box says K.R.I.S.
needs updated Google permissions and asks you to reconnect, and nothing reads or
writes Gmail or Calendar until you reconnect and approve. If you untick a permission on Google's consent page, the features that
need it report that access was not granted and the rest keep working.

**Tokens.** Sign-in uses OAuth 2.0 with PKCE and a loopback redirect to
`http://127.0.0.1:<port>/oauth/google/callback`. Only the refresh token and the
account's email address are kept, in your login **Keychain** (item "K.R.I.S.
Google Workspace"); access tokens stay in memory. Tokens never appear in logs,
API responses, or the browser, and email or event details are never logged.
**Disconnect** revokes access at Google and deletes the Keychain item.

**When something goes wrong** the box says what to do: not set up, not
connected, access declined, an account outside the domain, the organisation's
policy blocking the app, an API not enabled, access revoked or expired (connect
again), Google rate limits, or no network. Inbox refreshes every minute and
Calendar every five minutes while the page is visible.

**Adding permissions later** works the same way: a new scope raises the consent
version, so you are asked to reconnect and approve it, and every new action is a
model tool that always asks for your confirmation before it runs.

## Tasks and notes

Use the Notes tab in the web interface's workspace panel to create, edit and delete notes.
The Tasks tab is hidden by default because Apple Reminders is the reminder workflow (see
"Briefings, reminders and timers"); set `tasks_tab = on` under `[ui]` in `config.ini` to show
it again. Tasks themselves are unchanged: "add ... to my tasks" in chat, the task tools, the
`/api/tasks` endpoints and the briefing still use them, and no task data is moved or deleted.
With the tab shown, it creates, edits, completes, reopens and deletes tasks. Notes preserve their text and newlines. Records are
stored locally in `data/project_u.sqlite3`; the directory and schema are created
automatically the first time U starts. Data persists across restarts and is
excluded from Git. Removing the database file while U is stopped permanently
deletes its tasks and notes. In chat, U can also manage tasks and notes, but only
through the validated assistant tools described under "Assistant tools"; the
model has no other way to change records.

Tasks can have an optional due date. `due_at` is either a calendar date
(`YYYY-MM-DD`) or a UTC time (`YYYY-MM-DD HH:MM:SSZ`, optional fractional
seconds); any other format, or an impossible date, is rejected with a 400.
In the Tasks tab, pick a date (and optionally a time, entered in local time)
when adding a task, or use "Set due"/"Edit due" on an existing task to change
or remove it. Incomplete tasks past their due date are marked "Overdue".
`PATCH /api/tasks/<id>` accepts `completed`, `due_at`, or both; `"due_at": null`
clears the due date, and a field left out of the request is left unchanged.
Databases created before due dates existed gain the column automatically.

In chat, the assistant passes due times as your local wall-clock time
(`YYYY-MM-DD HH:MM`, 24-hour), or with an explicit offset such as `+05:30` only
when you name another time zone. Python converts them to the stored UTC form
using this Mac's time-zone rules, including daylight saving; a local time that a
daylight-saving change skips or repeats is rejected rather than guessed. The
confirmation and tool result show the stored time converted back to local time,
so what U reports is what was saved.

## Briefings, reminders and timers

**Briefing.** Ask "Kris, brief me", "What do I have today?", "What's coming up
tomorrow?", "What's overdue?", "What did I leave unfinished today?" or "Help me
plan the rest of my day". K.R.I.S. reads, for your local date and time zone:
Google Calendar events (when connected), K.R.I.S. tasks (overdue, due that day,
without a due date), Apple Reminders (overdue, due that day) and running timers.
Each source is reported as available, not connected, or unavailable with the
reason, so a missing source is never shown as an empty day. Nothing is invented,
and because K.R.I.S. does not record when a task was completed, it never claims
what you finished today. A task and a reminder with the same title due the same
day are shown once.

**Reminders live in Apple Reminders.** "Remind me to email the professor at 6
PM" creates a reminder in your default Reminders list. Reminders alerts you at
that time and syncs to your other devices, **whether or not K.R.I.S. is
running**. You can also ask "What reminders do I have today?", "Mark the
professor reminder as complete" and "Move that reminder to Friday". K.R.I.S.
reads every change back from Reminders before reporting it, refuses to add an
exact duplicate (same title, same day), only changes reminders it has found in
the same conversation, asks which one when several could be meant, and never
deletes reminders. If you give a time like "at 7" that could be AM or PM, it
asks. "Add … to my tasks" still means a K.R.I.S. task (the Tasks tab); nothing
is copied between the two.

*One-time setup:* the first time K.R.I.S. uses Reminders, macOS asks whether
the app that runs K.R.I.S. (for example Terminal) may control Reminders and
access your reminders. Allow both. If you decline, K.R.I.S. says it is not
allowed and points to System Settings > Privacy & Security (Automation >
Reminders, and Reminders); nothing is bypassed.

**Timers and focus sessions are K.R.I.S.'s own.** Apple's Clock app cannot be
scripted, so K.R.I.S. never claims to set a Clock timer or alarm. Say "Set a
25-minute timer", "Start a 50-minute focus session", "Give me a 10-minute break
after that", "How much time is left?" or "Cancel the timer" (if several are
running it asks which). Running timers appear in the Alerts tab with a
countdown. When one ends, K.R.I.S. shows a macOS notification (allow
notifications for the app that runs K.R.I.S. the first time), plays a short
chime, notes it in the Console, and speaks it when speech is on and the
microphone is not in use. Timers are saved, so they survive a restart, **but
they only alert while K.R.I.S. is running**: a timer that ends while K.R.I.S. is
closed is reported as *missed* (with its end time) the next time it starts,
never as done.

To be alerted even if K.R.I.S. is closed, ask for a reminder: "Set a 25-minute
focus session and remind me when it's over". K.R.I.S. then also creates an Apple
Reminder ("K.R.I.S. timer 3: 25-minute focus session ends") at the end time,
rounded up to the next minute because Reminders has no seconds, and confirms it
with Reminders before saying so. Reminders alerts on its own, and keeps the
alert across a Mac restart. Cancelling the timer marks that reminder complete
(it is never deleted); if Reminders refuses, K.R.I.S. says the reminder will
still alert. After a timer with a reminder ends, K.R.I.S. marks the reminder
complete a couple of minutes later, so finished timers do not pile up as
overdue reminders. Without the request, no reminder is created.

## Approvals, spoken confirmation and the Mac

### Confirmation policy

Small, readily undone actions run as soon as you ask for them; consequential ones wait for
your approval. "Asked for" means your own message requests that kind of action (for
example "remind me", "set a timer", "mark it as read", "open GitHub"); the model choosing
an action by itself is never enough, so the same call then asks first. Arguments are always
validated, targets must come from the current conversation (a reminder or email found
earlier), and each result is read back before it is reported.

| Runs directly when you ask | Always asks for approval |
| --- | --- |
| Start or cancel a K.R.I.S. timer or focus session | Sending, replying to or forwarding email; sending a draft |
| Add, complete or move an Apple Reminder | Creating Gmail drafts |
| Mark an email read or unread; star or unstar it | Archiving, trashing or labelling email |
| Open an approved app you named, or a website you named | All Calendar changes |
| Set or change the volume; mute or unmute | Deleting tasks or notes |
| Play, pause or skip in Music or Spotify | Opening an app or website the model chose itself |
| Run a project's configured tests; open a file or folder you named (or chose from a search) in its default app, a named app, or the editor | Opening something you did not name, or in an app you did not name |
| Play a song or playlist you chose from your Music library | Any tool named as a file change, install or Git change (none exist) |

A timer with an end-of-timer reminder is direct only when you asked to be reminded. Tasks
and notes keep their earlier rule (direct when your message asks for that exact write).
Apple Clock alarms are not available (macOS has no reliable interface; see FUTURE.md).

### Spoken confirmation

When an action needs approval, K.R.I.S. shows it in the approval panel and, if speech is on,
reads it out ("Waiting for your confirmation: Send email to ... Say "confirm" to approve it,
or "cancel".") In a hands-free conversation it then listens once for the answer.

- Only a short, explicit reply approves: "confirm", "confirmed", "yes, confirm", "approve".
  A bare "yes", "okay" or "sure" is not approval (whisper and background audio produce those
  easily); K.R.I.S. asks you to say "confirm". "Cancel", "no" or "never mind" cancel.
- A spoken "confirm" approves only the action that was already read out (or shown) when the
  recording started, through the same request as the Confirm button; the server accepts each
  approval once, so stale or repeated approvals never run anything.
- Anything else said while an action waits is not sent hands-free (sending a new message
  would cancel the action); it goes to the message box and the action keeps waiting. After
  two answers that are neither confirm nor cancel, or silence, the buttons remain.
- Stopping the read-back with Stop speaking does not listen for an answer; tap the mic or use
  the buttons. Typing "confirm" in chat is a message, never an approval.

### Wake acknowledgement

When K.R.I.S. hears its name it answers with one of a few short acknowledgements ("Yes?",
"I'm listening.", "Go ahead.", ...), never one of the last three it used, and never with
its name, so it cannot wake itself (standby has also released the microphone while it speaks).

### Mac control and system status

- Volume: "what's the volume?", "set the volume to 30", "a bit louder", "mute". Uses macOS
  Standard Additions (no permission needed). Values are limited to 0-100 and read back.
  Outputs whose volume is set on the device (some HDMI/digital outputs) are reported as such.
- Playback: play, pause, toggle, next and previous in Music or Spotify, and "what's playing?",
  only when that player is already running (it is never launched). The first use asks for
  permission to control that player (System Settings > Privacy & Security > Automation).
  Video or audio in a browser cannot be controlled.
- Music library: "play Cornfield Chase by Hans Zimmer" or "play my Interstellar playlist"
  searches your Music library (Music's own search) and plays the one song or playlist you
  mean, then checks that it is what Music is playing before saying so. Several matches
  (two versions of a song) are listed and you choose. The Apple Music catalogue cannot be
  searched (see FUTURE.md), so a song must be in your library; an album cannot be played as
  an album, only its songs or a playlist. Searching opens Music if it is not running.
- Apps and websites: approved apps (`allowed_apps`) and validated http(s) URLs, as above;
  "is Safari running?" checks an approved app without opening it.
- System: `get_system_info` adds load averages (with the core count) and uptime to the
  battery, memory, disk, CPU, GPU and thermal readings; anything unavailable is reported
  as unavailable, never estimated. Nothing polls in the background for this.

### Developer companion

Configure projects in `config.ini` (see `config.ini.example`):

```ini
[projects]
Project-U = ~/Project-U

[project_tests]
Project-U = .venv/bin/python -m unittest discover -s tests
    node --test tests/js/

[developer]
editor = Visual Studio Code
test_timeout_seconds = 300
```

Then ask "check the Git status of Project-U", "do I have uncommitted changes?", "summarize my
changes", "show the recent commits", "run the tests for this project", "show me the latest
test results" or "open this project in my editor". With one project configured, "this
project" means it.

- Git is read-only: status, branch and upstream, per-file line counts of staged and unstaged
  changes (never file contents), recent commits, and remotes without embedded credentials.
  `/usr/bin/git` runs with argument lists only for `status`, `diff`, `log`, `rev-parse` and
  `remote -v`; optional locks, pagers, external diff and text conversion are off.
- Tests run exactly the configured commands, without a shell, from the project folder, each
  stopped after `test_timeout_seconds` (its whole process group). Results are passed, failed
  (non-zero exit, whatever the output says), timed out or unavailable, with the runner's
  summary line and a short, credential-masked output tail. Opt-in live-test switches
  (`LIVE_*`, `*_SMOKE`) are removed from their environment. Nothing is installed. The latest
  result per project is kept in memory until K.R.I.S. restarts.
- Nothing stages, commits, resets, checks out, pushes, edits files or installs packages.
  Those tools do not exist; `check_confirmation_boundaries` refuses to register any of them
  without confirmation, and refuses a generic command runner outright.

## Selected documents

In Chat, choose and upload one `.txt` (UTF-8) or `.pdf` file to ask questions
about it. Extraction runs locally with `pypdf` (the only new dependency).
Flask may transiently spool multipart input while parsing it; no uploaded file
is retained after the request. Extracted text is held only in memory for the
active conversation and is never written to disk, SQLite, or the profile.
Clearing the attachment returns to
ordinary chat. Replacing an
attached document resets that conversation's history to keep document sources
separate. Starting a new conversation discards its attachment.

Uploads are limited to 5 MiB, PDFs to 200 pages, and decoded PDF streams to
500,000 bytes each. Extraction is capped at 100,000 characters, with a
12,000-character document-context budget per model request. Small documents
that fit are supplied in full. Larger documents use
simple keyword-ranked, page-labeled paragraph excerpts; this is not semantic
search, and U does not claim the full document was considered. If extraction
hits its limit, the remainder is unavailable. Scanned/image-only PDFs are not
OCR'd; encrypted, malformed, empty, unsupported, or textless files are rejected
with an explanation.

## Assistant tools

Terminal and web chat share `assistant_runtime.py` and Ollama's native tool-call
protocol. U can list/create/complete/reopen/delete tasks and set, change, or clear a
task's due date; list/read/create/edit/
delete notes; report local time, battery, memory, and disk status; request a
website opening in the default browser; and, when configured, search approved
folders by file name, open documents from them, and open approved applications. Task and note actions use the existing
SQLite storage layer. No model-generated shell commands or code are executed.

The runtime allows at most four tool executions per user turn. Read-only tools
run directly. A task/note write runs directly only when the user message clearly
requests it; ambiguous writes, every deletion, and every website opening pause
for explicit approval. Web users get Confirm/Cancel controls; the terminal asks
for a yes/no response. Tool errors and unavailable system readings are reported
as such rather than guessed. Each model request includes the current local date
and time so U can resolve dates such as "tomorrow" or "Friday" into local
dates and times (U converts them to UTC itself); when the day itself is unclear,
U is instructed to ask rather than guess.

### Conversations

Within a conversation, U sees the earlier messages and tool results, so
follow-ups such as "open the second one", "summarize that", or "actually make it
6pm" can refer to what it found or did before. History is scoped to one
conversation and kept only in memory: it is gone when you start a new
conversation, reload the page, or stop or restart U (the server or the terminal
client). The conversation itself is never saved to disk (tasks and notes that U
creates or changes during it are).

- **History limit.** Before each new message, if the stored conversation is
  larger than about 48,000 characters, U drops the oldest complete turns until it
  is at or below about 28,800 characters (a turn is your message plus U's tool
  requests, their results, and the reply). A tool request is never separated from
  its result, and the newest turn is always kept, even if it alone is larger than
  the limit; U no longer sees what was dropped. The model's hidden reasoning
  ("thinking") is not stored in the history.
- **Prompt order.** Each request sends the parts that do not change first (your
  profile, U's fixed instructions, and the earlier turns), then the parts that
  change with every message (the current date/time and any document excerpts),
  then the current turn. The changing parts are not stored in the history. This
  keeps the start of each request identical to the previous one, so Ollama can
  reuse its cached processing of the earlier turns instead of re-reading the whole
  conversation. How much time that saves depends on the conversation length and
  the Mac; the first message after Ollama starts or reloads the model (Ollama
  unloads an idle model after a few minutes by default), and the first one after
  the history limit drops old turns, are processed in full.
- **Cancelling.** When you click Cancel (or answer anything but "y"/"yes" in the
  terminal), the declined action is not run, and neither is any action U had
  queued after it in the same request. U replies with a fixed message rather than
  a model-written one, for example "Cancelled: Delete task 3. It was not run. 1
  other requested action was not run either." If earlier actions in the same
  request had already run, the reply says so and points to the activity list,
  which shows them.
- **Changing your mind.** While an action waits for confirmation in the web
  interface, Confirm and Cancel stay available, and you can also keep typing (or
  use push-to-talk). Sending a new message first cancels the waiting action
  exactly as Cancel would (it and anything queued after it never run), shows the
  cancellation message, and then answers the new message. The old confirmation
  becomes invalid, so a late Confirm for it is refused. If you had already
  clicked Confirm and the action is running, a new message is refused as busy
  until it finishes. Only the Confirm button approves an action: a typed (or
  spoken) "yes" is a new message, so it cancels the waiting action instead.

### Approved computer utilities

These tools are controlled by `config.ini`; restart U after editing it.
`config.ini.example` enables `find_files`/`open_file` for `~/Documents` and
`open_application` for Calendar and Notes, so copying it turns them on. Remove
the `[utility_folders]` section (or empty `allowed_apps`) to turn the matching
tools off; without them, the tools are not offered to the model.

- `[utility_folders]` lists `Label = folder` pairs. Folders must exist inside
  your home folder; your whole home folder, `~/Library`, and hidden folders are
  refused, and invalid entries are skipped with a warning.
- `[utilities] allowed_apps` is a comma-separated list of macOS app names.

`find_files` matches file names (never contents) in those folders and their
subfolders only. Every query word must appear in the name, ignoring case, spaces,
underscores, punctuation, and camelCase/PascalCase, so "performance sheet" finds
`PerformanceSheet.pdf`, `performance_sheet.pdf`, and `Performance Sheet.pdf`. An
optional `file_type` (e.g. `pdf`) filters by extension.

If a search of several words finds nothing and some of the words only describe a
kind of file, it is retried once with the remaining words, limited to these
extensions:

| Words | Extensions |
|---|---|
| spreadsheet, spreadsheets | `.xlsx` `.xls` `.csv` `.numbers` |
| pdf, pdfs | `.pdf` |
| document, documents, doc, docs | `.docx` `.doc` `.pdf` `.txt` `.md` `.rtf` `.pages` |
| image(s), photo(s), picture(s) | `.png` `.jpg` `.jpeg` `.heic` `.gif` |
| presentation(s), slides, slideshow | `.pptx` `.ppt` `.key` |

For example, "budget spreadsheets" finds `Budget 2026.xlsx`. The retry searches
the same approved folders with the same rules, and uses only what is left of the
same 5,000-entry limit (so both searches together never examine more). It does
not run when the first search found anything, when `file_type` was given, when
every word is a file-type word, or when the limit is already used up; the result
says when it retried. The model may name only
an approved root (folder label), never a path or subfolder; subfolders such as
`Desktop/Projects/` are reached only by searching their approved root. Hidden
items, symbolic links, and app/system packages are skipped; the search goes at most
four levels deep, examines at most 5,000 entries in total (shared in turns across
roots so no root is starved), and returns at most 20 matches as
`Label/relative/path`. Results state the words searched and name any root the
entry limit cut short. It is read-only and runs without confirmation.

`find_files` and `find_folders` also search the projects in `[projects]` (by label,
e.g. `Project-U/config.ini`), and both report roots macOS would not let them read.

`find_folders` finds folders by name ("find my Project-U folder", "where is the SOLVE
folder?", "search my Desktop for folders containing electronics"). Every word must
start a word in the folder name, ignoring case and separators ("solve" matches
`SOLVE` and `Solve Notes`, not `Resolve`). Exact names come first, each result has
its full location, and when more than one folder fits, K.R.I.S. lists them and
asks. It goes six levels deep and examines at most 20,000 entries; symbolic links
are never followed, packages never entered, and hidden, dependency, build and
cache folders (`node_modules`, `build`, `dist`, `__pycache__`, `.git`, ...) are
not searched unless you ask for hidden or build folders. It is read-only.

`open_file` opens a file or folder you identified: a search result, a project
(`Project-U`), or a path inside one (`Project-U/tests`). Without an app it uses
the default app (folders open in Finder; only `.pdf .txt .md .csv .docx .xlsx
.pptx .png .jpg .jpeg` files, so nothing executable runs). With an app ("open the
report in Preview") it uses exactly that app: one from `allowed_apps` or the
editor, checked to be installed in `/Applications`, `~/Applications` or the
system folders; if it is missing K.R.I.S. says so and does not fall back to the
default. Folders open only in apps that open folders (Finder, VS Code and a few
editors), and files are never opened in Terminal, Script Editor or similar apps
that could run them. `open_in_editor` does the same with the `[developer]` editor
("open config.ini in VS Code"). Every path component is checked without following
symbolic links; hidden items, package contents, `.`/`..` and paths outside the
roots are refused, and the checks run again just before opening.

Opening runs directly when you asked to open it and named it (or picked it from a
search in this conversation) and named the app, if any; otherwise you confirm
first. Opening one of several matches from the same request is refused until you
say which one ("the one on my Desktop", or "the second one").

`open_application` opens an app from `allowed_apps` (exact name). All open tools
run the fixed `/usr/bin/open` executable with an argument list (`open -a <app
bundle> <path>`), never a shell, and are macOS-only. An app that already has the
item open reuses its window, so VS Code does not open a duplicate window for a
folder it already shows.

### Voice (push-to-talk)

Voice is optional and fully local: speech is transcribed by
[whisper.cpp](https://github.com/ggml-org/whisper.cpp) and replies are spoken by
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), a natural-sounding
local voice (macOS `say` is used if Kokoro is not installed). No audio or text
leaves the machine. One-time setup (the only step that uses the network):

```sh
brew install ffmpeg whisper.cpp
mkdir -p data/models
curl -fL --proto '=https' --tlsv1.2 -o data/models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
# Natural voice (Kokoro): the Python package and two model files (about 350 MB).
.venv/bin/pip install -r requirements.txt
curl -fL --proto '=https' --tlsv1.2 -o data/models/kokoro-v1.0.onnx \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
curl -fL --proto '=https' --tlsv1.2 -o data/models/voices-v1.0.bin \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
```

The Kokoro files used here have these SHA-256 sums (`shasum -a 256 data/models/*`):
`kokoro-v1.0.onnx` 7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5,
`voices-v1.0.bin` bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d.

Compare `shasum -a 256 data/models/ggml-base.en.bin` with the SHA-256 shown for
the file on Hugging Face. `config.ini.example` already contains the `[voice]`
section (copy it into an existing `config.ini` if needed), then restart U. The
example paths are for Homebrew on Apple Silicon; on an Intel Mac use the paths
from `command -v ffmpeg whisper-cli` (usually `/usr/local/bin`). If FFmpeg,
whisper.cpp, or the model is missing, U logs a warning and hides push-to-talk;
without the `[voice]` section, all voice controls are hidden. Typed chat is
unchanged either way.

In Chat, hold the microphone button (or hold Space/Enter on it) while speaking
and release to stop; recordings are capped at `max_seconds` (60 by default).
FFmpeg converts the browser recording to 16 kHz mono WAV and whisper.cpp
transcribes it. Voice requests always go through the same chat path, tool
rules, and Confirm/Cancel buttons as typed ones.

- **Review mode (default).** The transcript is placed in the message box for you
  to review and edit; nothing is sent until you press Enter.
- **Hands-free (opt-in, off by default).** Tick "Hands-free" to send each
  transcript automatically, and to hear the reply, any confirmation prompt, and
  any error for that exchange spoken aloud, even if "Speak replies" is off. If you
  had already typed something in the message box, the transcript is added to it
  for review instead of being sent. The setting is not remembered; reloading the
  page turns it off.

**Automatic end-of-speech detection (Hands-free only).** In Hands-free mode,
*tap* the microphone and speak: recording stops by itself once you have been
quiet for `end_silence_ms` (1,200 ms by default) after speaking, but never
sooner than `min_recording_ms` (600 ms) after it started, and the existing
`max_seconds` limit still applies. Tap again to stop at once. Pressing and
holding still works and is fully manual: hold past about 0.4 s and the recording
stops only when you release. If no speech is heard within 6 seconds, the
recording is discarded and nothing is uploaded. Review mode is unchanged: hold to
talk, release to stop.

**Hands-free listening loop.** After you tap the mic once in Hands-free mode, U
keeps the conversation going: it stops recording when you pause, answers, speaks
its reply, and then starts listening again by itself, so you can simply say the
next thing. If spoken replies are unavailable, it starts listening again as soon
as the reply is ready. It never starts listening on page load, when Hands-free is
turned on, or while a reply is still being generated; only your first tap starts
it. The loop stops, and the status line says so, when:
- you turn Hands-free off, start a new conversation, switch to another tab or
  window, or type and send a message yourself (an open recording is dropped
  without uploading);
- it hears nothing twice in a row ("Didn't hear anything. Hands-free paused.");
- the microphone cannot start, a recording cannot be transcribed, U cannot
  answer, or the reply cannot be spoken.
It never retries by itself after a failure; tap the mic to continue.

While an action waits for confirmation, the loop does not listen on its own:
press Confirm or Cancel, or tap the mic and say "no" (a spoken "yes" still never
approves anything). Once the confirmation is resolved and U has finished
replying, listening resumes. If a transcript arrives while U is still busy with
another request, it is never sent behind your back or lost: it is left in the
message box for you to send, and the loop stops.

**Voice indicator and Stop speaking.** Whenever voice is available, a small
indicator above the message box shows what U is doing: Listening, Transcribing,
Thinking, Speaking, Waiting for confirmation, Hands-free paused, Standby (wake
word; see below), or Error (the
status line next to the toggles still gives the details). Screen readers hear
each change once. While U is speaking a reply, prompt, or error, a **Stop
speaking** button appears (it works with the keyboard too). It stops the audio
at once; in Hands-free mode U then starts listening for your next words, exactly
as if the reply had finished, except while an action waits for confirmation,
when it keeps waiting. In Review mode it only stops the audio. U cannot be interrupted while it is
generating a reply, and replies are not streamed.

**Interrupting a spoken reply (barge-in).** You do not have to wait for U to
finish speaking. In Hands-free mode, while a reply is being spoken the mic button
reads "Interrupt and talk": tap it (or focus it and press Space or Enter) and the
audio stops at once and U records your next words, which then go through the
normal end-of-speech, transcription, and chat steps. Exactly one recording starts
and it is sent once; the interrupted audio never resumes or triggers another
recording. If the microphone cannot start, U shows the error, pauses Hands-free,
and does not retry. Interrupting a confirmation prompt does not approve the action:
answers follow "Spoken confirmation" above ("confirm" approves the action shown,
"cancel" cancels, a bare "yes" is asked about, anything else stays in the message
box hands-free), and if you say nothing U goes back to waiting for Confirm or
Cancel. In Review mode, pressing the mic while U speaks also stops the audio, but
recording is manual as always (it lasts only while you hold) and nothing is sent
automatically. Turning Hands-free off, starting a new conversation, or switching
away while the interruption's recording starts cancels it without uploading.
Keyboard shortcuts apply only to the focused mic and Stop speaking buttons, so
typing in the message box (including spaces) never interrupts or records.
While U is still generating a reply (Thinking) the mic is disabled: generation
cannot be interrupted, so wait for the reply and interrupt it while it is spoken.

**Wake word ("Kris").** Switch on **Wake word** (next to Hands-free; it turns
Hands-free on too) and say "Kris", like the name Chris, on its own. K.R.I.S.
answers with a short acknowledgement ("Yes?", "Go ahead.", ...) and listens for your request exactly as if you had tapped the
mic; the conversation then continues hands-free (barge-in, Stop speaking and
confirmations work as above). When you have not said anything for
`wake_follow_up_seconds` (default 8, range 3–30, and shorter than `max_seconds`)
after the acknowledgment or a reply, it goes back to standby. Saying "Hey Kris"
or "Okay Kris" also works.

How it is recognised: in standby the page watches the microphone level only. A
short sound on its own (about 0.15–1.4 seconds, with a pause before and after)
is sent as a clip to the local K.R.I.S. server, where the same local
whisper.cpp model transcribes it; it wakes only if the clip is just the name.
The server answers only yes or no: the clip and its text are discarded and never
shown, stored or logged. Longer speech never leaves the page. Nothing is sent to
any other service.

The indicator shows **Standby · say "Kris"** with a hollow dot while it is
monitoring, and Listening with a solid pulsing dot only when your words are
actually being recorded. While standby is on, the macOS microphone indicator
stays on. Standby pauses (and releases the microphone) whenever K.R.I.S. is
recording, transcribing, speaking or in a conversation, so its own voice cannot
wake it, and resumes afterwards.

Limits, honestly:
- "Kris" and "Chris" sound the same, so saying "Chris" on its own will also wake
  it. Mentioning Chris in a sentence ("I called Chris yesterday") does not.
- It can miss the name if you run it into other words ("Kris what's the time"):
  say the name, pause briefly, then speak after the acknowledgement. Very quiet speech, or
  speech in steady loud noise, can also be missed.
- Other short words heard as the name can wake it occasionally; switch it off
  when you do not want to be listened to.
- It works only while the K.R.I.S. page is open and visible (an unfocused but
  visible window is fine). A hidden or minimised window, another tab, or a locked
  or sleeping Mac pauses it; it resumes when the page is visible again, if the
  microphone is still available.
- It starts switched off every time the page loads (browsers only allow audio
  processing after a click); switch it on again after relaunching.
- If microphone access is denied, the microphone disconnects, or local
  recognition stops responding, Wake word switches itself off with an alert and
  never turns itself back on; switch it on again to retry.
- Each checked clip runs whisper.cpp once (about a quarter of a second on an
  Apple Silicon Mac, with a brief memory peak while the model loads); the
  standby level check itself is light.

The detector runs in the browser on the same microphone stream that is
recorded. It compares loudness with the noise level measured at the start of
each recording, so it is a simple energy check, not true speech recognition, and
it is not perfectly reliable:
- steady background sound (a fan, a TV, other people talking) can keep a
  recording open until you tap or `max_seconds` is reached;
- speech that is not clearly louder than steady background noise (about 6 dB),
  or very quiet speech close to the room's noise level, may not be recognised as
  speech, and the recording is then discarded after 6 seconds; use press-and-hold
  in that case;
- a pause longer than `end_silence_ms` ends the recording mid-sentence; raise it
  if that happens often;
- if the browser has not allowed audio processing to start, the page says
  automatic stop is unavailable and you tap to stop.

The server's own silence check below still applies to every recording that is
uploaded. Both timings are set in the `[voice]` section of `config.ini`
(`end_silence_ms` 500–4,000, `min_recording_ms` 200–3,000 and shorter than
`max_seconds`); other values are ignored with a warning. Switching to another tab
or window, or the microphone disconnecting, stops a recording without uploading
it, in both modes.

**Confirmations by voice.** Only the Confirm button approves an action. While an
action waits for confirmation:
- saying just "yes", "okay", "go ahead", or a similar short approval does not
  approve it and is not sent; U asks you to press Confirm instead;
- saying just "no", "cancel", "never mind", or a similar short refusal acts
  exactly like the Cancel button, so the action is not run;
- saying anything longer, such as "no, open the March one instead", is an
  ordinary new message, which cancels the waiting action (see "Changing your
  mind") and is then answered.

Only a short reply on its own is treated this way; the classification is a fixed
list of phrases, not the model's judgement.

**Silent recordings.** whisper.cpp tends to transcribe silence as "you", so
before running it U measures the converted recording. Unless at least 0.12
seconds of it reaches −57 dBFS (measured in 30 ms windows), U skips whisper.cpp
and reports "No speech was detected" in both modes; nothing is sent. The
threshold was chosen from a quiet room's microphone noise floor (loudest window
about −59 dBFS) and the quietest speech whisper.cpp still transcribed correctly in
that room (about −55 dBFS). Brief noises such as typing can still pass the check;
whisper.cpp then usually returns nothing, which is reported the same way. If your
quiet speech is rejected, lower `SPEECH_LEVEL_DBFS` in `voice.py`; U logs each
recording's measured level (numbers only) at INFO level to help with tuning.

"Speak replies" (off by default) reads each final reply aloud. When speech is
on, the confirmation prompt and cancellation messages are spoken too. Turning
the toggle off, turning Hands-free off, recording, or starting a new conversation
stops playback.

**The voice.** Replies are spoken by Kokoro-82M (voice `af_heart`, American
English) running on the CPU. The model loads in the background when the server
starts (about 2 seconds) and then uses about 550–750 MB of memory. A reply is
spoken **sentence by sentence**: K.R.I.S. splits it at sentence ends (not after
"Dr.", "a.m.", initials or decimal points, and long sentences at a comma or
semicolon), synthesizes the first sentence, starts playing it, and synthesizes
the next one while the current one plays, with a short pause between sentences.
This is not streaming from the language model: the reply is complete before it
is spoken. Measured on an M3, the first audio starts about 0.3–0.8 s after the
reply arrives when the first sentence is short or has a comma to split at, and up
to about 2 s for a long first sentence without one; each second of speech takes
about 0.25 s to synthesize.
Stop speaking, interrupting (tapping the mic), or a newer reply stops the audio
at once and cancels the sentences not yet synthesized; a sentence already being
synthesized (well under a second) finishes on the server and is discarded.
Kokoro pauses at every full stop, colon or comma inside a word, so the spoken
text (never the text shown in the chat) is rewritten first: "Dr. Patel" becomes
"Doctor Patel" (also Mr., Mrs., Ms., Prof.), "J. Smith" becomes "J Smith",
"7:45 a.m." becomes "seven forty-five AM", "9:30" becomes "nine thirty",
"$1,249.99" becomes "1249 dollars and 99 cents" (also pounds and euros),
"1,200" becomes "1200", dates become "the 12th of March" (from "12 March") or
"March 12th" (from "March 12"), "ada@example.org" becomes "ada at example dot
org", and plain decimals become "3 point 5". Version numbers
("1.2.3") and ratios ("3:2") are left as written. Long replies are spoken up to
about 4,000 characters.

Set `kokoro_voice` in `[voice]` to change the voice (for example `af_bella`,
`am_michael`, or British `bf_emma`; British voices use British pronunciation)
and `kokoro_speed` (0.5–2.0, default 1.0). If the Kokoro package or model files
are missing, or Kokoro fails, replies are spoken by macOS `say` instead (with
`say_voice` if set; list voices with `say -v '?'`), and the server logs why
(never the text). `tts_engine = say` always uses `say`.

Recordings are limited to 3 MiB and WebM, Ogg, or MP4 audio; conversion,
transcription, and speech each have a timeout, and one transcription runs at a
time. Audio and text exist only in a private temporary directory during a
request and are deleted afterwards; they are never logged or stored (only the
loudness numbers above are logged). Requests that change anything, including
uploading a recording, are refused (HTTP 403) when they come from another
website, so other pages cannot make U transcribe or speak. To run the
local speech round-trip test (no microphone needed):

```sh
VOICE_SMOKE=1 .venv/bin/python -m unittest tests.test_voice -v
```

Assistant responses render Markdown with locally served `markdown-it` 15.0.2
and DOMPurify 3.4.16 bundles in `static/vendor/`. Raw HTML is disabled, parsed
output is sanitized against an allowlist, and unsafe URL schemes are removed.
Safe HTTP(S) links open in a new tab with `noopener noreferrer`. The browser
check covers headings, emphasis, nested lists, links, code, tables, blockquotes,
horizontal rules, multiline text, malformed responses, and raw-HTML/script
injection. The sampled live model response did not contain LaTeX-style math.
Math is not rendered; delimiters remain ordinary text, since a math engine
would add disproportionate assets for this milestone.

To verify, confirm the header reports the configured model as ready, then send
`What is 17 × 23?` followed by `What is 17 × 24? Find the difference between the
previous and current results.` U should use the previous answer and return
`391`, `408`, and `17`. The health check is also available at
<http://127.0.0.1:5000/api/health>.
