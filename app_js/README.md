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
│   └── stream_server.py
├── cv/
│   └── ...
└── llm/
    └── ...
```

### Files inside `app_js/`

| File | Purpose |
|---|---|
| `index.html` | Main page structure and AI assistant panel |
| `styles.css` | Complete visual styling, layout, animation, responsive behavior |
| `app.js` | Frontend state, live feed controls, chat interaction, streaming response rendering |
| `web_server.py` | Python HTTP bridge between the browser and the existing Python CV/LLM backend |
| `wrapper.py` | Starts/stops the live CV pipeline and provides the live stream |
| `stream_server.py` | Serves the MJPEG camera stream |

### Existing backend files

The frontend does **not** replace the existing CV or LLM implementation.

- `app_js/wrapper.py` starts/stops the live CV pipeline.
- `app_js/stream_server.py` serves the MJPEG camera stream.
- `llm.main.getResponseFromGemini()` provides the streamed AI response.

---

## Requirements

### Python

Python must be installed and available from the command line.

The existing project dependencies are also required, including the packages used by:

- the CV pipeline
- `wrapper.py`
- the LLM module
- `stream_server.py`

For the live camera pipeline, the existing Windows-specific dependencies such as `pywin32` and `Pillow` are required by the current backend.

### Browser

Use a modern browser such as:

- Google Chrome
- Microsoft Edge
- Firefox

JavaScript must be enabled.

The frontend loads MathJax from the jsDelivr CDN for LaTeX rendering.

---

## Setup

`app_js/` is self-contained: `wrapper.py` and `stream_server.py` live in it
alongside the frontend, so there is nothing to move or copy.

The one requirement is that `cv/` and `llm/` are importable from the same Python
environment that starts the web server.

---

## Running the Application

Open a terminal in the project directory and run:

```bash
python app_js/web_server.py
```

The server runs at:

```text
http://127.0.0.1:8502
```

Open that address in your browser.

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
Existing CV backend
   │
   ▼
Native pipeline window
   │
   ▼
Frame capture / MJPEG stream
   │
   ▼
Browser <img>
```

The browser displays the streamed image, and clicking on it selects a droplet.

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

`pipeline.py` is not modified and is not aware the click came from a browser:
it already accepts remote clicks on stdin for exactly this purpose, and treats
one identically to a click on its own window.

Fractions, not pixels: the sender is scaling the picture to whatever size its
panel happens to be, and a fraction survives that. The frontend maps the click
through the `<img>`'s `object-fit: contain` letterboxing first, so it lands on
the right part of the frame even when the feed is scaled or padded to fit.

`wrapper.click_live()` does the same job by posting Win32 mouse messages to the
native window. It is still there, but the browser does not use it -- the stdin
route needs no window to have focus and is the one `pipeline.py` documents.

### Starting the feed

Click:

```text
▶ Start live feed
```

with the source field left blank. The backend first performs its normal camera/dongle check. If the required device is unavailable, the frontend shows a user-facing message instead of exposing backend details.

When successful, the button changes to:

```text
■ Stop live feed
```

### Developing without the apparatus

The source field next to the button accepts:

- nothing (default) — unchanged behavior, runs the bench check against a real camera/dongle.
- a number — used directly as a camera index, skipping the bench check.
- anything else — a clip spec (a file path, or `frames:DIR`) played back through the pipeline's own `Droplets` window at real speed, with click-to-pick still working. No apparatus or dongle required.

The **🎬 Try demo clip** button fills the field with a bundled test fixture and starts it immediately, for a one-click way to exercise the whole stack (server, stream, click-to-pick) from a desk.

### Stopping the feed

Click:

```text
■ Stop live feed
```

The frontend calls:

```text
POST /api/live/stop
```

and the backend tears down the current live session.

---

## Selected Droplet Panel

The panel on the right shows the droplet the operator currently has picked in
the pipeline. It is filled entirely from the CV API; the browser does not
compute any experiment value.

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
GET /api/live/status  ->  "selected"
   │
   ▼
