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
const feedLoading = document.getElementById("feedLoading");
const feedLoadingBody = document.getElementById("feedLoadingBody");
const videoContainer = document.getElementById("videoContainer");
const feedHint = document.getElementById("feedHint");
const feedStatus = document.getElementById("feedStatus");
const feedTitle = document.getElementById("feedTitle");
const emptyStateStartButton = document.getElementById("emptyStateStartButton");
const sessionTimer = document.getElementById("sessionTimer");
const sessionTimerText = document.getElementById("sessionTimerText");

const notificationList = document.getElementById("notificationList");
const notificationPlaceholder = document.getElementById("notificationPlaceholder");

const dropletLabel = document.getElementById("dropletLabel");
const dropletRing = document.getElementById("dropletRing");
const dropletStatus = document.getElementById("dropletStatus");
const dropletVoltage = document.getElementById("dropletVoltage");
const dropletFallTime = document.getElementById("dropletFallTime");
const dropletVelocity = document.getElementById("dropletVelocity");
const dropletField = document.getElementById("dropletField");
const guidanceLine = document.getElementById("guidanceLine");
const whyButton = document.getElementById("whyButton");
const whyAnswer = document.getElementById("whyAnswer");
const whyText = document.getElementById("whyText");
const whyMore = document.getElementById("whyMore");

const feedLoadingTitle = document.getElementById("feedLoadingTitle");
const feedProgress = document.getElementById("feedProgress");
const feedProgressBar = document.getElementById("feedProgressBar");
const feedProgressMeta = document.getElementById("feedProgressMeta");
const connBanner = document.getElementById("connBanner");
const connBannerText = document.getElementById("connBannerText");
const dropletVoltageHeld = document.getElementById("dropletVoltageHeld");
const dropletFallHeld = document.getElementById("dropletFallHeld");
const dropletVelocityHeld = document.getElementById("dropletVelocityHeld");
const starterPrompts = document.getElementById("starterPrompts");

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
  // Whether the current/last-started live session is the bundled demo clip
  // rather than a real camera -- drives the LIVE vs DEMO labeling.
  isDemo: false,
  // Wall-clock bookkeeping for the "elapsed session time" badge.
  sessionStartTs: null,
  sessionTimerInterval: null,
  // Used to notice droplet-pick and suitability *transitions* so the Message
  // panel gets one note per real event rather than one per poll.
  lastDropletLabel: null,
  lastBannerText: null,       // the aid's line as it currently reads
  lastWhy: null,              // ...and the explanation showing for it, if any
  lastSuitable: null,
  lastStage: null,            // which half of the test it has met
  // The last free-fall speed seen while fieldState was "off". Held like the
  // voltage and fall time: while the field is working or lifting the droplet is
  // held or rising, so its live speed says nothing about the droplet.
  lastSpeed: null,
  // Session start-up / re-attach bookkeeping.
  starting: false,            // /api/live/start is in flight
  cancelRequested: false,     // the operator pressed Cancel while it was
  attaching: false,           // re-attaching to a session this page did not start
  attachRetryAt: 0,
  // Connection health (drives the "connection lost" pill on the feed).
  pollFailures: 0,
  connLost: false,            // the server stopped answering
  streamLost: false,          // the video stream itself dropped
  streamRetryTimer: null,
  streamRetryAt: 0,
  streamProbing: false,

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

// ---------------------------------------------------------------------------
//  Notifications panel (right column).
//
//  Contextual experiment feedback (errors, a droplet becoming suitable, a
//  session ending, etc.) is surfaced here, not inside the AI chat -- the
//  chat panel is for talking with the tutor, not for status updates. The
//  newest notification is inserted at the top; existing cards animate
//  downward (a small FLIP) to make room. Once the panel is full, the oldest
//  (bottom-most) card fades out and is dropped.
// ---------------------------------------------------------------------------
const NOTIFICATION_ICONS = { info: "ℹ️", success: "✅", warning: "⚠️", error: "🛑" };
const MAX_NOTIFICATIONS = 6;

function createNotificationItem(kind, text) {
  const item = document.createElement("div");
  item.className = `notification-item ${kind} notification-entering`;
  item.addEventListener("animationend", () => item.classList.remove("notification-entering"), { once: true });

  const icon = document.createElement("span");
  icon.className = "icon";
  icon.textContent = NOTIFICATION_ICONS[kind] || NOTIFICATION_ICONS.info;

  const body = document.createElement("span");
  body.className = "notif-text";
  body.textContent = text;

  const time = document.createElement("span");
  time.className = "notif-time";
  time.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  item.append(icon, body, time);
  return item;
}

function removeNotification(el) {
  if (!el || el.classList.contains("notification-leaving")) return;
  el.style.transition = "";
  el.style.transform = "";
  el.classList.add("notification-leaving");
  el.addEventListener("animationend", () => {
    el.remove();
    if (!notificationList.querySelector(".notification-item")) {
      notificationPlaceholder.classList.remove("hidden");
    }
  }, { once: true });
}

