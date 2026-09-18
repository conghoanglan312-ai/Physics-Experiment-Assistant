const MAX_CHARS = 2000;
const SESSION_ID = "millikan_test_session";

const aiFab = document.getElementById("aiFab");
const aiPanel = document.getElementById("aiPanel");
const aiClose = document.getElementById("aiClose");
const aiPromptBubble = document.getElementById("aiPromptBubble");
const aiInput = document.getElementById("aiInput");
const sendButton = document.getElementById("sendButton");
const charCounter = document.getElementById("charCounter");
const chatHistory = document.getElementById("chatHistory");

const liveButton = document.getElementById("liveButton");
const liveSourceInput = document.getElementById("liveSourceInput");
const demoClipButton = document.getElementById("demoClipButton");
const liveFeed = document.getElementById("liveFeed");
const fallbackVideo = document.getElementById("fallbackVideo");
const videoPlaceholder = document.getElementById("videoPlaceholder");
const videoContainer = document.getElementById("videoContainer");
const feedHint = document.getElementById("feedHint");
const feedStatus = document.getElementById("feedStatus");

const dropletLabel = document.getElementById("dropletLabel");
const dropletRing = document.getElementById("dropletRing");
const dropletStatus = document.getElementById("dropletStatus");
const dropletVoltage = document.getElementById("dropletVoltage");
const dropletFallTime = document.getElementById("dropletFallTime");
const dropletVelocity = document.getElementById("dropletVelocity");
const dropletField = document.getElementById("dropletField");
const dropletNote = document.getElementById("dropletNote");

const state = {
  messages: [],
  generating: false,
  live: null,
  livePoll: null,
  // The last voltage reading seen while fieldState was "work". Kept so the
  // panel can hold that number steady while the field is off or lifting,
  // instead of re-printing whatever the (irrelevant) reading happens to be.
  lastFieldOnVoltage: null,
  // The last fall-time reading seen while fieldState was "off" (i.e. while
  // the droplet was actually falling). Held steady the same way once the
  // field goes back to "work" or "lift", since the droplet isn't falling then.
  lastFallSeconds: null,
};

function pythonLen(text) {
  // Python len(str) counts Unicode code points. Array.from() matches that
  // behavior much more closely than JavaScript's UTF-16 string.length.
  return Array.from(text || "").length;
}

function canSubmit(text) {
  return !state.generating && pythonLen(text) >= 1 && pythonLen(text) <= MAX_CHARS;
}

