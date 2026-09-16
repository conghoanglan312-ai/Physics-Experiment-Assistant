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
- `app_test/stream_server.py`

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

Place the JavaScript version in the `app_js/` directory inside your existing project.

For example:

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
└── llm/
```

Make sure the `cv/` and `llm/` packages are accessible from the same Python environment used to start the web server.

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

Clicking the live feed is forwarded to the pipeline's own window as a real click:

```text
Browser click on <img>
   │
   │ POST /api/live/click  { x, y, token }
   ▼
web_server.py
   │
   │ wrapper.click_live(handle, x, y)
   ▼
PostMessage(hwnd, WM_LBUTTONDOWN / WM_LBUTTONUP, ...)
   │
   ▼
pipeline.py's own cv2.setMouseCallback("Droplets", onMouse)
```

`pipeline.py` itself is never modified or aware that the click didn't come
from a physical mouse: it already reads click coordinates out of the
window's ordinary Win32 mouse messages, so posting the same message it would
normally receive is enough to select a droplet, same as clicking the native
window directly. The frontend maps the click through the `<img>`'s
`object-fit: contain` letterboxing so it lands on the correct pixel even
when the displayed feed is scaled or padded to fit the panel.

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

Example response:

```json
{
  "running": true,
  "device": 1
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

`app_test/stream_server.py` serves the newest JPEG frame over MJPEG.

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
- `wrapper.py` can import `app_test.stream_server`

### The AI assistant does not respond

Check that:

- the Python LLM environment is configured correctly
- `llm.main` can be imported
- the Gemini credentials/configuration used by the existing LLM backend are available
- the query is between 1 and 2000 characters

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
