# Millikan Oil Drop AI Assistant — JavaScript Frontend

A web-based frontend for the Millikan Oil Drop Experiment application.

This version replaces the original Streamlit UI with a browser-based interface built with:

- HTML
- CSS
- JavaScript

The existing Python CV and LLM backends are kept in place. The JavaScript frontend communicates with them through `web_server.py`.

---

## Project Structure

All files for this JavaScript version are organized under `app_js/`.

```text
project/
├── app_js/
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   ├── web_server.py
│   ├── wrapper.py
│   ├── stream_server.py
│   └── fixtures/
│       └── demo_clip.mp4        (clip played by "Try Demo Video")
├── cv/
│   ├── api.py
│   ├── pipeline.py
│   ├── UVC_check.py
│   └── ...
└── llm/
    ├── main.py
    ├── src/
    │   └── ingestWrapper.py
    └── ...
```

### Files inside `app_js/`

| File | Purpose |
|---|---|
| `index.html` | Main page structure and AI assistant panel |
| `styles.css` | Complete visual styling, layout, animation, responsive behavior |
| `app.js` | Frontend state, live feed controls, droplet panel, notifications, chat interaction, streaming response rendering |
| `web_server.py` | Python HTTP bridge between the browser and the existing Python CV/LLM backend. Also serves the frontend files and decides the droplet-suitability verdict |
| `wrapper.py` | Runs the bench check, starts/stops the live CV pipeline, receives its frames and forwards browser clicks to it |
| `stream_server.py` | Serves the newest camera frame to the browser as an MJPEG stream |

### Existing backend files

The frontend does **not** replace the existing CV or LLM implementation.