function updateInputState() {
  const count = pythonLen(aiInput.value);
  charCounter.textContent = `${count} / ${MAX_CHARS}`;
  charCounter.classList.toggle("over-limit", count >= MAX_CHARS);
  sendButton.disabled = !canSubmit(aiInput.value);
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function renderBasicMarkdown(text) {
  const codeSpans = [];
  let escaped = escapeHtml(text);

  escaped = escaped.replace(/`([^`\n]+?)`/g, (_, value) => {
    const id = codeSpans.push(value) - 1;
    return `\u0000CODE${id}\u0000`;
  });

  escaped = escaped.replace(/\*\*([^\n]+?)\*\*/g, "<strong>$1</strong>");
  escaped = escaped.replace(/(?<!\*)\*([^\n*]+?)\*(?!\*)/g, "<em>$1</em>");

  escaped = escaped.replace(/\u0000CODE(\d+)\u0000/g, (_, index) => `<code>${codeSpans[Number(index)]}</code>`);

  const lines = escaped.split("\n");
  const parts = [];
  let i = 0;

  while (i < lines.length) {
    const stripped = lines[i].trim();
    let m = stripped.match(/^(#{1,6})\s+(.*)$/);
    if (m) {
      const level = m[1].length;
      parts.push(`<h${level}>${m[2]}</h${level}>`);
      i++;
      continue;
    }

    if (/^[-*]\s+/.test(stripped)) {
      const items = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i].trim())) {
        items.push(`<li>${lines[i].trim().replace(/^[-*]\s+/, "")}</li>`);
        i++;
      }
      parts.push(`<ul>${items.join("")}</ul>`);
      continue;
    }

    if (/^\d+\.\s+/.test(stripped)) {
      const items = [];
      while (i < lines.length && /^\d+\.\s+/.test(lines[i].trim())) {
        items.push(`<li>${lines[i].trim().replace(/^\d+\.\s+/, "")}</li>`);
        i++;
      }
      parts.push(`<ol>${items.join("")}</ol>`);
      continue;
    }

    parts.push(stripped === "" ? "" : stripped);
    i++;
  }

  return parts.join("<br>").replaceAll("<br><ul>", "<ul>").replaceAll("</ul><br>", "</ul>").replaceAll("<br><ol>", "<ol>").replaceAll("</ol><br>", "</ol>");
}

function renderMathAndMarkdown(text) {
  // Keep the same $...$ / $$...$$ boundaries as ai_assist.py. MathJax is used
  // for browser-side rendering; malformed expressions simply remain visible.
  const mathSlots = [];
  const withSlots = String(text).replace(/\$\$(.+?)\$\$|\$([^$\n]+?)\$/gs, (match, block, inline) => {
    const src = (block ?? inline ?? "").trim();
    const id = mathSlots.push({ src, block: Boolean(block) }) - 1;
    return `\u0000MATH${id}\u0000`;
  });

  let html = renderBasicMarkdown(withSlots);

  html = html.replace(/\u0000MATH(\d+)\u0000/g, (_, index) => {
    const math = mathSlots[Number(index)];
    const escaped = escapeHtml(math.src);
    return math.block ? `\\[${escaped}\\]` : `\\(${escaped}\\)`;
  });

  return html;
}

function wrapLatexForHorizontalScroll(root) {
  if (!root) return;

  // Only the outer <mjx-container> is the actual visible rendering box.
  // MathJax also nests a hidden <math> (accessibility/assistive MML) inside
  // it; selecting both used to wrap the same equation twice and corrupt its
  // box model, which is what made even short equations report as
  // "overflowing".
  const mathNodes = root.querySelectorAll('mjx-container');
  mathNodes.forEach(node => {
    if (node.parentElement?.classList.contains('latex-scroll')) return;

    // Inline LaTeX must remain inline. Wrapping every equation in a
    // block-level <div> would force things such as "equilibrium voltage
    // $U_n$" onto two lines. Only display equations should behave like
    // blocks -- unless an inline one turns out to be too wide to fit at
    // all, in which case latex-overflow (below) promotes it to a block so
    // there's an actual scrollable box to reveal the rest of it.
    const isDisplay = node.getAttribute('display') === 'true';

    const wrapper = document.createElement(isDisplay ? 'div' : 'span');
    wrapper.className = `latex-scroll${isDisplay ? ' latex-display' : ' latex-inline'}`;
    node.parentNode.insertBefore(wrapper, node);
    wrapper.appendChild(node);

    // Do not create a horizontal scrollbar for short LaTeX such as q, t_1,
    // U_n, etc. Only enable scrolling when the rendered equation actually
    // exceeds the available width of its container.
    //
    // Reading scrollWidth/clientWidth straight off the live node is not
    // reliable here: an inline-block wrapper's shrink-to-fit width
    // calculation can end up adopting an unbreakable child's forced
    // intrinsic width too, so the wrapper and the child end up reporting
    // the same size and overflow never gets detected even though the
    // equation is genuinely wider than the chat bubble. Instead, measure a
    // disposable clone that has no width constraints at all (inserted right
    // next to the original so it inherits the exact same font/scale), and
    // compare that ground-truth size against the space actually available.
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const probe = node.cloneNode(true);
        probe.style.cssText = "position:absolute; visibility:hidden; pointer-events:none; left:0; top:0; width:auto; min-width:0; max-width:none; white-space:nowrap;";
        wrapper.appendChild(probe);
        const naturalWidth = probe.scrollWidth;
        wrapper.removeChild(probe);

        // Measure the available space against the message bubble itself
        // rather than the wrapper: the bubble is reliably pinned to
        // max-width:75% of the chat panel (with min-width:0 overriding the
        // usual flex-item content-protection), so it's a stable reference
        // even if the wrapper's own width is momentarily uncertain because
        // of the very shrink-to-fit ambiguity this check exists to work
        // around.
        const bubble = wrapper.closest(".message-bubble");
        const bubbleStyle = bubble ? getComputedStyle(bubble) : null;
        const bubblePadding = bubbleStyle
          ? parseFloat(bubbleStyle.paddingLeft || "0") + parseFloat(bubbleStyle.paddingRight || "0")
          : 0;
        const available = bubble ? bubble.clientWidth - bubblePadding : wrapper.clientWidth;

        wrapper.classList.toggle('latex-overflow', naturalWidth > available + 1);
      });
    });
  });
}

async function typesetChat(root) {
  if (!root || !window.MathJax?.typesetPromise) return;
  // During streaming this is called once per chunk without being awaited by
  // the caller, and each chunk replaces the bubble's innerHTML from scratch.
  // A slower, older call can otherwise finish after a newer one and run the
  // overflow measurement against a bubble state it no longer matches. Tag
  // each call with a token and bail out if a newer call has started since.
  const token = (root._typesetToken = (root._typesetToken || 0) + 1);
  try {
    await window.MathJax.typesetPromise([root]);
  } catch (_) {
    return;
  }
  if (root._typesetToken !== token) return;
  wrapLatexForHorizontalScroll(root);
}

function scrollChat() {
  chatHistory.scrollTop = chatHistory.scrollHeight;
}

function createMessageElement(role, content, animate = true) {
  const wrapper = document.createElement("div");
  wrapper.className = `chat-message ${role}`;
  if (!animate) wrapper.style.animation = "none";

  if (role === "assistant") {
    const row = document.createElement("div");
    row.className = "assistant-row";

    const icon = document.createElement("div");
    icon.className = "ai-icon";
    icon.textContent = "✦";

    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.innerHTML = renderMathAndMarkdown(content);

    row.append(icon, bubble);
    wrapper.appendChild(row);
  } else {
    const bubble = document.createElement("div");
    bubble.className = "message-bubble";
    bubble.innerHTML = renderMathAndMarkdown(content);
    wrapper.appendChild(bubble);
  }

  return wrapper;
}

function renderHistory() {
  chatHistory.replaceChildren();
  for (const message of state.messages) {
    chatHistory.appendChild(createMessageElement(message.role, message.content, false));
  }
  scrollChat();
  typesetChat(chatHistory);
}

function addMessage(role, content, animate = true) {
  state.messages.push({ role, content });
  chatHistory.appendChild(createMessageElement(role, content, animate));
  scrollChat();
  typesetChat(chatHistory);
}

const THINKING_MESSAGES = [
  "Thinking…",
  "Reviewing droplet fall data…",
  "Checking the balance voltage range…",
  "Cross-referencing reference constants…",
  "Working through the charge calculation…",
  "Consulting the experiment guide…",
  "Weighing up Brownian motion effects…",
];

function createThinking() {
  const wrapper = document.createElement("div");
  wrapper.className = "chat-message assistant";
  const row = document.createElement("div");
  row.className = "assistant-row";
  const icon = document.createElement("div");
  icon.className = "ai-icon";
  icon.textContent = "✦";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";

  const status = document.createElement("div");
  status.className = "thinking-status";

  const dots = document.createElement("div");
  dots.className = "thinking-dots";
  for (let i = 0; i < 3; i++) {
    const dot = document.createElement("div");
    dot.className = "thinking-dot";
    dots.appendChild(dot);
  }

  const label = document.createElement("span");
  label.className = "thinking-label";
  label.textContent = THINKING_MESSAGES[0];

  const timer = document.createElement("span");
  timer.className = "thinking-timer";
  timer.textContent = "0s";

  status.append(dots, label, timer);
  bubble.appendChild(status);
  row.append(icon, bubble);
  wrapper.appendChild(row);
  return wrapper;
}

// Keeps the thinking bubble feeling alive for the up-to-60s wait: an
// elapsed-time counter so it never looks frozen, plus a slowly rotating
// status line so it doesn't look stuck on one thought. Returns a cleanup
// function; call it the moment the bubble's content is replaced (first real
// chunk, error, or timeout) so the intervals don't keep ticking against
// detached DOM nodes.
function startThinkingAnimation(thinking) {
  const label = thinking.querySelector(".thinking-label");
  const timer = thinking.querySelector(".thinking-timer");
  const start = Date.now();
  let msgIndex = 0;

  const timerId = setInterval(() => {
    if (!timer.isConnected) return;
    timer.textContent = `${Math.floor((Date.now() - start) / 1000)}s`;
  }, 1000);

  const rotateId = setInterval(() => {
    if (!label.isConnected) return;
    msgIndex = (msgIndex + 1) % THINKING_MESSAGES.length;
    label.textContent = THINKING_MESSAGES[msgIndex];
  }, 7000);

  return () => {
    clearInterval(timerId);
    clearInterval(rotateId);
  };
}

function openAi() {
  aiPanel.classList.add("open");
  aiPanel.setAttribute("aria-hidden", "false");
  aiFab.setAttribute("aria-expanded", "true");
  aiPromptBubble.style.display = "none";
  scrollChat();
  aiInput.focus();
}

function closeAi() {
  aiPanel.classList.remove("open");
  aiPanel.setAttribute("aria-hidden", "true");
  aiFab.setAttribute("aria-expanded", "false");
}

async function sendQuery() {
  const raw = aiInput.value;
  const query = raw.trim();

  if (!query || !canSubmit(raw)) {
    updateInputState();
    return;
  }

  state.generating = true;
  updateInputState();
  sendButton.classList.add("locked");
  sendButton.textContent = "Generating…";

  // Match the original immediate client-side clear behavior.
  aiInput.value = "";
  updateInputState();

  addMessage("user", query, true);
  const thinking = createThinking();
  chatHistory.appendChild(thinking);
  scrollChat();
  let stopThinking = startThinkingAnimation(thinking);

  let assistantText = "";
  let streamFinished = false;

  // The thinking row is also the final assistant row. Its bubble is
  // replaced in-place when the first text chunk arrives, so there is never
  // a second blank assistant bubble.
  const assistantBubble = thinking.querySelector(".message-bubble");

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
      body: JSON.stringify({ userQuery: query, sessionID: SESSION_ID }),
    });

    if (!response.ok || !response.body) {
      let message = "Unable to get a response from the AI assistant.";
      try {
        const errorPayload = await response.json();
        if (errorPayload?.error) message = errorPayload.error;
      } catch (_) {}
      throw new Error(message);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (!streamFinished) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const events = buffer.split("\n\n");
      buffer = events.pop() ?? "";

      for (const event of events) {
        const dataLines = event
          .split("\n")
          .filter(line => line.startsWith("data:"))
          .map(line => line.slice(5).trimStart());
        if (!dataLines.length) continue;

        let payload;
        try { payload = JSON.parse(dataLines.join("\n")); }
        catch { continue; }

        if (payload.type === "text") {
          if (stopThinking) { stopThinking(); stopThinking = null; }
          assistantText += payload.data ?? "";
          assistantBubble.innerHTML = renderMathAndMarkdown(assistantText);
          scrollChat();
          typesetChat(assistantBubble);
        } else if (payload.type === "error") {
          if (stopThinking) { stopThinking(); stopThinking = null; }
          assistantBubble.classList.add("error-bubble");
          assistantBubble.textContent = "Unable to get a response from the AI assistant.";
          streamFinished = true;
          break;
        } else if (payload.type === "timeout") {
          if (stopThinking) { stopThinking(); stopThinking = null; }
          assistantBubble.classList.add("error-bubble");
          assistantBubble.textContent = payload.data?.includes("60 seconds")
            ? "The AI assistant took too long to respond. Please try again."
            : "The AI assistant stopped responding. Please try again.";
          streamFinished = true;
          break;
        } else if (payload.type === "done") {
          streamFinished = true;
          break;
        }
      }
    }
  } catch (error) {
    if (stopThinking) { stopThinking(); stopThinking = null; }
    assistantBubble.classList.add("error-bubble");
    assistantBubble.textContent = error?.message || "Unable to get a response from the AI assistant.";
  } finally {
    if (stopThinking) stopThinking(); // safety net -- should already be null by now
    if (assistantText) state.messages.push({ role: "assistant", content: assistantText });
    state.generating = false;
    sendButton.classList.remove("locked");
    sendButton.textContent = "Send";
    updateInputState();
    scrollChat();
  }
}

// A bundled fixture that ships with the pipeline's own test suite -- lets
// the feed be tried with no camera or dongle attached. Adjust this if your
// checkout keeps its fixtures somewhere else.
const DEMO_CLIP_SOURCE = "app_js/fixtures/demo_clip.mp4";

async function startLive(explicitSource) {
  const source = (explicitSource ?? liveSourceInput.value).trim();

  liveButton.disabled = true;
  liveSourceInput.disabled = true;
  demoClipButton.disabled = true;
  feedStatus.className = "feed-status";
  feedStatus.textContent = source
    ? `Starting playback for "${source}"...`
    : "Running bench check and starting capture...";
  feedStatus.classList.remove("hidden", "error", "live");

  try {
    const response = await fetch("/api/live/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(source ? { source } : {}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to start live feed.");

    state.live = payload;
    showLiveFeed(payload.url, payload.device, payload.source);
    liveButton.textContent = "■ Stop live feed";
    liveButton.disabled = false;
    scheduleLivePoll(0);
  } catch (error) {
    state.live = null;
    showFallback();
    feedStatus.textContent = error?.message || "Please plug in the dongle first.";
    feedStatus.className = "feed-status error";
    liveButton.textContent = "▶ Start live feed";
    liveButton.disabled = false;
    liveSourceInput.disabled = false;
    demoClipButton.disabled = false;
  }
}

function showLiveFeed(url, device, source) {
  fallbackVideo.pause();
  fallbackVideo.classList.add("hidden");
  videoPlaceholder.classList.add("hidden");
  liveFeed.src = `${url}?t=${Date.now()}`;
  liveFeed.classList.remove("hidden");
  feedStatus.textContent = (typeof source === "string" && source && !/^-?\d+$/.test(source))
    ? `● LIVE — playing ${source}`
    : `● LIVE — device ${device}`;
  feedStatus.className = "feed-status live";
  feedHint.classList.remove("hidden");
  liveSourceInput.disabled = true;
  demoClipButton.disabled = true;
}

function showFallback() {
  liveFeed.removeAttribute("src");
  liveFeed.classList.add("hidden");
  if (fallbackVideo.readyState > 0) {
    fallbackVideo.classList.remove("hidden");
    videoPlaceholder.classList.add("hidden");
  } else {
    fallbackVideo.classList.add("hidden");
    videoPlaceholder.classList.remove("hidden");
  }
  feedStatus.classList.add("hidden");
  feedHint.classList.add("hidden");
  liveSourceInput.disabled = false;
  demoClipButton.disabled = false;
}

demoClipButton.addEventListener("click", () => {
  if (state.live) return;
  liveSourceInput.value = DEMO_CLIP_SOURCE;
  startLive(DEMO_CLIP_SOURCE);
});

function showClickRipple(containerX, containerY) {
  const ripple = document.createElement("div");
  ripple.className = "click-ripple";
  ripple.style.left = `${containerX}px`;
  ripple.style.top = `${containerY}px`;
  videoContainer.appendChild(ripple);
  ripple.addEventListener("animationend", () => ripple.remove());
}

async function sendLiveClick(x, y) {
  if (!state.live) return;
  try {
    await fetch("/api/live/click", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y, token: state.live.token }),
    });
    // The pick changes on the pipeline's next pass. Look sooner than the
    // ordinary poll would so the panel keeps up with the click.
    scheduleLivePoll(300);
  } catch (_) {
    // Best-effort -- a dropped click just means the operator clicks again.
  }
}

liveFeed.addEventListener("click", (event) => {
  if (!state.live || !liveFeed.naturalWidth || !liveFeed.naturalHeight) return;

  const rect = liveFeed.getBoundingClientRect();
  const natW = liveFeed.naturalWidth;
  const natH = liveFeed.naturalHeight;

  // object-fit:contain shows the whole frame letterboxed inside the <img>
  // box rather than cropping it, which is what lets clicks map onto pixels
  // that are actually still on screen. Work out where the real image sits
  // within that box before doing anything else with the click.
  const scale = Math.min(rect.width / natW, rect.height / natH);
  const dispW = natW * scale;
  const dispH = natH * scale;
  const offsetX = (rect.width - dispW) / 2;
  const offsetY = (rect.height - dispH) / 2;

  const localX = event.clientX - rect.left - offsetX;
  const localY = event.clientY - rect.top - offsetY;
  if (localX < 0 || localY < 0 || localX > dispW || localY > dispH) return; // clicked the letterbox margin, not the frame

  showClickRipple(event.clientX - videoContainer.getBoundingClientRect().left,
                   event.clientY - videoContainer.getBoundingClientRect().top);

  // A FRACTION of the frame, not a pixel of it. cv/pipeline.py's
  // readRemoteClicks() takes fractions precisely so the sender can scale the
  // picture to whatever size its panel happens to be; sending pixels meant
  // every click was clamped to 1.0 and landed in the bottom-right corner.
  sendLiveClick(localX / dispW, localY / dispH);
});

async function stopLive() {
  if (!state.live) return;
  liveButton.disabled = true;
  try {
    await fetch("/api/live/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: state.live.token }),
    });
  } catch (error) {
    feedStatus.textContent = `Failed to stop live feed: ${error.message}`;
    feedStatus.className = "feed-status error";
  } finally {
    state.live = null;
    state.lastFieldOnVoltage = null;
    state.lastFallSeconds = null;
    showFallback();
    liveButton.textContent = "▶ Start live feed";
    liveButton.disabled = false;
    scheduleLivePoll(0);   // clears the panel back to placeholders
  }
}

// ---------------------------------------------------------------------------
//  The Selected Droplet panel.
//
//  Pipeline click -> cv/pipeline.py's `picked` -> cv/api.live_status() ->
//  /api/live/status -> here. Nothing on this side works anything out: it prints
//  the numbers the CV API sent and the verdict the server reached from them. A
//  value the API did not provide is shown as a dash rather than guessed at, and
//  the physics stays in Python.
// ---------------------------------------------------------------------------
const MISSING = "—";

const SUITABILITY = {
  yes: { text: "Suitable", ring: "suitable" },
  no: { text: "Not suitable", ring: "unsuitable" },
  unknown: { text: "Not enough data", ring: "unknown" },
};

function showMeasurement(element, value, unit, digits) {
  const missing = value === null || value === undefined || Number.isNaN(Number(value));
  element.textContent = missing ? MISSING : `${Number(value).toFixed(digits)} ${unit}`;
}

function voltageToDisplay(data) {
  // Only a genuine "work" reading is allowed to move the number. Anything
  // else -- off, lift, or an unrecognized/missing fieldState -- just keeps
  // showing whatever was last held. This is deliberately not an if/else over
  // every possible fieldState value: the set of values this pipeline can
  // send isn't something app.js should have to keep in exact sync with, so
  // "not a fresh work reading" is treated as "hold", full stop.
  if (data.fieldState === "work" && data.voltageV !== null && data.voltageV !== undefined) {
    state.lastFieldOnVoltage = data.voltageV;
  }
  return state.lastFieldOnVoltage;
}

function fallSecondsToDisplay(data) {
  // Same rule as voltageToDisplay, mirrored: only a genuine "off" reading
  // (the droplet is actually falling) is allowed to move the number.
  if (data.fieldState === "off" && data.fallSeconds !== null && data.fallSeconds !== undefined) {
    state.lastFallSeconds = data.fallSeconds;
  }
  return state.lastFallSeconds;
}

function renderSelectedDroplet(selected) {
  const data = selected || {};
  const verdict = SUITABILITY[data.suitable] || SUITABILITY.unknown;

  dropletLabel.textContent = data.label || MISSING;
  dropletStatus.textContent = verdict.text;
  dropletRing.className = `droplet-ring ${verdict.ring}`;
  dropletRing.title = data.note || verdict.text;

  showMeasurement(dropletVoltage, voltageToDisplay(data), "V", 0);
  showMeasurement(dropletFallTime, fallSecondsToDisplay(data), "s", 1);
  showMeasurement(dropletVelocity, data.speedGridPerSec, "grid/s", 2);
  dropletField.textContent = data.fieldState || MISSING;

  dropletNote.textContent = data.note || "";
}

// The pipeline rewrites its status about once a second, so polling faster than
// that only re-reads the same file. When nothing is running there is nothing to
// keep up with, so ease off rather than stopping: the page then picks up a
// session started from elsewhere without anyone reloading it.
const LIVE_POLL_MS = 1000;
const IDLE_POLL_MS = 5000;

function scheduleLivePoll(delay) {
  if (state.livePoll) clearTimeout(state.livePoll);
  state.livePoll = setTimeout(pollLiveState, delay ?? LIVE_POLL_MS);
}

async function pollLiveState() {
  let payload;
  try {
    const response = await fetch("/api/live/status");
    payload = await response.json();
  } catch (_) {
    scheduleLivePoll(1500);
    return;
  }

  if (state.live && !payload.running) {
    state.live = null;
    state.lastFieldOnVoltage = null;
    state.lastFallSeconds = null;
    showFallback();
    feedStatus.textContent = "The live session ended.";
    feedStatus.className = "feed-status error";
    liveButton.textContent = "▶ Start live feed";
    liveButton.disabled = false;
  }

  renderSelectedDroplet(payload.selected);
  scheduleLivePoll(payload.running ? LIVE_POLL_MS : IDLE_POLL_MS);
}

const THEME_KEY = "millikanTheme";
const themeToggle = document.getElementById("themeToggle");

function forceRepaint() {
  // Some browsers don't recomposite backdrop-filter panels (the glass guide
  // cards especially) when only a CSS custom property changes -- here,
  // --panel-bg/--guide-content-bg/etc. flipping with the theme. They stay
  // visually stale until something else (a hover, a resize) forces a
  // repaint. A one-frame opacity nudge forces one immediately instead.
  const el = document.body;
  el.style.opacity = "0.999";
  requestAnimationFrame(() => { el.style.opacity = ""; });
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  if (themeToggle) {
    const isLight = theme === "light";
    themeToggle.textContent = isLight ? "☀️" : "🌙";
    themeToggle.setAttribute("aria-label", isLight ? "Switch to dark mode" : "Switch to light mode");
  }
  forceRepaint();
}

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

if (themeToggle) {
  applyTheme(currentTheme());
  themeToggle.addEventListener("click", () => {
    const next = currentTheme() === "light" ? "dark" : "light";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
  });
}

aiFab.addEventListener("click", () => {
  if (aiPanel.classList.contains("open")) closeAi(); else openAi();
});
aiClose.addEventListener("click", closeAi);

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && aiPanel.classList.contains("open")) closeAi();
});

aiInput.addEventListener("input", updateInputState);
aiInput.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    event.stopPropagation();
    if (!state.generating && canSubmit(aiInput.value)) sendQuery();
  }
});
sendButton.addEventListener("click", sendQuery);

liveButton.addEventListener("click", () => state.live ? stopLive() : startLive());

fallbackVideo.addEventListener("error", () => {
  fallbackVideo.classList.add("hidden");
  videoPlaceholder.classList.remove("hidden");
});

updateInputState();
showFallback();
renderSelectedDroplet(null);
pollLiveState();