Selected Droplet panel
```

The page polls `/api/live/status` once a second while a session is running (the
rate the pipeline rewrites its status at) and every five seconds when one is
not, so the panel follows the pipeline without anyone reloading anything.

### Fields

| Panel field | CV API source |
|---|---|
| Voltage | `voltageV` |
| Fall time | `picked.fallSeconds` |
| Velocity | `picked.speedGridPerSec` |
| Status | derived from the two below |

A value the CV API did not provide is shown as:

```text
—
```

It is never filled in with a guess, a default, or a value worked out in
JavaScript.

### Status

The lab manual asks two questions about a candidate droplet:

```text
falling speed at 0 V     0.2 - 0.5 grid/s
balance voltage          150 - 250 V
```

They are answered at different moments -- Ⅳ.3 balances the droplet with the
field on, Ⅳ.2 times its fall with the field off -- so each answer is remembered
for as long as the same droplet stays picked, and forgotten the moment the pick
changes.

| Ring | Meaning |
|---|---|
| Green | Both conditions were observed and both hold |
| Red | One of them was observed and does not hold |
| Yellow | Not enough has been observed yet to say |

Yellow is the honest answer, not a soft failure. A droplet drifting well off
balance at 180 V has not shown its balance voltage, so 180 V is not judged
against the band; a droplet that has never been cut to 0 V has not shown its
falling speed. The panel says which observation is missing.

---

## AI Assistant

The AI assistant is opened using the floating:

```text
🛸
```

button.

The panel contains:

- chat history
- user input area
- character counter
- Send button
- streaming assistant response
- thinking animation

The interface is designed to behave like the previous Streamlit implementation while removing the Streamlit dependency from the frontend.

---

## AI Input Rules

The maximum query length is:

```text
2000 characters
```

The character counter is updated live.

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
- an AI response is currently being generated

---

## Keyboard Behavior

### Enter

Pressing:

```text
Enter
```

sends the current query.

### Shift + Enter

Pressing:

```text
Shift + Enter
```

creates a new line without sending the message.

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

The frontend displays a user-friendly timeout message instead of exposing Python exceptions or backend implementation details.

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

AI messages support LaTeX using MathJax.

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

`web_server.py` exposes the browser-facing endpoints below.

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
    "label": "Droplet 1",
    "voltageV": 212,
    "fallSeconds": null,
    "speedGridPerSec": 0.313,
    "suitable": "unknown",
    "note": "Balanced at 212 V. Cut to 0 V to measure its falling speed."
  }
}
```

`suitable` is `"yes"`, `"no"` or `"unknown"`. Any measurement the CV API did not
provide is `null`, which the page renders as a dash.

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

On success, the response contains the MJPEG stream URL and device information.

### Stop live feed

```http
POST /api/live/stop
```

The current live-session token is required.

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

The response is streamed using Server-Sent Events (`text/event-stream`).

---

## Error Handling

The frontend intentionally avoids exposing implementation details such as:

- Python tracebacks
- subprocess commands
- internal module names
- backend object state
- raw exception messages

The user should receive simple messages describing what they need to do next.

For example, when the camera dongle is unavailable:

```text
Please plug in the dongle first.
```

---

## Design

The interface uses:

- glassmorphism panels
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

### Live stream architecture

`stream_server.py` serves the newest JPEG frame over MJPEG.

The stream is intentionally designed so slow browser readers do not create an ever-growing frame backlog. The browser displays the latest available frame rather than trying to process every frame.

---

## Troubleshooting

### The page does not load

Check that the web server is running:

```bash
python app_js/web_server.py
```

Then open:

```text
http://127.0.0.1:8502
```

### The live feed does not start

Make sure:

- the camera/dongle is connected
- the required Windows dependencies are installed
- the existing CV pipeline works independently
- `wrapper.py` can import `app_js.stream_server`

### The AI assistant does not respond

Check that:

- the Python LLM environment is configured correctly
- `llm.main` can be imported
- the Gemini credentials/configuration used by the existing LLM backend are available
- the query is between 1 and 2000 characters

### The Selected Droplet panel shows only dashes

The panel prints what the CV API gives it, so dashes mean the CV API has nothing
to give yet. Check, in order:

- a live session is actually running
- the pipeline is past its background-learning phase
- a droplet is picked -- click one in the feed
- for Fall time, the droplet's fall clock is running; it is `null` between falls

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