function pushSystemMessage(kind, text) {
  notificationPlaceholder.classList.add("hidden");

  const existing = Array.from(notificationList.querySelectorAll(".notification-item"));
  // Once the panel is full, the card about to be pushed off the bottom is
  // handled separately below -- it should fade out in place, not join the
  // "settle downward" animation of the cards that are staying.
  const overflow = existing.length >= MAX_NOTIFICATIONS ? existing[existing.length - 1] : null;
  const settling = overflow ? existing.slice(0, -1) : existing;

  const oldTops = new Map();
  settling.forEach(el => oldTops.set(el, el.getBoundingClientRect().top));

  const item = createNotificationItem(kind, text);
  notificationList.prepend(item);

  // FLIP: measure where each staying card now sits after the new one pushed
  // it down, snap it back to where it was with a transform, then release
  // that transform so it transitions smoothly into its new spot.
  settling.forEach(el => {
    const oldTop = oldTops.get(el);
    const newTop = el.getBoundingClientRect().top;
    const delta = oldTop - newTop;
    if (!delta) return;
    el.style.transition = "none";
    el.style.transform = `translateY(${delta}px)`;
    requestAnimationFrame(() => {
      el.style.transition = "transform .3s ease";
      el.style.transform = "";
    });
  });

  if (overflow) removeNotification(overflow);
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
  aiFab.classList.remove("has-unread");
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

  if (starterPrompts) starterPrompts.remove();
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
    if (!assistantText && assistantBubble.querySelector(".thinking-status")) {
      assistantBubble.classList.add("error-bubble");
      assistantBubble.textContent = "Unable to get a response from the AI assistant.";
    }
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
// checkout keeps its fixtures somewhere else. This is an internal detail --
// it is never shown to the user; see DEMO_LABEL below for what they see.
const DEMO_CLIP_SOURCE = "app_js/fixtures/demo_clip.mp4";
const DEMO_LABEL = "the bundled demo clip";

const FEED_LABELS = {
  offline: { text: "[ MICROSCOPIC VIEW ] · OFFLINE", cls: "offline" },
  live:    { text: "[ LIVE MICROSCOPIC VIEW ] · LIVE", cls: "live" },
  demo:    { text: "[ DEMO MICROSCOPIC VIEW ] · DEMO", cls: "demo" },
};

function setFeedState(mode) {
  const info = FEED_LABELS[mode] || FEED_LABELS.offline;
  feedTitle.textContent = info.text;
  feedTitle.className = `feed-title ${info.cls}`;
}

// ---------------------------------------------------------------------------
//  Elapsed-time badge for the current live/demo session (top-left of the
//  video). This is a plain wall-clock readout of how long the session has
//  been running -- it does not come from the CV pipeline and has no effect
//  on it.
// ---------------------------------------------------------------------------
function formatElapsed(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const hh = Math.floor(s / 3600);
  const mm = Math.floor((s % 3600) / 60);
  const ss = s % 60;
  const pad = n => String(n).padStart(2, "0");
  return hh > 0 ? `${pad(hh)}:${pad(mm)}:${pad(ss)}` : `${pad(mm)}:${pad(ss)}`;
}

// The start time is remembered so a reloaded page (or a second tab) can carry
// on counting from the real start instead of from zero.
const SESSION_START_KEY = "millikanSessionStart";

function rememberSessionStart(ts) {
  try { localStorage.setItem(SESSION_START_KEY, String(ts)); } catch (_) {}
}

function forgetSessionStart() {
  try { localStorage.removeItem(SESSION_START_KEY); } catch (_) {}
}

function resumeSessionTimer() {
  let ts = NaN;
  try { ts = Number(localStorage.getItem(SESSION_START_KEY)); } catch (_) {}
  const age = Date.now() - ts;
  // No trustworthy start time (session begun elsewhere): show no timer rather than a wrong one.
  if (Number.isFinite(ts) && ts > 0 && age >= 0 && age < 12 * 3600 * 1000) startSessionTimer(ts);
}

function startSessionTimer(startTs = Date.now()) {
  state.sessionStartTs = startTs;
  sessionTimerText.textContent = formatElapsed((Date.now() - startTs) / 1000);
  sessionTimer.classList.remove("hidden");
  if (state.sessionTimerInterval) clearInterval(state.sessionTimerInterval);
  state.sessionTimerInterval = setInterval(() => {
    // Frozen while the connection is down, so it doesn't keep counting under a stalled feed.
    if (state.connLost || state.streamLost) return;
    sessionTimerText.textContent = formatElapsed((Date.now() - state.sessionStartTs) / 1000);
  }, 1000);
  renderConnectionBanner();
}

function stopSessionTimer() {
  if (state.sessionTimerInterval) clearInterval(state.sessionTimerInterval);
  state.sessionTimerInterval = null;
  state.sessionStartTs = null;
  forgetSessionStart();
  sessionTimer.classList.add("hidden");
  sessionTimer.classList.remove("stalled");
}

// ---------------------------------------------------------------------------
//  Start-up progress. The server does not report progress, so the bar is an
//  estimate: it eases toward ~90% over the expected time, never reaches 100%
//  by itself, and snaps to 100% the moment /api/live/start answers.
// ---------------------------------------------------------------------------
const START_ESTIMATE_SECONDS = { camera: 45, source: 30 };   // camera: bench check + background learning
let progressTimer = null;

function setProgress(pct) {
  feedProgressBar.style.width = `${pct}%`;
  feedProgress.setAttribute("aria-valuenow", String(Math.round(pct)));
}

function stopProgress() {
  if (progressTimer) clearInterval(progressTimer);
  progressTimer = null;
}

function startProgress(estimateSeconds) {
  stopProgress();
  feedProgress.classList.remove("indeterminate");
  const t0 = Date.now();
  const tick = () => {
    const elapsed = (Date.now() - t0) / 1000;
    setProgress(Math.max(3, 95 * (1 - Math.exp(-3 * elapsed / estimateSeconds))));
    feedProgressMeta.textContent = elapsed < estimateSeconds
      ? `About ${Math.max(1, Math.ceil(estimateSeconds - elapsed))} s left`
      : "Taking a little longer than usual…";
  };
  tick();
  progressTimer = setInterval(tick, 250);
}

function showIndeterminateProgress(text) {
  stopProgress();
  feedProgress.classList.add("indeterminate");
  feedProgressMeta.textContent = text || "";
}

function finishProgress() {
  stopProgress();
  feedProgress.classList.remove("indeterminate");
  setProgress(100);
  feedProgressMeta.textContent = "Ready";
  return new Promise(resolve => setTimeout(resolve, 260));
}

function resetLiveButton() {
  liveButton.classList.remove("is-cancel");
  liveButton.textContent = "▶ Start Live Feed";
  liveButton.disabled = false;
}

async function stopSessionOnServer(token) {
  try {
    await fetch("/api/live/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token }),
    });
  } catch (_) { /* nothing more to do from here */ }
}