- `cv/pipeline.py` is the live droplet tracker. It is launched as a subprocess by `app_js/wrapper.py`.
- `cv/UVC_check.py` is the bench check that finds the camera/dongle.
- `cv/api.py` (`live_status()`) exposes the pipeline's published status to `web_server.py`.
- `llm.main.getResponseFromGemini()` provides the streamed AI response.
- `llm.src.ingestWrapper.runIngestion()` builds the lab-manual vector store the AI assistant retrieves from (see [Lab-manual ingestion](#lab-manual-ingestion)).

---

## Requirements

### Python

Python must be installed and available from the command line.

Install the project dependencies from `requirements.txt`:

```bash
pip install -r requirements.txt
```

This covers the packages used by the CV pipeline, `wrapper.py`, the LLM module and `stream_server.py`. The live camera pipeline is Windows-only in its current form.

### Browser

Use a modern browser such as:

- Google Chrome
- Microsoft Edge
- Firefox

JavaScript must be enabled.

The frontend loads MathJax from the jsDelivr CDN for LaTeX rendering, so an internet connection is needed for formulas to render.

---

## Setup

`app_js/` is self-contained: `wrapper.py` and `stream_server.py` live in it
alongside the frontend, so there is nothing to move or copy.

Two requirements:

- `cv/` and `llm/` must be importable from the same Python environment that
  starts the web server.
- The web server must be started **from the project root**. The code imports
  `app_js.wrapper`, `cv.api` and `llm.main` by package name, the pipeline is
  launched as `python -m cv.pipeline ...` in the server's working directory, and
  the demo clip path (`app_js/fixtures/demo_clip.mp4`) is relative to it.

---

## Running the Application

Open a terminal in the project root and run:

```bash
python -m app_js.web_server
```

Running `python app_js/web_server.py` also works if the project root is already on Python's import path (for example through `PYTHONPATH`); `-m` from the project root guarantees it.

The server runs at:

```text
http://127.0.0.1:8502
```

Open that address in your browser. The server only listens on `127.0.0.1`.

Press `Ctrl+C` to stop it. If a live session is running, it is stopped as well.

### Lab-manual ingestion

Importing `wrapper.py` (which happens when the web server starts) calls `runIngestion()` once so the AI assistant's vector store exists. It does nothing if the manual was already ingested. A failure here (missing dependency, missing PDF, failed embedding) is printed to the console and swallowed: it never prevents the camera pipeline or the page from working, but the chat tutor may then lack lab-manual context.

---

## Live Microscopic Feed

The **Start live feed** button performs the following flow:

```text
Browser
   │
   │ POST /api/live/start
   ▼
web_server.py
   │
   │ wrapper.start_live()
   ▼
Bench check (cv.UVC_check)  -- skipped when a camera index or clip is given
   │
   ▼
python -m cv.pipeline live N --publish PORT      (or:  play <clip> --publish PORT)
   │
   │  length-prefixed JPEG frames over a local TCP connection
   │  (wrapper.py listens, the pipeline connects)
   ▼
stream_server.FrameStreamer   (newest frame only)
   │
   │ MJPEG over HTTP
   ▼
Browser <img>
```

The browser displays the streamed image, and clicking on it selects a droplet.

The pipeline still opens its own native `Droplets` window. `wrapper.py` waits for it to appear (up to 45 seconds) and then sends it to the bottom of the window stack; it is a backend rendering surface, not something the operator needs to use.

**Fallback:** if the pipeline never connects to publish frames (an older pipeline, a firewall), `wrapper.py` prints a message and instead screenshots the `Droplets` window and serves those screenshots. That path is slower — one mirrored frame costs about 59 ms, which caps it near 17 fps (`MIRROR_FPS = 15`) — whereas a published frame costs the pipeline about 4 ms to encode.

Clicking the live feed selects a droplet in the pipeline:

```text
Browser click on <img>
   │
   │ POST /api/live/click  { x, y, token }     x, y are FRACTIONS of the frame
   ▼
web_server.py
   │
   │ wrapper.send_click(handle, x, y)
   ▼
one JSON object per line down the pipeline's stdin
   │
   ▼
pipeline.py's own readRemoteClicks(), which turns the fraction back into a
pixel and drops it into the same queue cv2.setMouseCallback fills
```

`pipeline.py` is not aware the click came from a browser: it already accepts remote clicks on stdin for exactly this purpose, and treats one identically to a click on its own window.

Fractions, not pixels: the sender is scaling the picture to whatever size its
panel happens to be, and a fraction survives that. The frontend maps the click
through the `<img>`'s `object-fit: contain` letterboxing first, so it lands on
the right part of the frame even when the feed is scaled or padded to fit. A
click on the letterbox margin is ignored. Each accepted click shows a small
ripple on the feed.

`wrapper.click_live()` does the same job by posting Win32 mouse messages to the
native window. It is still there, but the browser does not use it -- the stdin
route needs no window to have focus and is the one `pipeline.py` documents.

### The stream server

`stream_server.py` is display-only: it has no endpoint that accepts input, so
nothing a viewer does in the browser reaches the pipeline process. It listens on
`127.0.0.1`, on the first free port from 8899 upwards, and serves:

| Route | Response |
|---|---|
| `GET /stream` (or `/`) | `multipart/x-mixed-replace` MJPEG stream |
| `GET /frame.jpg` | The newest single frame; `503` until one exists |
| anything else | `404` |

The stream URL is returned by `POST /api/live/start` as `url`.

### Feed labels and the session timer

The caption under the feed changes with the mode:

| Mode | Caption |
|---|---|
| Idle | `[ MICROSCOPIC VIEW ] · OFFLINE` |
| Camera | `[ LIVE MICROSCOPIC VIEW ] · LIVE` |
| Demo clip | `[ DEMO MICROSCOPIC VIEW ] · DEMO` |

While a session runs, a badge at the top-left of the feed shows the elapsed time
(`mm:ss`, or `hh:mm:ss` after an hour). It is a plain wall-clock readout; it does
not come from the CV pipeline. The feed box is shaped to the frame's real size
(taken from the stream, or from `frameWidth` / `frameHeight` in the status), so
there is no letterboxing; while idle it keeps a fixed 909 × 645 shape.

### Starting the feed

Click:

```text
▶ Start live feed
```

with the source field left blank. The backend first performs its normal camera/dongle check (`python -m cv.UVC_check`, whose last line must name the device to use). If the required device is unavailable, the frontend shows a user-facing message instead of exposing backend details.

When successful, the button changes to:

```text
■ Stop live feed
```

Pressing **Enter** in the camera-index box does the same as pressing the button.

#### While the feed is starting

Starting takes a while (the bench check, then background learning) and the server
does not report progress, so the page shows an **estimated** progress bar over the
feed:

- The default estimate is **45 seconds** (`START_ESTIMATE_SECONDS.camera` in
  `app.js`). The bar reaches about 90 % at the estimate and keeps creeping toward
  95 %, never completing by itself, and jumps to 100 % the moment
  `POST /api/live/start` answers. A caption counts down ("About N s left"); if the
  start runs past the estimate it reads "Taking a little longer than usual…". The
  camera start text also tells the operator not to spray yet, because the
  pipeline is learning the background.
- The bundled demo clip uses a 30-second estimate (`START_ESTIMATE_SECONDS.source`).
- Coming from the idle feed box, the darker inner box first expands out to the
  edges of the feed (about 0.35 s); the spinner, text and bar then fade in. With
  the operating system's "reduce motion" setting on, this is effectively instant.
- The bar has its own colours per theme (`--progress-track` / `--progress-fill` in
  `styles.css`): the original purple-to-sky-blue in dark mode, a light trough with
  a deep indigo-to-violet fill in light mode.
- The camera-index box and the demo button are disabled until the start finishes.

While a feed is starting the main button turns into **✕ Cancel**. The server
cannot abort a start half-way, so Cancel does not stop it instantly: the page
shows "Cancelling…" (with an indeterminate bar) and, as soon as
`POST /api/live/start` answers, immediately calls `POST /api/live/stop` with the
token it just received. Nothing is left running. An instant cancel would need a
server-side endpoint for it.

If the pipeline's window does not appear within 45 seconds, the start fails; the
pipeline prints its own progress to the console the server was started from.

### Developing without the apparatus

The source field next to the button is a **camera index**. It accepts:

- nothing (default) — runs the bench check against a real camera/dongle.
- a whole number (`0`, `1`, `2`, …) — used directly as a camera index, skipping
  the bench check. Spaces around the number are trimmed and leading zeros are
  dropped (`007` is sent as `7`).

Anything else is rejected **in the browser, before any request is made**: text,
decimals (`1.5`, `1.0`), signs (`-1`, `+1`), exponents (`1e3`), hex (`0x1`), lists
(`1,2`), non-ASCII digits, and numbers too large to be safe integers. The field
gets a red outline and keyboard focus, and this appears under the controls:

```text
Camera index must be a whole number (0, 1, 2…). Leave it empty to use the default camera.
```

The message clears as soon as the operator types again. The same rule applies
whether the feed is started with the button, the empty-state button or Enter.

**Clip specs are no longer typed into this field.** The backend still accepts a
clip spec (a file path, or `frames:DIR`) as `source` on `POST /api/live/start`
and runs it through `python -m cv.pipeline play <source>`, but the browser only
ever sends one from the demo button.

The **🎬 Try Demo Video** button starts the bundled clip
(`DEMO_CLIP_SOURCE`, currently `app_js/fixtures/demo_clip.mp4`, resolved against
the server's working directory) with one click, so the whole stack (server,
stream, click-to-pick) can be exercised from a desk. It does not read or change
the source field, so it works even if invalid text has been left in it.

When the feed is idle, the feed box plays a looping footage clip
(`/app_test/test_footage.mp4`, served from `app_js/app_test/`) under an `OFFLINE`
label. If the clip cannot be loaded, the empty "No camera feed detected" state is
shown instead, with its own **▶ Start Live Feed** button.

### Stopping the feed

Click:

```text
■ Stop live feed
```

The frontend calls:

```text
POST /api/live/stop
```

and the backend tears down the current live session (stream server stopped,
pipeline process terminated). The same happens if the web server itself is shut
down.

### Reloading, or opening a second tab, during a session

`GET /api/live/status` carries no stream URL and no click token, so a page that did
not start the session cannot get them from it. `POST /api/live/start`, however, is
idempotent: when a session is already running it simply returns that session's
`url`, `device`, `source` and `token`. When the poll sees `running: true` while
this page has no session, it calls `POST /api/live/start` once and attaches exactly
as if it had started the session itself:

- the feed picture, the **■ Stop live feed** button and click-to-pick all work;
- a "Reconnected to the session that is already running." notification appears;
- the session timer continues from the real start time. The start time is kept in
  `localStorage` (`millikanSessionStart`, ignored if older than 12 hours). If the
  session was started somewhere that left no start time, no timer is shown rather
  than a wrong one;
- the droplet notes are announced again, because a freshly loaded page treats the
  current selection as new.

If attaching fails, it is retried no more than once every five seconds.

### Connection health

Two different failures both look like a frozen picture under a `LIVE` label, so
both are surfaced as a red pill at the top of the feed and both freeze the session
timer until they clear:

| Pill text | Cause | Recovery |
|---|---|---|
| Connection lost — reconnecting… | `/api/live/status` stopped answering (two failed polls during a session, one when idle; idle reads "Can't reach the server — retrying…") | Clears on the next successful poll |
| Video stream interrupted — reconnecting… | The stream `<img>` failed to connect or was refused | The page reloads the stream every 3 seconds; the pill clears once it has run for a few seconds without failing again |

**Limitation:** a stream that dies or freezes *mid-session* while the server keeps
answering is **not** detected. Browsers raise no event for an interrupted
multipart (MJPEG) `<img>`, even when the connection is reset. Detecting it needs a
frame counter or "last updated" time in the status the CV API publishes; the page
could then compare it between polls.

---

## Selected Droplet Panel

The panel on the right (above the `[ DROPLET ANALYSIS ]` caption) shows the
droplet the operator currently has picked in the pipeline. It is filled entirely
from the CV API; the browser does not compute any experiment value.

```text
Click in the feed (or the pipeline's own auto-pick)
   │
   ▼
cv/pipeline.py keeps it as `picked` and republishes it ~1/s
   │
   ▼
cv/api.live_status()
   │
   ▼
web_server.selection_view()   (labels the droplet, holds readings, decides `suitable`)
   │
   ▼
GET /api/live/status  ->  "selected"
   │
   ▼
Selected Droplet panel
```

The page polls `/api/live/status` every 500 ms while a session is running (the
pipeline itself rewrites its status about once a second) and every five seconds
when one is not, so the panel follows the pipeline without anyone reloading
anything.

The droplet is labelled `DROPLET n`, where `n` counts the distinct droplets picked
since the session started. The tracker's internal id is never shown.

### Fields

| Panel field | CV API source |
|---|---|
| Voltage | `voltageV`, last value seen while `fieldState` is `work` |
| Fall time | `picked.fallSeconds`, last value seen while `fieldState` is `off` |
| Velocity | `picked.speedGridPerSec`, last value seen while `fieldState` is `off` |
| Field | `fieldState`, shown as **Off (falling)**, **Working** or **Lifting** |
| Status | the server's `suitable` verdict (see below) |

A value the CV API did not provide is shown as:

```text
—
```

It is never filled in with a guess, a default, or a value worked out in
JavaScript.

### Held readings, target bands and tinting

Each measurement is only meaningful in one field state, so the panel keeps the
**last genuine reading** and marks it while the field is in another state:

| Reading | Updates only while the field is | Otherwise |
|---|---|---|
| Voltage | `work` | shown lighter with a small `HELD` tag |
| Fall time | `off` | shown lighter with a small `HELD` tag |
| Velocity | `off` | shown lighter with a small `HELD` tag |

This matters most for velocity: while the field is working or lifting the droplet
is held or rising, so its live speed swings wildly and says nothing about whether
it is a good droplet. Held values are cleared whenever a different droplet is
picked or the session ends.

Voltage and velocity also print their **target band** in small, faint text under
their label (`target 150–250 V`, `target 0.2–0.5 grid/s`), and the value is tinted
green when it is inside the band and red when it is outside (darker shades in
light mode). Fall time has no band and is not tinted. This shows the student
*why* a droplet is or is not suitable.

The bands are `BALANCE_VOLTS_RANGE` and `FALL_SPEED_RANGE` in `app.js`. They are
**display-only**: the tint is a comparison against the lab manual's bands, not a
verdict. The verdict (`suitable`) is still decided by `web_server.py`. Keep the
three copies in step (see [Development Notes](#development-notes)).

### Status

The lab manual asks two questions about a candidate droplet:

```text
falling speed at 0 V     0.2 - 0.5 grid/s
balance voltage          150 - 250 V
```

They are answered at different moments -- the balance voltage is read with the
field on, the fall is timed with the field off -- so `web_server.py` remembers each
answer for as long as the same droplet stays picked, and forgets both the moment
the pick changes:

- a speed counts as the free-fall speed only while `fieldState` is `off`;
- a voltage counts as the balance voltage only while `fieldState` is `work`.

The verdict is only given once **both** have been observed; it is never inferred
from one alone. It also has to hold on the same side of the bands for
`SETTLE_SECONDS` (3 s) before it changes, so a single noisy sample cannot flip the
light.

| Ring | Status text | Meaning |
|---|---|---|
| Green | Suitable | Both readings were observed, both are in band, and have stayed there for 3 s |
| Red | Not suitable | Both readings were observed, at least one is out of band, and has stayed so for 3 s |
| Yellow | Not enough data | One or both readings have not been observed yet |

Yellow is the honest answer, not a soft failure: a droplet that has never been
cut to 0 V has not shown its falling speed, and one that has never been held with
the field on has not shown its balance voltage. The server's `note` says which is
missing (and, once both exist, which one is out of range, or how many seconds
remain before a pending change is confirmed); it is shown as the ring's tooltip
and in the notifications.

---

## Guidance Line and Notifications

### Guidance line

Under `[ DROPLET ANALYSIS ]` one line shows the pipeline's own guidance
(`status.message`), replaced in place; a superseded instruction is not kept. Its
colour follows the wording: green for good news, amber for warnings, red for
discard-type messages (`bannerClass()` in `app.js` matches on the pipeline's
phrases). The tracker's `picked id 4015` is rewritten to the panel's `DROPLET n`.
Before a session it shows a placeholder in a softer (semibold, italic) style; while
a session is running but the pipeline has said nothing yet it reads "Waiting for
the aid…". The line uses the same glass background as the other panels, in both
themes.

### Notifications

The `[ NOTIFICATIONS ]` panel keeps the six most recent notes, newest on top (older
cards slide down, and the oldest fades out when the panel is full). Each card has
an icon for its kind (info, success, warning, error) and a timestamp. The
suitability notices are driven by the server's verdict (`selected.suitable`) and
its `note`:

| Event | Notification |
|---|---|
| A droplet becomes suitable | `Valid measurement — DROPLET n is suitable.` plus the server's note |
| A droplet becomes not suitable | `DROPLET n is not suitable.` plus the server's note, which names the reading(s) out of range |
| A suitable droplet stops being suitable | `DROPLET n is no longer suitable.` plus the note |
| Verdict is `unknown` | nothing |

The browser does **not** judge fall speed itself. `web_server.py` only counts a
speed measured while `fieldState` is `off` (and a voltage measured while it is
`work`), so the out-of-range speed of a droplet being lifted can never produce a
false "not suitable". The panel and the notifications therefore always agree.

Other notifications: `DROPLET n selected — <note>` when a new droplet is picked,
session ended, start cancelled, reconnected to a running session, and start/stop
errors. Before anything has happened the panel shows a placeholder asking the
operator to start a session and click a droplet.

The `[ NOTIFICATIONS ]` caption is kept level with the **Try Demo Video** button by
a small measurement in `app.js`, because that button's position moves when the
status line appears or the controls wrap.

---

## AI Assistant

The AI assistant is opened using the floating:

```text
🛸
```

button (or closed again with the same button, the × in its header, or **Esc**).
Until it is first opened, a small prompt bubble ("Ask AI about the experiment")
hovers beside the button.

The panel contains:

- chat history
- starter prompts
- user input area
- character counter
- Send button
- streaming assistant response
- thinking animation

The chat history lives only in the page: reloading clears it. Every request uses
the same fixed session ID, `millikan_test_session`.

### Starter prompts

An empty chat shows three one-click questions under "Try asking":

- What makes an oil droplet suitable?
- How do I select a good droplet?
- How do I calculate the charge on a droplet?

Clicking one sends it exactly as if it had been typed. The prompts disappear once
the first message is sent. They are the `.starter-chip` buttons in `index.html`.

### Thinking animation

While waiting for the first chunk the reply bubble shows three animated dots, an
elapsed-seconds counter, and a status line that rotates every 7 seconds ("Thinking…",
"Reviewing droplet fall data…", …). It is replaced in place by the first chunk of
the answer, an error, or a timeout, so there is never a second empty bubble.

The interface is designed to behave like the previous Streamlit implementation while removing the Streamlit dependency from the frontend.

---

## AI Input Rules

The maximum query length is:

```text
2000 characters
```

Length is counted in Unicode code points, the same way Python's `len()` counts
it, so the browser and `web_server.py` agree (an emoji counts as one).

The character counter is updated live and turns to its over-limit style at 2000.
The textarea itself accepts one character more than the limit (`maxlength="2001"`)
so that going over is visible and blocks sending, rather than the extra character
being silently dropped.

Examples:

```text
0 / 2000
25 / 2000
1200 / 2000
2000 / 2000
```

A query is sendable only when:

```text
1 <= character count <= 2000
```

The Send button is disabled when:

- the input is empty
- the input exceeds 2000 characters
- an AI response is currently being generated (the button reads "Generating…")

The server enforces the same limit and answers `400` for a query outside it.

---

## Keyboard Behavior

### Enter

Pressing:

```text
Enter
```

sends the current query. In the camera-index box it starts the live feed instead.

### Shift + Enter

Pressing:

```text
Shift + Enter
```

creates a new line without sending the message.

### Escape

Pressing:

```text
Esc
```

closes the AI assistant panel.

---

## AI Streaming

The frontend consumes the response from:

```text
POST /api/chat
```

The backend returns a streamed response.

The browser processes the response incrementally:

```text
User sends question
        ↓
Thinking animation
        ↓
First AI chunk
        ↓
Thinking bubble becomes AI response
        ↓
Additional chunks update the same message
        ↓
Done
```

The response is rendered progressively rather than waiting for the entire answer before displaying it.

Each Server-Sent Event is a `data:` line holding JSON of the form `{ "type": ..., "data": ... }`,
where `type` is one of:

| `type` | Meaning |
|---|---|
| `text` | A chunk of the answer; appended to the same bubble |
| `done` | The answer is complete |
| `error` | Generation failed; the browser shows a generic message |
| `timeout` | The 60 s or 5 s rule below fired |

While the stream is otherwise quiet the server sends `: heartbeat` comment lines so
the connection is not treated as idle.

If the request fails outright (a `400` for a bad query, or a `500` because the
assistant is not configured, for example a missing or invalid API key), no stream is
opened. The server waits up to about 2.5 seconds for such an immediate failure and
answers with plain JSON `{ "error": ... }`, which the browser shows in the reply
bubble.

---

## AI Timeout Behavior

The frontend/backend bridge uses two timeout rules.

### Initial response timeout

If the AI does not produce its first response within:

```text
60 seconds
```

the request is treated as timed out.

### Inter-chunk timeout

After generation begins, if no new chunk is received for:

```text
5 seconds
```

the stream is treated as stalled.

The frontend displays a user-friendly timeout message instead of exposing Python exceptions or backend implementation details:

| Timeout | Message shown |
|---|---|
| First response | The AI assistant took too long to respond. Please try again. |
| Stalled stream | The AI assistant stopped responding. Please try again. |

### Active generations

A session holds at most one generation slot, claimed when a request starts real
work and released in a single `finally` that every exit path runs through:

```text
before a request          0
while one is running      1
normal finish             0
60s or 5s timeout         0
missing/invalid API key   0
browser closed mid-stream 0
any other exception       0
```

A request that is rejected for length never starts a generation, so it never
claims a slot at all. The count can be read at any time:

```http
GET /api/chat/status?sessionID=millikan_test_session
```

A timed-out request releases its slot at once. Its worker thread may still be
stuck inside the Gemini call, so that thread -- and only that thread -- is kept
separately, cancelled when the next request for the session arrives, and then
forgotten whether or not it cooperated. One dead request therefore cannot slow
or block the requests that follow it.

---

## LaTeX Rendering

AI messages support LaTeX using MathJax. Formulas written between `$ … $` (inline)
or `$$ … $$` (display) are handed to MathJax, which also understands `\( … \)` and
`\[ … \]`.

For example:

```text
The equilibrium voltage is \(U_n\).
```

is rendered as:

```text
The equilibrium voltage is Uₙ.
```

Inline expressions stay inline with surrounding text.

Scientific notation such as:

```text
U_n
t_1
q
e
v
```

must remain part of the surrounding sentence rather than being interpreted as Markdown line breaks.

### Long equations

Only a LaTeX expression that is genuinely too wide for the chat bubble receives horizontal scrolling.

Short expressions such as:

```text
\(q\)
\(t_1\)
\(U_n\)
```

do not show a horizontal scrollbar.

Normal text, Markdown, lists, and other chat content remain vertically scrollable without horizontal scrolling.

---

## Supported Markdown

The message renderer supports the same basic formatting used by the original interface, including:

- headings
- ordered lists
- unordered lists
- bold text
- italic text
- inline code

Fenced code blocks, tables and links are not rendered specially. All text is
HTML-escaped before rendering.

Example:

```markdown
### Example

**Important:** use a suitable droplet.

1. Set the voltage.
2. Observe the droplet.
3. Record the result.
```

---

## Guide Section

The main page includes the experiment guide:

1. What kind of oil droplet is suitable?
2. Selecting a suitable oil droplet
3. Precautions

The sections are expandable and use the same guidance content as the previous frontend.

---

## API Endpoints

`web_server.py` exposes the browser-facing endpoints below. Any other `GET` path is
treated as a request for a static file from `app_js/` (`/` is `index.html`); paths
that escape that folder are refused with `403`, and missing files give `404`.
Responses carry `Cache-Control: no-store` and permissive CORS headers.

### Health

```http
GET /health
```

Example response:

```json
{
  "ok": true
}
```

### Live status

```http
GET /api/live/status
```

Answers on every call, running or not. Example response:

```json
{
  "running": true,
  "device": 1,
  "source": 1,
  "status": { "phase": "running", "voltageV": 212, "fieldState": "work" },
  "selected": {
    "picked": true,
    "label": "DROPLET 1",
    "fieldState": "work",
    "voltageV": 212,
    "fallSeconds": null,
    "speedGridPerSec": 0.313,
    "balancePercent": null,
    "suitable": "unknown",
    "note": "Balanced at 212 V. Cut to 0 V to measure its falling speed."
  }
}
```

`suitable` is `"yes"`, `"no"` or `"unknown"`. Any measurement the CV API did not
provide is `null`, which the page renders as a dash. `status` is the raw status
published by `cv/api.live_status()` (the page also reads `message`, `frameWidth`
and `frameHeight` from it) and is `null` when nothing is running.

### Chat status

```http
GET /api/chat/status?sessionID=millikan_test_session
```

Example response:

```json
{
  "sessionID": "millikan_test_session",
  "activeGenerations": 0
}
```

### Start live feed

```http
POST /api/live/start
```

On success, the response contains the MJPEG stream URL, device information and the
session token. The body may carry `{ "source": ... }`; the browser sends only a
validated camera index (or the demo clip). On failure the response is `500` with an
`error` message.

The call is **idempotent**: if a session is already running it returns that
session's `url`, `device`, `source` and `token` instead of starting another. The
browser relies on this to reattach after a reload (see
[Reloading, or opening a second tab](#reloading-or-opening-a-second-tab-during-a-session)).

### Click on the feed

```http
POST /api/live/click
```

Request:

```json
{ "x": 0.42, "y": 0.63, "token": "<session token>" }
```

`x` and `y` are fractions of the frame (0 to 1). Answers `{ "ok": true }`, or `409`
when no session is running, `403` for a wrong token, `400` for a malformed body.

### Stop live feed

```http
POST /api/live/stop
```

The current live-session token is required (`{ "token": ... }`); a wrong token is
refused with `403`. Stopping when nothing is running simply answers `{ "ok": true }`.

### AI chat

```http
POST /api/chat
```

Request:

```json
{
  "userQuery": "What is the purpose of the Millikan oil drop experiment?",
  "sessionID": "millikan_test_session"
}
```

The response is streamed using Server-Sent Events (`text/event-stream`); see
[AI Streaming](#ai-streaming).

---

## Error Handling

The frontend intentionally avoids exposing implementation details such as:

- Python tracebacks
- subprocess commands
- internal module names
- backend object state
- raw exception messages

The user should receive simple messages describing what they need to do next. Tracebacks are printed to the server console only.

For example, when the camera dongle is unavailable:

```text
Please plug in the dongle first.
```

That friendly message is used when the feed is started with the source field
empty. When a camera index or a clip is given, the start error names it and
includes the underlying reason (for example, that the pipeline's window never
appeared), because there the operator is developing rather than running the bench.

---

## Design

The interface uses:

- glassmorphism panels
- light and dark themes (toggle in the title box; the choice is saved in
  `localStorage` as `millikanTheme`, and defaults to the system setting)
- animated radial gradient background
- rounded panels
- purple accent color
- floating AI assistant button
- animated AI prompt bubble
- responsive chat panel
- animated thinking dots

The microscopic feed and droplet-analysis panels follow the same visual structure as the previous Streamlit version.

---

## Development Notes

### Do not move CV logic into JavaScript

The purpose of this version is to replace the UI layer only.

Keep:

```text
Python CV → Python
Python LLM → Python
Browser UI → HTML/CSS/JavaScript
```

The browser communicates through `web_server.py`.

### Do not expose backend internals

Frontend-facing errors should remain user-friendly.

Avoid returning raw Python exception strings to the browser unless they are intentionally being translated into a safe user-facing message.

### Keep the display bands in step

The target bands exist in three places, and only the second decides anything:

```text
cv/pipeline.py      MIN_SPEED/MAX_SPEED, ADVISORY_MIN_V/ADVISORY_MAX_V
web_server.py       FALL_SPEED_MIN/MAX, BALANCE_VOLTS_MIN/MAX   <- decides `suitable`
app_js/app.js       FALL_SPEED_RANGE, BALANCE_VOLTS_RANGE        <- display only
```

If the lab manual's bands change, update all three. The `cv/api.py` layer does not
re-export the pipeline's constants, which is why they are repeated. The 3-second
debounce (`SETTLE_SECONDS`) also lives in `web_server.py`.

### Browser storage

| Key | Purpose |
|---|---|
| `millikanTheme` | `light` or `dark` |
| `millikanSessionStart` | Start time of the running session, so a reloaded page can keep the timer going |

### Live stream architecture

`stream_server.py` serves the newest JPEG frame over MJPEG.

There is one writer (the frame feed in `wrapper.py`) and any number of readers (a browser tab, and whatever it reconnects to). Readers wait on a condition variable rather than pulling from a queue, so a slow or momentarily-absent reader can never make the writer block or build up a backlog. The stream is intentionally designed so slow browser readers do not create an ever-growing frame backlog: the browser displays the latest available frame rather than trying to process every frame. A dropped frame is invisible; a growing lag is not.

`push_jpeg()` takes the pipeline's already-encoded frames with no decode or re-encode; `push()` (BGR image → JPEG) is used only by the window-capture fallback.

---

## Troubleshooting

### "Camera index must be a whole number"

The source field takes only a whole number (or nothing). Clear the field to use the
default camera, or type `0`, `1`, … for a specific one. To play a recorded clip use
the **🎬 Try Demo Video** button; clip paths are no longer typed into the field.

### A red "Connection lost" or "Video stream interrupted" pill appears

See [Connection health](#connection-health). "Connection lost" means the page
cannot reach `web_server.py` (is it still running?); it clears by itself once the
server answers. "Video stream interrupted" means the camera stream refused the
connection; the page retries every three seconds.

### The progress bar does not match how long the start really takes

It is an estimate (45 seconds for a camera, 30 seconds for the demo clip). It jumps
to 100 % when the start actually finishes. Adjust `START_ESTIMATE_SECONDS` in
`app.js` if your hardware is consistently faster or slower.

### The page does not load

Check that the web server is running, from the project root:

```bash
python -m app_js.web_server
```

Then open:

```text
http://127.0.0.1:8502
```

If it exits immediately with an import error for `app_js`, `cv` or `llm`, the
project root is not on Python's import path; see [Setup](#setup).

### The live feed does not start

Make sure:

- the camera/dongle is connected
- the dependencies in `requirements.txt` are installed
- the existing CV pipeline works independently (`python -m cv.pipeline live N`)
- `wrapper.py` can import `app_js.stream_server`
- the server was started from the project root
- for **Try Demo Video**, `app_js/fixtures/demo_clip.mp4` exists

If the pipeline's `Droplets` window does not appear within 45 seconds the start
fails; the pipeline prints what it is stuck on to the console the server was
started from.

### The feed is choppy

If the console shows "Pipeline did not publish frames; mirroring its window
instead", the browser is receiving screenshots of the pipeline window (about 15
fps at best) instead of the pipeline's own frames. Check that the pipeline in use
supports `--publish` and that nothing blocks a local connection to `127.0.0.1`.

### The AI assistant does not respond

Check that:

- the Python LLM environment is configured correctly
- `llm.main` can be imported
- the Gemini credentials/configuration used by the existing LLM backend are available
- the query is between 1 and 2000 characters
- the console did not print a manual-ingestion failure at startup (the assistant then answers without lab-manual context)

### The Selected Droplet panel shows only dashes

The panel prints what the CV API gives it, so dashes mean the CV API has nothing
to give yet. Check, in order:

- a live session is actually running
- the pipeline is past its background-learning phase
- a droplet is picked -- click one in the feed
- for Fall time, the droplet's fall clock is running; it is `null` between falls

### The status stays yellow ("Not enough data")

The verdict needs both a balance voltage (seen while the field is `work`) and a
free-fall speed (seen while it is `off`) for the *same* picked droplet, and picking
a different droplet starts over. Hold the droplet balanced, then cut to 0 V.
The ring's tooltip says which reading is still missing.

### The AI response times out

The frontend expects:

- the first response within 60 seconds
- subsequent chunks at intervals of no more than 5 seconds

A slow or stalled backend stream will therefore be terminated from the frontend's point of view.

---

## Summary

The JavaScript version separates the application into three layers:

```text
┌──────────────────────────────────────┐
│          HTML / CSS / JavaScript     │
│              Browser UI              │
└─────────────────┬────────────────────┘
                  │ HTTP / SSE / MJPEG
                  ▼
┌──────────────────────────────────────┐
│             web_server.py             │
│             Python bridge             │
└───────────────┬──────────────────────┘
                │
        ┌───────┴────────┐
        ▼                ▼
┌───────────────┐  ┌───────────────┐
│ Existing CV   │  │ Existing LLM  │
│   backend     │  │    backend    │
└───────────────┘  └───────────────┘
```

The frontend can therefore be developed independently while the existing CV and LLM implementations remain unchanged.