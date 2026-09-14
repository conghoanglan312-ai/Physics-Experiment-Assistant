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
const liveFeed = document.getElementById("liveFeed");
const fallbackVideo = document.getElementById("fallbackVideo");
const videoPlaceholder = document.getElementById("videoPlaceholder");
const feedStatus = document.getElementById("feedStatus");

const state = {
  messages: [],
  generating: false,
  live: null,
  livePoll: null,
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

  const mathNodes = root.querySelectorAll('mjx-container, math');
  mathNodes.forEach(node => {
    if (node.parentElement?.classList.contains('latex-scroll')) return;

    // Inline LaTeX must remain inline. The previous implementation wrapped
    // every equation in a block-level <div>, which forced things such as
    // "equilibrium voltage $U_n$" onto two lines. Only display equations
    // should behave like blocks.
    const isDisplay =
      node.getAttribute('display') === 'true' ||
      node.matches('mjx-container[display=\"true\"]');

    const wrapper = document.createElement(isDisplay ? 'div' : 'span');
    wrapper.className = `latex-scroll${isDisplay ? ' latex-display' : ' latex-inline'}`;
    node.parentNode.insertBefore(wrapper, node);
    wrapper.appendChild(node);

    // Do not create a horizontal scrollbar for short LaTeX such as q, t_1,
    // U_n, etc. Only enable scrolling when the rendered equation actually
    // exceeds the available width of its container.
    requestAnimationFrame(() => {
      const overflows = wrapper.scrollWidth > wrapper.clientWidth + 1;
      wrapper.classList.toggle('latex-overflow', overflows);
    });
  });
}

async function typesetChat(root) {
  if (!root || !window.MathJax?.typesetPromise) return;
  try {
    await window.MathJax.typesetPromise([root]);
    wrapLatexForHorizontalScroll(root);
  } catch (_) {}
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
  const dots = document.createElement("div");
  dots.className = "thinking-dots";
  for (let i = 0; i < 3; i++) {
    const dot = document.createElement("div");
    dot.className = "thinking-dot";
    dots.appendChild(dot);
  }
  bubble.appendChild(dots);
  row.append(icon, bubble);
  wrapper.appendChild(row);
  return wrapper;
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
        if (errorPayload?.error === "Query must contain between 1 and 2000 characters.") {
          message = errorPayload.error;
        }
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
          assistantText += payload.data ?? "";
          assistantBubble.innerHTML = renderMathAndMarkdown(assistantText);
          scrollChat();
          typesetChat(assistantBubble);
        } else if (payload.type === "error") {
          assistantBubble.classList.add("error-bubble");
          assistantBubble.textContent = "Unable to get a response from the AI assistant.";
          streamFinished = true;
          break;
        } else if (payload.type === "timeout") {
          assistantBubble.classList.add("error-bubble");
          assistantBubble.textContent = payload.data?.includes("30 seconds")
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
    assistantBubble.classList.add("error-bubble");
    assistantBubble.textContent = "Unable to get a response from the AI assistant.";
  } finally {
    if (assistantText) state.messages.push({ role: "assistant", content: assistantText });
    state.generating = false;
    sendButton.classList.remove("locked");
    sendButton.textContent = "Send";
    updateInputState();
    scrollChat();
  }
}

async function startLive() {
  liveButton.disabled = true;
  feedStatus.className = "feed-status";
  feedStatus.textContent = "Running bench check and starting capture...";
  feedStatus.classList.remove("hidden", "error", "live");

  try {
    const response = await fetch("/api/live/start", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Failed to start live feed.");

    state.live = payload;
    showLiveFeed(payload.url, payload.device);
    liveButton.textContent = "■ Stop live feed";
    liveButton.disabled = false;
    pollLiveState();
  } catch (error) {
    state.live = null;
    showFallback();
    feedStatus.textContent = "Please plug in the dongle first.";
    feedStatus.className = "feed-status error";
    liveButton.textContent = "▶ Start live feed";
    liveButton.disabled = false;
  }
}

function showLiveFeed(url, device) {
  fallbackVideo.pause();
  fallbackVideo.classList.add("hidden");
  videoPlaceholder.classList.add("hidden");
  liveFeed.src = `${url}?t=${Date.now()}`;
  liveFeed.classList.remove("hidden");
  feedStatus.textContent = `● LIVE — device ${device}`;
  feedStatus.className = "feed-status live";
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
}

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
    if (state.livePoll) clearTimeout(state.livePoll);
    showFallback();
    liveButton.textContent = "▶ Start live feed";
    liveButton.disabled = false;
  }
}

function pollLiveState() {
  if (!state.live) return;
  fetch("/api/live/status")
    .then(r => r.json())
    .then(payload => {
      if (!state.live) return;
      if (!payload.running) {
        state.live = null;
        showFallback();
        feedStatus.textContent = "The live session ended.";
        feedStatus.className = "feed-status error";
        liveButton.textContent = "▶ Start live feed";
        liveButton.disabled = false;
        return;
      }
      state.livePoll = setTimeout(pollLiveState, 1000);
    })
    .catch(() => {
      if (state.live) state.livePoll = setTimeout(pollLiveState, 1500);
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