// ---------------------------------------------------------------------------
//  Camera-index input. It is a camera INDEX, so the only accepted values are
//  empty (= default camera) or a whole number 0, 1, 2, ... Anything else --
//  letters, decimals, signs, exponents, spaces inside, non-ASCII digits -- is
//  rejected here, before any request is made.
// ---------------------------------------------------------------------------
const liveSourceError = document.getElementById("liveSourceError");
const CAMERA_INDEX_ERROR = "Camera index must be a whole number (0, 1, 2…). Leave it empty to use the default camera.";

// Returns { ok: true, value } with the index as a canonical string ("" = default),
// or { ok: false }.
function parseCameraIndex(raw) {
  const text = String(raw ?? "").trim();
  if (text === "") return { ok: true, value: "" };
  if (!/^[0-9]+$/.test(text)) return { ok: false };      // ASCII digits only
  const n = Number(text);
  if (!Number.isSafeInteger(n)) return { ok: false };
  return { ok: true, value: String(n) };                 // "007" -> "7"
}

function showCameraIndexError(message) {
  liveSourceError.textContent = message || "";
  liveSourceError.classList.toggle("hidden", !message);
  liveSourceInput.classList.toggle("invalid", Boolean(message));
  if (message) liveSourceInput.setAttribute("aria-invalid", "true");
  else liveSourceInput.removeAttribute("aria-invalid");
}

liveSourceInput.addEventListener("input", () => showCameraIndexError(""));

