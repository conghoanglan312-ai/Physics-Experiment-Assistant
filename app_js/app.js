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
  lastSuitable: null,
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

function startSessionTimer() {
  state.sessionStartTs = Date.now();
  sessionTimerText.textContent = formatElapsed(0);
  sessionTimer.classList.remove("hidden");
  if (state.sessionTimerInterval) clearInterval(state.sessionTimerInterval);
  state.sessionTimerInterval = setInterval(() => {
    sessionTimerText.textContent = formatElapsed((Date.now() - state.sessionStartTs) / 1000);
  }, 1000);
}

function stopSessionTimer() {
  if (state.sessionTimerInterval) clearInterval(state.sessionTimerInterval);
  state.sessionTimerInterval = null;
  state.sessionStartTs = null;
  sessionTimer.classList.add("hidden");
}

async function startLive(explicitSource) {
  const source = (explicitSource ?? liveSourceInput.value).trim();

  liveButton.disabled = true;
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
      : "Running the bench check, then learning the background for about 30 seconds. Do not spray yet.");

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
    startSessionTimer();
    liveButton.textContent = "■ Stop Live Feed";
    liveButton.disabled = false;
    scheduleLivePoll(0);
  } catch (error) {
    state.live = null;
    showFallback();
    feedStatus.classList.add("hidden");
    pushSystemMessage("error", `Couldn't start the ${state.isDemo ? "demo video" : "live feed"} — ${
      error?.message || "please plug in the dongle first."
    }`);
    liveButton.textContent = "▶ Start Live Feed";
    liveButton.disabled = false;
    liveSourceInput.disabled = false;
    demoClipButton.disabled = false;
  }
}

function showLoading(text) {
  if (text) feedLoadingBody.textContent = text;
  // Only play the expand-out when coming from the empty-state box.
  const fromPlaceholder = !videoPlaceholder.classList.contains("hidden");
  videoPlaceholder.classList.add("hidden");
  fallbackVideo.classList.add("hidden");
  liveFeed.classList.add("hidden");
  feedLoading.classList.toggle("expanding", fromPlaceholder);
  feedLoading.classList.remove("hidden");
}

function hideLoading() {
  feedLoading.classList.add("hidden");
}

function showLiveFeed(url, device, source) {
  hideLoading();
  fallbackVideo.pause();
  fallbackVideo.classList.add("hidden");
  videoPlaceholder.classList.add("hidden");
  liveFeed.src = `${url}?t=${Date.now()}`;
  liveFeed.classList.remove("hidden");
  if (state.isDemo) {
    feedStatus.textContent = `🖱️ Click a droplet in the feed to select it`;
    feedStatus.className = "feed-status demo";
  } else {
    feedStatus.textContent = `🖱️ Click a droplet in the feed to select it`;
    feedStatus.className = "feed-status live";
  }
  setFeedState(state.isDemo ? "demo" : "live");
  feedHint.classList.remove("hidden");
  liveSourceInput.disabled = true;
  demoClipButton.disabled = true;
}

function showFallback() {
  hideLoading();
  unfitFeedBox();
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
  setFeedState("offline");
}

demoClipButton.addEventListener("click", () => {
  if (state.live) return;
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
const FEED_ASPECT_W = 909, FEED_ASPECT_H = 645;

function unfitFeedBox() {
  fitFeedBox(FEED_ASPECT_W, FEED_ASPECT_H);
}

liveFeed.addEventListener("load", () => fitFeedBox(liveFeed.naturalWidth, liveFeed.naturalHeight));

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
    guidanceLine.textContent = "Waiting for the aid…";
    guidanceLine.className = "guidance-line idle";
    return;
  }
  // "picked id 4015" is the tracker's number; the panel calls it DROPLET 9.
  const label = (selected && selected.label) || null;
  const text = label ? raw.replace(/picked id \d+/g, label) : raw;
  if (text === state.lastBannerText) return;      // same words, no repaint
  state.lastBannerText = text;
  guidanceLine.textContent = text;
  guidanceLine.className = `guidance-line ${bannerClass(text)}`;
}

function clearBanner() {
  state.lastBannerText = null;
  guidanceLine.textContent = "Start a session to see the aid's guidance here.";
  guidanceLine.className = "guidance-line idle";
}

function renderSelectedDroplet(selected) {
  const data = selected || {};
  const verdict = SUITABILITY[data.suitable] || SUITABILITY.unknown;

  // A genuinely new pick (not just the same droplet polled again) gets a
  // one-time guidance note; a suitability change (confirmed suitable, or
  // dropping back out after being suitable) gets its own note. Both reuse
  // the server's own note text -- nothing here is invented client-side.
  if (data.label && data.label !== state.lastDropletLabel) {
    if (data.note) pushSystemMessage("info", `${data.label} selected — ${data.note}`);
    // A different droplet: drop the previous one's held voltage / fall time now,
    // instead of waiting for fieldState to overwrite them.
    resetHeldMeasurements();
    state.lastDropletLabel = data.label;
    state.lastSuitable = null;
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
  if (!data.label) {
    resetHeldMeasurements();
    state.lastDropletLabel = null;
    state.lastSuitable = null;
  }

  dropletLabel.textContent = data.label || MISSING;
  dropletStatus.textContent = verdict.text;
  dropletRing.className = `droplet-ring ${verdict.ring}`;
  dropletRing.title = data.note || verdict.text;

  showMeasurement(dropletVoltage, voltageToDisplay(data), "V", 0);
  showMeasurement(dropletFallTime, fallSecondsToDisplay(data), "s", 1);
  showMeasurement(dropletVelocity, data.speedGridPerSec, "grid/s", 2);
  dropletField.textContent = data.fieldState || MISSING;
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
    resetHeldMeasurements();
    stopSessionTimer();
    showFallback();
    pushSystemMessage("warning", `The ${state.isDemo ? "demo playback" : "live session"} ended.`);
    liveButton.textContent = "▶ Start Live Feed";
    liveButton.disabled = false;
  }

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

liveButton.addEventListener("click", () => {
  if (state.live) {
    stopLive();
  } else {
    state.isDemo = false;
    startLive();
  }
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