async function startLive(explicitSource) {
  // explicitSource is only ever the bundled demo clip; what the operator typed
  // is a camera index and is validated first.
  let source;
  if (explicitSource !== undefined) {
    source = String(explicitSource).trim();
    showCameraIndexError("");
  } else {
    const parsed = parseCameraIndex(liveSourceInput.value);
    if (!parsed.ok) {
      showCameraIndexError(CAMERA_INDEX_ERROR);
      liveSourceInput.focus();
      return;
    }
    showCameraIndexError("");
    source = parsed.value;
  }
  const isClip = explicitSource !== undefined;

  state.starting = true;
  state.cancelRequested = false;
  // While the session is starting the main button turns into Cancel.
  liveButton.classList.add("is-cancel");
  liveButton.textContent = "✕ Cancel";
  liveButton.disabled = false;
  liveSourceInput.disabled = true;
  demoClipButton.disabled = true;
  feedStatus.className = "feed-status";
  feedStatus.textContent = state.isDemo
    ? `Starting ${DEMO_LABEL}...`
    : source
      ? `Starting playback for "${source}"...`
      : "Running bench check and starting capture...";
  feedStatus.classList.remove("hidden", "error", "live", "demo");
  showLoading(state.isDemo
    ? "Opening the clip."
    : source
      ? `Opening "${source}".`
      : "Running the bench check, then learning the background for about 45 seconds. Do not spray yet.",
    isClip ? START_ESTIMATE_SECONDS.source : START_ESTIMATE_SECONDS.camera);

  try {
    const response = await fetch("/api/live/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(source ? { source } : {}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to start live feed.");

    if (state.cancelRequested) {
      // The server can't abort a start half-way, so it was allowed to finish;
      // now shut the session it just opened back down.
      await stopSessionOnServer(payload.token);
      state.live = null;
      showFallback();
      feedStatus.classList.add("hidden");
      pushSystemMessage("info", "Start cancelled.");
      resetLiveButton();
      scheduleLivePoll(0);
      return;
    }

    liveButton.disabled = true;            // no Cancel during the last flourish
    await finishProgress();
    state.live = payload;
    showLiveFeed(payload.url, payload.device, payload.source);
    const startedAt = Date.now();
    rememberSessionStart(startedAt);
    startSessionTimer(startedAt);
    liveButton.classList.remove("is-cancel");
    liveButton.textContent = "■ Stop Live Feed";
    liveButton.disabled = false;
    scheduleLivePoll(0);
  } catch (error) {
    state.live = null;
    showFallback();
    feedStatus.classList.add("hidden");
    if (state.cancelRequested) {
      pushSystemMessage("info", "Start cancelled.");
    } else {
      pushSystemMessage("error", `Couldn't start the ${state.isDemo ? "demo video" : "live feed"} — ${
        error?.message || "please plug in the dongle first."
      }`);
    }
    resetLiveButton();
    liveSourceInput.disabled = false;
    demoClipButton.disabled = false;
  } finally {
    state.starting = false;
    state.cancelRequested = false;
  }
}

function requestCancelStart() {
  if (!state.starting || state.cancelRequested) return;
  state.cancelRequested = true;
  liveButton.disabled = true;
  liveButton.textContent = "Cancelling…";
  feedLoadingTitle.textContent = "Cancelling…";
  feedLoadingBody.textContent = "Waiting for the camera to finish starting so it can be shut down cleanly.";
  showIndeterminateProgress("");
}

// A session this page did not start (page reloaded mid-session, or a second
// tab): /api/live/status carries no feed URL or click token, but
// /api/live/start hands back the running session's own when there is one, so
// ask it once and attach exactly as if this page had started it.
async function attachToRunningSession(info) {
  if (state.attaching || state.live || state.starting || Date.now() < state.attachRetryAt) return;
  state.attaching = true;
  state.isDemo = Boolean(info.source) && String(info.source) === DEMO_CLIP_SOURCE;
  try {
    const response = await fetch("/api/live/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(info.source ? { source: info.source } : {}),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "attach failed");
    if (state.live || state.starting) return;      // something else got there first
    state.live = payload;
    showLiveFeed(payload.url, payload.device, payload.source);
    resumeSessionTimer();
    liveButton.classList.remove("is-cancel");
    liveButton.textContent = "■ Stop Live Feed";
    liveButton.disabled = false;
    pushSystemMessage("info", "Reconnected to the session that is already running.");
  } catch (_) {
    state.attachRetryAt = Date.now() + 5000;       // don't hammer the server
  } finally {
    state.attaching = false;
  }
}

function showLoading(text, estimateSeconds) {
  if (text) feedLoadingBody.textContent = text;
  feedLoadingTitle.textContent = state.isDemo ? "Starting the demo…" : "Starting the camera…";
  // Play the expand-out from whatever the idle feed box is showing (the empty
  // state or the idle footage).
  const fromIdle = !videoPlaceholder.classList.contains("hidden") || !fallbackVideo.classList.contains("hidden");
  videoPlaceholder.classList.add("hidden");
  fallbackVideo.classList.add("hidden");
  liveFeed.classList.add("hidden");
  feedLoading.classList.toggle("expanding", fromIdle);
  feedLoading.classList.remove("hidden");
  startProgress(estimateSeconds || START_ESTIMATE_SECONDS.camera);
}

function hideLoading() {
  stopProgress();
  feedLoading.classList.add("hidden");
}

function showLiveFeed(url, device, source) {
  hideLoading();
  fallbackVideo.pause();
  fallbackVideo.classList.add("hidden");
  videoPlaceholder.classList.add("hidden");
  liveFeed.src = `${url}?t=${Date.now()}`;
  liveFeed.classList.remove("hidden");
  feedStatus.textContent = "🖱️ Click a droplet in the feed to select it";
  feedStatus.className = `feed-status ${state.isDemo ? "demo" : "live"}`;
  setFeedState(state.isDemo ? "demo" : "live");
  feedHint.classList.remove("hidden");
  liveSourceInput.disabled = true;
  demoClipButton.disabled = true;
}

function showFallback() {
  hideLoading();
  unfitFeedBox();
  clearStreamTrouble();
  liveFeed.removeAttribute("src");
  liveFeed.classList.add("hidden");
  if (fallbackVideo.readyState > 0) {
    fallbackVideo.classList.remove("hidden");
    videoPlaceholder.classList.add("hidden");
    // showLiveFeed() paused it; without this it would sit on a frozen frame.
    const playing = fallbackVideo.play();
    if (playing && playing.catch) playing.catch(() => {});
  } else {
    fallbackVideo.classList.add("hidden");
    videoPlaceholder.classList.remove("hidden");
  }
  feedStatus.classList.add("hidden");
  feedHint.classList.add("hidden");
  liveSourceInput.disabled = false;
  demoClipButton.disabled = false;
  setFeedState("offline");
}

demoClipButton.addEventListener("click", () => {
  if (state.live || state.starting) return;
  state.isDemo = true;
  // The input stays untouched (no internal fixture path shown in it) --
  // the demo status text and the DEMO feed-title label are what tell the
  // user a demo is running.
  startLive(DEMO_CLIP_SOURCE);
});

emptyStateStartButton.addEventListener("click", () => liveButton.click());

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

// Shape the box to the frame, so `contain` has nothing left to letterbox. An
// MJPEG <img> is one endless response, so its load event cannot be relied on to
// fire per frame -- the status carries the same two numbers, so either will do.
function fitFeedBox(w, h) {
  if (!w || !h) return;
  videoContainer.style.aspectRatio = `${w} / ${h}`;
  videoContainer.style.height = "auto";
  videoContainer.style.minHeight = "0";
}

// Idle, the box keeps the apparatus's shape rather than a tall placeholder: an
// empty panel far taller than any frame it will ever show is dead space before
// the session has even started. Real numbers replace these on the first frame.
// The DONGLE's frame, not the ROI's. These were the ROI's proportions, 909x645,
// which is a taller box than any frame the feed will ever put in it -- so the
// panel shrank the moment a session started and the whole column jumped with it.
// What the pipeline publishes is the app view, and that is the 1280x720 the
// capture delivers.
const FEED_ASPECT_W = 1280, FEED_ASPECT_H = 720;

function unfitFeedBox() {
  fitFeedBox(FEED_ASPECT_W, FEED_ASPECT_H);
}

async function askWhy() {
  if (whyButton.disabled) return;
  const asked = state.lastBannerText;
  whyButton.disabled = true;
  whyAnswer.classList.remove("hidden");
  whyAnswer.classList.add("thinking");
  whyText.textContent = "Working it out…";
  try {
    const response = await fetch("/api/ai/why", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "The assistant could not answer.");
    whyAnswer.classList.remove("thinking");
    whyText.textContent = payload.text;
    state.lastWhy = { asked, answer: payload.text };
  } catch (error) {
    whyAnswer.classList.remove("thinking");
    whyText.textContent = error.message;
    state.lastWhy = null;
  } finally {
    whyButton.disabled = false;
  }
}

// The chat keeps the conversation; the panel keeps the answer. Following up puts
// both halves in the chat so the model has the context the student is replying to.
function whyIntoChat() {
  if (!state.lastWhy) return;
  addMessage("user", `Why is the aid saying: "${state.lastWhy.asked}"?`);
  addMessage("assistant", state.lastWhy.answer);
  openAi();
  aiInput.focus();
}

whyButton.addEventListener("click", askWhy);
whyMore.addEventListener("click", whyIntoChat);

liveFeed.addEventListener("load", () => {
  fitFeedBox(liveFeed.naturalWidth, liveFeed.naturalHeight);
  if (state.streamLost) clearStreamTrouble();
});


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
    pushSystemMessage("error", `Couldn't confirm the feed stopped — ${error.message}`);
  } finally {
    state.live = null;
    resetHeldMeasurements();
    stopSessionTimer();
    showFallback();
    liveButton.textContent = "▶ Start Live Feed";
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

// IV.2 asks two things of a droplet and they are met at different moments: a
// usable free-fall speed, and a balance voltage in the working band. The card
// says which one it has, because "Not suitable" on a droplet that has already
// passed half the test reads as a rejection of the operator's own work.
const STAGE = {
  speed: { text: "Speed suitable", ring: "partial" },
  balance: { text: "Balanceable", ring: "partial" },
};
const SUITABILITY = {
  yes: { text: "Suitable", ring: "suitable" },
  no: { text: "Not suitable", ring: "unsuitable" },
  unknown: { text: "Not enough data", ring: "unknown" },
};

function showMeasurement(element, value, unit, digits) {
  const missing = value === null || value === undefined || Number.isNaN(Number(value));
  element.textContent = missing ? MISSING : `${Number(value).toFixed(digits)} ${unit}`;
}

// The voltage and fall time are "held" readings (see the two functions below):
// they keep their last genuine value until the right fieldState comes round
// again. That is only correct for the droplet they were measured on, so they
// must be dropped whenever the selection changes -- otherwise a newly picked
// droplet would show the previous one's numbers until fieldState happened to
// flip to "work"/"off".
function resetHeldMeasurements() {
  state.lastFieldOnVoltage = null;
  state.lastFallSeconds = null;
  state.lastSpeed = null;
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

function speedToDisplay(data) {
  // Same rule again: only a genuine "off" reading is a free-fall speed. The
  // live speed while working/lifting swings wildly (the droplet is held or
  // being pulled up) and is not what the lab manual's band is about.
  const speed = Number(data.speedGridPerSec);
  if (data.fieldState === "off" && data.speedGridPerSec !== null && data.speedGridPerSec !== undefined && Number.isFinite(speed)) {
    state.lastSpeed = data.speedGridPerSec;
  }
  return state.lastSpeed;
}

// Target bands from the lab manual. These mirror FALL_SPEED_MIN/MAX and
// BALANCE_VOLTS_MIN/MAX in web_server.py (which decides the actual verdict);
// they are only used here to tint each reading and to print the hint beside it.
const FALL_SPEED_RANGE = { min: 0.2, max: 0.5, unit: "grid/s" };
const BALANCE_VOLTS_RANGE = { min: 150, max: 250, unit: "V" };
document.getElementById("rangeVoltage").textContent =
  `target ${BALANCE_VOLTS_RANGE.min}–${BALANCE_VOLTS_RANGE.max} ${BALANCE_VOLTS_RANGE.unit}`;
document.getElementById("rangeVelocity").textContent =
  `target ${FALL_SPEED_RANGE.min}–${FALL_SPEED_RANGE.max} ${FALL_SPEED_RANGE.unit}`;

const FIELD_LABELS = { off: "Off (falling)", work: "Working", lift: "Lifting" };

// One reading: print it, tint it against its band, and mark it "held" when the
// field isn't in the state that measures it.
function showReading(valueEl, heldEl, value, unit, digits, range, held) {
  showMeasurement(valueEl, value, unit, digits);
  const has = value !== null && value !== undefined && Number.isFinite(Number(value));
  valueEl.classList.remove("in-range", "out-of-range");
  if (has && range) {
    const v = Math.abs(Number(value));
    valueEl.classList.add(v >= range.min && v <= range.max ? "in-range" : "out-of-range");
  }
  const showHeld = has && held;
  heldEl.classList.toggle("hidden", !showHeld);
  valueEl.closest(".info-item").classList.toggle("is-held", showHeld);
}

// The pipeline's own guidance line -- what it draws along the bottom of its
// window -- shown here instead, because the feed is cropped to the microscope
// picture on purpose. It is the instruction the operator acts on.
//
// ONE line, replaced in place, exactly like the banner in the pipeline's window.
// It was first built as notifications, and the operator's verdict was that stale
// cards piling up are "very disrupting": the aid's line is an instruction about
// right now, so a superseded one is not worth reading. Nothing here accumulates.
function bannerClass(text) {
  if (/BALANCED|USABLE|within the noise|as good as it gets/.test(text)) return "good";
  if (/DISCARD|no reachable balance/.test(text)) return "bad";
  if (/lost your droplet|left the view|DIFFERENT droplet|too heavy|RISING|barely held|will not hold still/.test(text)) return "warn";
  return "";
}

function showBanner(status, selected) {
  const raw = (status && status.message) || "";
  if (!raw) {
    state.lastBannerText = null;   // so the same line returning later is repainted
    guidanceLine.textContent = "Waiting for the aid…";
    guidanceLine.className = "guidance-line idle";
    return;
  }
  // "picked id 4015" is the tracker's number; the panel calls it DROPLET 9.
  const label = (selected && selected.label) || null;
  const text = label ? raw.replace(/picked id \d+/g, label) : raw;
  if (text === state.lastBannerText) return;      // same words, no repaint
  state.lastBannerText = text;
  whyButton.disabled = false;
  if (state.lastWhy && state.lastWhy.asked !== text) {
    whyAnswer.classList.add("hidden");       // it explained a line that has moved on
    state.lastWhy = null;
  }
  guidanceLine.textContent = text;
  guidanceLine.className = `guidance-line ${bannerClass(text)}`;
}

function clearBanner() {
  state.lastBannerText = null;
  state.lastWhy = null;
  whyAnswer.classList.add("hidden");
  whyButton.disabled = true;
  guidanceLine.textContent = "Start a session to see the aid's guidance here.";
  guidanceLine.className = "guidance-line idle";
}

// The tutor speaks rarely and only when our own rules say the student is stuck
// (app_js/tutor.py), so each message is worth a place in the conversation rather
// than a toast that scrolls away. It lands in the same chat the student types in,
// so "why?" can be asked straight back.
function showTutor(messages) {
  if (!Array.isArray(messages) || !messages.length) return;
  for (const m of messages) {
    if (!m || !m.text) continue;
    addMessage("assistant", m.text);
  }
  if (!aiPanel.classList.contains("open")) {
    aiFab.classList.add("has-unread");
    aiPromptBubble.textContent = "";
    const spark = document.createElement("span");
    spark.className = "ai-sparkle";
    spark.textContent = "✨";
    aiPromptBubble.append(spark, document.createTextNode("The tutor has a suggestion"));
    aiPromptBubble.style.display = "";
  }
}

function renderSelectedDroplet(selected) {
  const data = selected || {};
  const verdict = (data.suitable !== "yes" && STAGE[data.stage])
    || SUITABILITY[data.suitable] || SUITABILITY.unknown;

  // A genuinely new pick (not just the same droplet polled again) gets a
  // one-time guidance note; a suitability change (confirmed suitable, or
  // dropping back out after being suitable) gets its own note. Both reuse
  // the server's own note text -- nothing here is invented client-side.
  if (data.label && data.label !== state.lastDropletLabel) {
    // A different droplet: drop the previous one's held voltage / fall time now,
    // instead of waiting for fieldState to overwrite them.
    resetHeldMeasurements();
    state.lastDropletLabel = data.label;
    state.lastSuitable = null;
    state.lastStage = null;
  }
  // The verdict comes from web_server.py, which already does the field-aware
  // check: a fall speed only counts while the field is "off", the balance
  // voltage only while it is "work", the verdict stays "unknown" until both
  // readings exist, and it only flips after SETTLE_SECONDS. So nothing is
  // re-checked here -- the popup just announces the server's verdict and its
  // own note (which spells out which reading is out of range).
  if (data.suitable && data.suitable !== "unknown" && data.suitable !== state.lastSuitable) {
    const name = data.label || "the droplet";
    if (data.suitable === "yes") {
      pushSystemMessage("success", `Valid measurement — ${name} is suitable.${data.note ? ` ${data.note}` : ""}`);
    } else {
      pushSystemMessage(
        "warning",
        `${name} ${state.lastSuitable === "yes" ? "is no longer suitable." : "is not suitable."}${data.note ? ` ${data.note}` : ""}`
      );
    }
    state.lastSuitable = data.suitable;
  }

  // One card as each half of the test is met, and none for losing it: the
  // server no longer demotes a droplet that has passed, so the pair of
  // contradicting cards that appeared seconds apart cannot happen again.
  if (data.stage && data.stage !== state.lastStage) {
    // "suitable" is announced by the suitability block above, so it is not repeated here.
    if (data.stage === "speed") {
      pushSystemMessage("success", `${data.label || "The droplet"} has a usable falling speed.${data.note ? ` ${data.note}` : ""}`);
    } else if (data.stage === "balance") {
      pushSystemMessage("success", `${data.label || "The droplet"} balances in range.${data.note ? ` ${data.note}` : ""}`);
    }
    state.lastStage = data.stage;
  }

  if (!data.label) {
    resetHeldMeasurements();
    state.lastDropletLabel = null;
    state.lastSuitable = null;
    state.lastStage = null;
  }

  dropletLabel.textContent = data.label || MISSING;
  dropletStatus.textContent = verdict.text;
  dropletRing.className = `droplet-ring ${verdict.ring}`;
  dropletRing.title = data.note || verdict.text;

  const field = data.fieldState;
  showReading(dropletVoltage, dropletVoltageHeld, voltageToDisplay(data), "V", 0, BALANCE_VOLTS_RANGE, field !== "work");
  showReading(dropletFallTime, dropletFallHeld, fallSecondsToDisplay(data), "s", 1, null, field !== "off");
  showReading(dropletVelocity, dropletVelocityHeld, speedToDisplay(data), "grid/s", 2, FALL_SPEED_RANGE, field !== "off");
  dropletField.textContent = FIELD_LABELS[field] || (field ? String(field) : MISSING);
}

// The pipeline rewrites its status about once a second, so polling faster than
// that only re-reads the same file. When nothing is running there is nothing to
// keep up with, so ease off rather than stopping: the page then picks up a
// session started from elsewhere without anyone reloading it.
const LIVE_POLL_MS = 500;
const IDLE_POLL_MS = 5000;

function scheduleLivePoll(delay) {
  if (state.livePoll) clearTimeout(state.livePoll);
  state.livePoll = setTimeout(pollLiveState, delay ?? LIVE_POLL_MS);
}

// ---------------------------------------------------------------------------
//  Connection health. Two different things can go wrong and look the same to
//  the operator (a frozen picture under a LIVE label): the server stops
//  answering, or the video stream itself drops. Either one raises the pill on
//  the feed and freezes the session timer until things recover.
// ---------------------------------------------------------------------------
function renderConnectionBanner() {
  let message = "";
  if (state.connLost) {
    message = state.live || state.starting ? "Connection lost — reconnecting…" : "Can't reach the server — retrying…";
  } else if (state.streamLost) {
    message = "Video stream interrupted — reconnecting…";
  }
  connBannerText.textContent = message;
  connBanner.classList.toggle("hidden", !message);
  sessionTimer.classList.toggle("stalled", Boolean(message));
}

function clearStreamTrouble() {
  state.streamLost = false;
  state.streamProbing = false;
  if (state.streamRetryTimer) clearTimeout(state.streamRetryTimer);
  state.streamRetryTimer = null;
  renderConnectionBanner();
}

function scheduleStreamRetry() {
  if (state.streamRetryTimer) return;
  state.streamRetryTimer = setTimeout(() => {
    state.streamRetryTimer = null;
    if (!state.live || !state.streamLost) return;
    state.streamProbing = true;
    state.streamRetryAt = Date.now();
    liveFeed.src = `${state.live.url}?t=${Date.now()}`;   // if it errors again, the handler schedules another try
  }, 3000);
}

liveFeed.addEventListener("error", () => {
  if (!state.live) return;
  state.streamLost = true;
  state.streamProbing = false;
  renderConnectionBanner();
  scheduleStreamRetry();
});

async function pollLiveState() {
  let payload;
  try {
    const response = await fetch("/api/live/status");
    payload = await response.json();
  } catch (_) {
    state.pollFailures += 1;
    if (state.pollFailures >= (state.live ? 2 : 1) && !state.connLost) {
      state.connLost = true;
      renderConnectionBanner();
    }
    scheduleLivePoll(1500);
    return;
  }
  state.pollFailures = 0;
  if (state.connLost) {
    state.connLost = false;
    renderConnectionBanner();
  }
  // A reconnect attempt that has run for a few seconds without erroring again worked.
  if (state.streamLost && state.streamProbing && Date.now() - state.streamRetryAt > 2500) {
    clearStreamTrouble();
  }

  if (state.live && !payload.running) {
    state.live = null;
    resetHeldMeasurements();
    stopSessionTimer();
    showFallback();
    pushSystemMessage("warning", `The ${state.isDemo ? "demo playback" : "live session"} ended.`);
    liveButton.textContent = "▶ Start Live Feed";
    liveButton.disabled = false;
  }

  if (payload.running && !state.live && !state.starting) attachToRunningSession(payload);
  showTutor(payload.tutor);

  renderSelectedDroplet(payload.selected);
  if (payload.running && payload.status) {
    fitFeedBox(payload.status.frameWidth, payload.status.frameHeight);
  }
  if (payload.running) showBanner(payload.status, payload.selected);
  else clearBanner();
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

// Starter prompts: one click sends the question as if it had been typed.
if (starterPrompts) {
  starterPrompts.addEventListener("click", event => {
    const chip = event.target.closest(".starter-chip");
    if (!chip || state.generating) return;
    aiInput.value = chip.dataset.prompt || chip.textContent;
    updateInputState();
    sendQuery();
  });
}

liveButton.addEventListener("click", () => {
  if (state.starting) {
    requestCancelStart();
  } else if (state.live) {
    stopLive();
  } else {
    state.isDemo = false;
    startLive();
  }
});

// The idle footage may finish loading after the page's first paint; once it
// has, and nothing is running or starting, show it instead of the empty state.
fallbackVideo.addEventListener("loadeddata", () => {
  if (!state.live && !state.starting) showFallback();
});

// Enter in the camera-index box starts the feed, like pressing the button.
liveSourceInput.addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  if (!liveButton.disabled && !state.live && !state.starting) liveButton.click();
});

fallbackVideo.addEventListener("error", () => {
  fallbackVideo.classList.add("hidden");
  videoPlaceholder.classList.remove("hidden");
});

// ---- Keep [ NOTIFICATIONS ] level with the "Try Demo Video" button ----
// The button sits below the video, feed title, and (sometimes) a status line,
// and the control row can wrap on narrow desktops -- so its position isn't
// fixed. Measure it and hand the numbers to the stylesheet.
const NOTIF_TITLE_HEIGHT = 25;
const infoColumnEl = document.querySelector(".info-column");
const videoColumnEl = document.querySelector(".video-column");

function alignNotificationTitle() {
  if (!infoColumnEl || !demoClipButton) return;
  const colRect = infoColumnEl.getBoundingClientRect();
  const btnRect = demoClipButton.getBoundingClientRect();
  if (!btnRect.height) return; // hidden / not laid out yet
  const center = btnRect.top + btnRect.height / 2 - colRect.top;
  const top = Math.max(0, Math.round(center - NOTIF_TITLE_HEIGHT / 2));
  infoColumnEl.style.setProperty("--notif-title-top", `${top}px`);
  infoColumnEl.style.setProperty("--notif-col-height", `${top + NOTIF_TITLE_HEIGHT + 5}px`);
}

if (infoColumnEl && videoColumnEl) {
  if (typeof ResizeObserver === "function") {
    // Fires when the status line shows/hides, controls wrap, or the viewport changes.
    const ro = new ResizeObserver(alignNotificationTitle);
    ro.observe(videoColumnEl);
    ro.observe(demoClipButton);
  }
  window.addEventListener("resize", alignNotificationTitle);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(alignNotificationTitle);
  alignNotificationTitle();
}

updateInputState();
showFallback();
renderSelectedDroplet(null);
pollLiveState();