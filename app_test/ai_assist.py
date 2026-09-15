import html
import re

import latex2mathml.converter
import streamlit as st
import streamlit.components.v1 as components
from llm.main import getResponseFromGemini


# =========================================================
# Configuration
# =========================================================

SESSION_ID = "millikan_test_session"

# Character (not word) limit for a single query, enforced with
# Python's len(text). Exactly MAX_CHARS is still a valid, sendable
# query; only 0 and > MAX_CHARS are invalid.
MAX_CHARS = 2000

# Visual height (px) of the user-input text area before it starts
# scrolling internally. Textareas get a native browser scrollbar for
# free once their content exceeds this height - no extra JS needed.
INPUT_AREA_HEIGHT = 90


# =========================================================
# Message Content Rendering (LaTeX + Markdown)
#
# Streamlit's built-in Markdown/LaTeX support (remark/KaTeX)
# never fires inside our hand-rolled <div> HTML blocks below,
# since raw HTML blocks aren't re-parsed for markdown. So we
# do both conversions ourselves before dropping the result
# into the bubble HTML:
#
#   1. $...$ / $$...$$  -> MathML (rendered natively, no JS)
#   2. ### / ** / * / `  -> real HTML tags
#
# Math is extracted first and swapped for placeholder tokens
# so the markdown pass below can't mangle its contents (e.g.
# an underscore inside a LaTeX command). The placeholders are
# restored as the very last step.
# =========================================================

MATH_PATTERN = re.compile(
    r"\$\$(?P<block>.+?)\$\$|\$(?P<inline>[^$\n]+?)\$",
    re.DOTALL,
)

MATH_PLACEHOLDER = re.compile(r"\x00MATH(\d+)\x00")

CODE_PATTERN = re.compile(r"`([^`\n]+?)`")
CODE_PLACEHOLDER = re.compile(r"\x00CODE(\d+)\x00")

BOLD_PATTERN = re.compile(r"\*\*([^\n]+?)\*\*")
ITALIC_STAR_PATTERN = re.compile(r"(?<!\*)\*([^\n*]+?)\*(?!\*)")
ITALIC_UNDERSCORE_PATTERN = re.compile(r"(?<!_)_([^\n_]+?)_(?!_)")

HEADER_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
UNORDERED_ITEM_PATTERN = re.compile(r"^[-*]\s+(.*)$")
ORDERED_ITEM_PATTERN = re.compile(r"^\d+\.\s+(.*)$")


def _extract_math(text):
    """Replace $...$/$$...$$ spans with placeholder tokens.

    Returns the placeholder-laced text plus the list of rendered
    replacements (MathML, or an escaped fallback for bad LaTeX) to
    swap back in once markdown formatting has been applied.
    """

    placeholders = []

    def stash(match):
        is_block = match.group("block") is not None
        latex_src = (match.group("block") if is_block else match.group("inline")).strip()

        if not latex_src:
            # Empty "$$" or "$$$$" etc. - nothing to render, keep as-is.
            rendered = html.escape(match.group(0))
        else:
            try:
                rendered = latex2mathml.converter.convert(
                    latex_src,
                    display="block" if is_block else "inline",
                )
            except Exception:
                # Malformed LaTeX shouldn't break the whole message -
                # just show the raw source instead of rendered math.
                rendered = f'<span class="math-fallback">{html.escape(match.group(0))}</span>'

        placeholders.append(rendered)
        return f"\x00MATH{len(placeholders) - 1}\x00"

    return MATH_PATTERN.sub(stash, text), placeholders


def _format_inline(escaped_line):
    """Apply inline code/bold/italic to an already-HTML-escaped line.

    Escaping happens before this runs, so *, _ and ` are still the
    literal markdown characters while <, >, & etc. are already safe.
    """

    code_spans = []

    def stash_code(match):
        code_spans.append(match.group(1))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    line = CODE_PATTERN.sub(stash_code, escaped_line)

    line = BOLD_PATTERN.sub(r"<strong>\1</strong>", line)
    line = ITALIC_STAR_PATTERN.sub(r"<em>\1</em>", line)
    line = ITALIC_UNDERSCORE_PATTERN.sub(r"<em>\1</em>", line)

    def restore_code(match):
        return f"<code>{code_spans[int(match.group(1))]}</code>"

    return CODE_PLACEHOLDER.sub(restore_code, line)


def _format_escaped_line(raw_line):
    return _format_inline(html.escape(raw_line))


def _markdown_to_html(text):
    """Turn headers, lists, and plain lines into HTML block-by-block.

    Plain paragraph lines are still joined with <br> (matching the
    original behaviour), but headers/lists are emitted as real block
    elements with no stray <br> glued to them.
    """

    lines = text.split("\n")
    blocks = []  # list of ("line" | "block", html)
    i, n = 0, len(lines)

    while i < n:
        stripped = lines[i].strip()

        header_match = HEADER_PATTERN.match(stripped)
        if header_match:
            level = len(header_match.group(1))
            content = _format_escaped_line(header_match.group(2))
            blocks.append(("block", f"<h{level}>{content}</h{level}>"))
            i += 1
            continue

        if UNORDERED_ITEM_PATTERN.match(stripped):
            items = []
            while i < n:
                item_match = UNORDERED_ITEM_PATTERN.match(lines[i].strip())
                if not item_match:
                    break
                items.append(f"<li>{_format_escaped_line(item_match.group(1))}</li>")
                i += 1
            blocks.append(("block", "<ul>" + "".join(items) + "</ul>"))
            continue

        if ORDERED_ITEM_PATTERN.match(stripped):
            items = []
            while i < n:
                item_match = ORDERED_ITEM_PATTERN.match(lines[i].strip())
                if not item_match:
                    break
                items.append(f"<li>{_format_escaped_line(item_match.group(1))}</li>")
                i += 1
            blocks.append(("block", "<ol>" + "".join(items) + "</ol>"))
            continue

        blocks.append(("line", _format_escaped_line(lines[i])))
        i += 1

    parts = []
    for idx, (kind, content) in enumerate(blocks):
        if idx > 0 and kind == "line" and blocks[idx - 1][0] == "line":
            parts.append("<br>")
        parts.append(content)

    return "".join(parts)


def render_message_content(content):
    """Escape content and render both LaTeX math and basic Markdown.

    Any not-yet-closed "$", "**", "*", or "`" (as happens mid-stream
    while a token-by-token response is still typing) simply falls
    through as literal escaped text until it closes, at which point
    it snaps to its rendered form on the next re-render.
    """

    text_with_placeholders, math_replacements = _extract_math(str(content))
    rendered = _markdown_to_html(text_with_placeholders)

    def restore_math(match):
        return math_replacements[int(match.group(1))]

    return MATH_PLACEHOLDER.sub(restore_math, rendered)


# =========================================================
# Message Rendering
# =========================================================

def render_message(role, content, animate=True):
    """Render one chat message using the global chat CSS."""

    safe_content = render_message_content(content)

    animation_class = " chat-animate" if animate else ""

    if role == "user":
        st.markdown(
            f"""
            <div class="chat-message user{animation_class}">
                <div class="message-bubble">
                    {safe_content}
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    elif role == "assistant":
        st.markdown(
            f"""
            <div class="chat-message assistant{animation_class}">
                <div class="assistant-row">
                    <div class="ai-icon">✦</div>
                    <div class="message-bubble">
                        {safe_content}
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render_thinking_indicator(container):
    """Render the animated thinking dots."""

    container.markdown(
        """
        <div class="chat-message assistant">
            <div class="assistant-row">
                <div class="ai-icon">✦</div>
                <div class="message-bubble">
                    <div class="thinking-dots">
                        <div class="thinking-dot"></div>
                        <div class="thinking-dot"></div>
                        <div class="thinking-dot"></div>
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_streaming_message(container, response):
    """Render the currently streamed response without replaying the entrance animation."""

    safe_response = render_message_content(response)

    container.markdown(
        f"""
        <div class="chat-message assistant">
            <div class="assistant-row">
                <div class="ai-icon">✦</div>
                <div class="message-bubble">
                    {safe_response}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def scroll_chat_to_bottom():
    """Scroll the actual height-limited Streamlit chat container after DOM updates.

    NOTE: this only ever looks for the container with overflow-y: auto
    (the chat-history st.container(height=...)). The popover's outer
    wrapper is deliberately set to overflow: hidden in ui.py's CSS
    (see the "POPOVER LAYOUT" block), so this will keep finding the
    inner chat-history region only, never the header or input area.
    """

    components.html(
        """
        <script>
        (function () {
            const doc = window.parent.document;

            function findScrollContainer() {
                const popover = doc.querySelector('div[data-testid="stPopoverBody"]');
                if (!popover) {
                    return null;
                }

                return Array.from(popover.querySelectorAll('.stVerticalBlock'))
                    .find(el => {
                        const style = getComputedStyle(el);
                        return style.overflowY === 'auto'
                            && el.clientHeight > 0;
                    }) || null;
            }

            function scrollToBottom(el) {
                if (!el) return;
                el.scrollTop = el.scrollHeight;
            }

            function startScrolling() {
                const scrollContainer = findScrollContainer();
                if (!scrollContainer) {
                    requestAnimationFrame(startScrolling);
                    return;
                }

                // Scroll immediately, then keep the container pinned to the
                // bottom while Streamlit inserts the freshly-rendered user
                // message / thinking indicator into the DOM.
                scrollToBottom(scrollContainer);

                const observer = new MutationObserver(() => {
                    scrollToBottom(scrollContainer);
                });

                observer.observe(scrollContainer, {
                    childList: true,
                    subtree: true
                });

                // Give Streamlit enough time to finish this render cycle,
                // then stop observing so normal manual scrolling is preserved.
                setTimeout(() => {
                    observer.disconnect();
                    scrollToBottom(scrollContainer);
                }, 1000);
            }

            startScrolling();
        })();
        </script>
        """,
        height=0,
    )


def render_input_enhancements_script():
    """Client-side conveniences layered on top of st.text_area:

    1. A live '<n> / 2000' counter. st.text_area only pushes its value to
       Python on blur / Ctrl+Enter, not on every keystroke, so a purely
       server-driven counter would lag behind typing. This attaches a
       plain 'input' listener directly to the underlying <textarea> DOM
       node for the visual counter only - it never decides whether a
       query can be submitted; that's Python's job (see
       can_submit_query), checked both when the Send button is drawn and
       again right before a query is actually dispatched to Gemini.

    2. Enter-to-send / Shift+Enter-for-newline. Plain Enter is
       intercepted, blurs the textarea first (so Streamlit commits the
       latest keystrokes to st.session_state before anything else
       happens), then clicks the real Send <button> so Python's own
       click-handling + validation path runs exactly as if the user had
       clicked it themselves. No Ctrl+Enter handling is added here at
       all - Shift+Enter is left completely untouched and still inserts
       a newline, which is the browser's normal textarea behaviour.

    3. Clearing the box immediately (client-side) the moment a valid
       Send happens - via Enter or by clicking the button - instead of
       waiting for the next full Streamlit rerun to reset it.
    """

    components.html(
        f"""
        <script>
        (function () {{
            const doc = window.parent.document;
            const MAX_CHARS = {MAX_CHARS};

            function setup() {{
                const popover = doc.querySelector('div[data-testid="stPopoverBody"]');
                if (!popover) {{
                    requestAnimationFrame(setup);
                    return;
                }}

                const textarea = popover.querySelector('textarea');
                const counter = popover.querySelector('.ai-char-counter');
                const sendBtn = popover.querySelector('[data-testid="stButton"] button');

                if (!textarea || !counter || !sendBtn) {{
                    requestAnimationFrame(setup);
                    return;
                }}

                function nudgeRepaint() {{
                    // Chromium can fail to repaint a descendant of a
                    // backdrop-filter element (the popover uses one for
                    // its glass effect) immediately after its disabled
                    // state flips, until something else forces a
                    // repaint - e.g. hovering it. This nudges it
                    // without any visible flicker.
                    requestAnimationFrame(function () {{
                        sendBtn.style.opacity = '0.999';
                        requestAnimationFrame(function () {{
                            sendBtn.style.opacity = '';
                        }});
                    }});
                }}

                function syncInputState() {{
                    // Drives both the counter AND the Send button's
                    // disabled state directly from the textarea's
                    // *actual* current value on every keystroke.
                    // st.text_area only commits to Python on blur /
                    // Ctrl+Enter, so without this the button would only
                    // ever update once focus leaves the box - and,
                    // since Python's session_state can briefly be stale
                    // relative to the real DOM value right after a
                    // rerun (see render_ai_assistant()'s docstring),
                    // this also self-corrects that mismatch on setup.
                    const len = textarea.value.length;
                    counter.textContent = len + ' / ' + MAX_CHARS;
                    counter.classList.toggle('over-limit', len >= MAX_CHARS);

                    const shouldDisable = (len === 0 || len > MAX_CHARS);
                    if (sendBtn.disabled !== shouldDisable) {{
                        sendBtn.disabled = shouldDisable;
                        nudgeRepaint();
                    }}
                }}

                // Both of these run on *every* rerun, unconditionally -
                // cheap, idempotent, and needed because the button
                // itself gets fully replaced (real <button> <-> locked
                // <div>) whenever generation starts/stops, so its state
                // needs re-checking against the real textarea value
                // every time, not just once.
                syncInputState();
                nudgeRepaint();

                // The <button> node is recreated on that swap too, so
                // its own click listener is (re)bound per-node here
                // rather than gated on the textarea's one-time guard
                // below.
                if (!sendBtn._aiClickBound) {{
                    sendBtn._aiClickBound = true;

                    // Fires on any valid click (a disabled button never
                    // dispatches click events at all, so reaching this
                    // means the click was valid) - clears the box
                    // immediately, client-side only. This deliberately
                    // never touches st.session_state: doing so is what
                    // previously wiped out a pre-written next question
                    // once generation finished (see ai_assist.py).
                    sendBtn.addEventListener('click', function () {{
                        requestAnimationFrame(function () {{
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLTextAreaElement.prototype, 'value'
                            ).set;
                            setter.call(textarea, '');
                            textarea.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        }});
                    }});
                }}

                // The textarea persists across reruns (only the button
                // gets swapped), so these only need binding once.
                if (textarea._aiInputBound) {{
                    return;
                }}
                textarea._aiInputBound = true;

                textarea.addEventListener('input', syncInputState);

textarea.addEventListener('keydown', function(e) {{
    if (e.key !== "Enter" || e.shiftKey) {{
        return;
    }}

    e.preventDefault();
    e.stopPropagation();

    const query = textarea.value.trim();

    if (!query) {{
        return;
    }}

    // Prevent duplicate Enter submissions
    if (popover.dataset.aiSubmitting === "true") {{
        return;
    }}

    popover.dataset.aiSubmitting = "true";

    console.log(
        "AI Enter submit:",
        query
    );


    // Notify Streamlit that the textarea value changed.
    // Do NOT fire both input and change because it can cause
    // extra reruns before the button event.
    textarea.dispatchEvent(
        new Event(
            "input",
            {{
                bubbles: true
            }}
        )
    );


    // Allow Streamlit frontend state to settle,
    // then trigger the existing Send button.
    setTimeout(function() {{

        const btn =
            popover.querySelector(
                '[data-testid="stButton"] button'
            );


        console.log(
            "AI Send button:",
            btn,
            "disabled:",
            btn ? btn.disabled : "missing"
        );


        if (!btn) {{
            popover.dataset.aiSubmitting = "false";
            return;
        }}


        // Trigger Streamlit button event
        btn.click();


        // Allow future submissions after rerun
        setTimeout(function() {{
            popover.dataset.aiSubmitting = "false";
        }}, 1500);


    }}, 200);

}});
            }}

            setup();
        }})();
        </script>
        """,
        height=0,
    )


# =========================================================
# State / validation helpers
# =========================================================

def initialize_chat_state():
    """Set up all session-state this module relies on."""

    if "messages" not in st.session_state:
        st.session_state.messages = []

    if "ai_generating" not in st.session_state:
        st.session_state.ai_generating = False

    if "ai_query_text" not in st.session_state:
        st.session_state.ai_query_text = ""


def count_characters(text):
    """Character count via Python's len() - explicitly NOT a word count."""

    return len(text or "")


def can_submit_query(text, generating=None):
    """Single source of truth for whether a query may be sent right now.

    Used both to decide the Send button's disabled state and, again,
    as a backend safety check immediately before a query is actually
    dispatched - so a stale click can never slip a second generation in
    while one is already running, and can never submit 0 or >2000 chars
    even if the frontend's disabled state was somehow out of date.
    """

    if generating is None:
        generating = st.session_state.get("ai_generating", False)

    if generating:
        return False

    n = count_characters(text)
    return 1 <= n <= MAX_CHARS


def render_char_counter(count):
    """Render the '<n> / 2000' counter, turning red at/over the limit."""

    css_class = "ai-char-counter over-limit" if count >= MAX_CHARS else "ai-char-counter"

    st.markdown(
        f'<div class="{css_class}">{count} / {MAX_CHARS}</div>',
        unsafe_allow_html=True,
    )


# =========================================================
# Layout sections
# =========================================================

def render_chat_history():
    """Render the scrollable chat-history region and return its container."""

    chat_area = st.container(height=360, border=False)

    with chat_area:
        for message in st.session_state.messages:
            render_message(
                message["role"],
                message["content"],
                animate=False,
            )

    return chat_area


def render_locked_send_button(placeholder):
    """Swap the real Send button for an inert 'Generating…' visual.

    Called the instant a submission is accepted - before the slow
    Gemini call starts - so the browser reflects GENERATING for the
    rest of this run without waiting for the final st.rerun(). This is
    a plain styled <div>, not a second st.button(): Streamlit forbids
    two widgets sharing one key in the same run, and more importantly,
    a non-widget div can't dispatch a Streamlit click at all - so even
    a stray click during generation is a genuine no-op, closing the
    race where a second query could be submitted before the Gemini
    call actually starts (the button's disabled= only takes effect
    from the *next* full rerun onward, which is too late by itself).
    """

    placeholder.markdown(
        '<div class="ai-send-btn-locked">Generating…</div>',
        unsafe_allow_html=True,
    )


def render_input_area():
    """Render the character-limited, scrollable, generation-aware input row.

    Replaces st.chat_input, which doesn't expose a disabled state, a
    custom send button, or a live character counter. A plain
    st.text_area gives us all of that plus a free native vertical
    scrollbar once its content exceeds INPUT_AREA_HEIGHT, without any
    extra JS to fake the scrolling.

    Returns (submit_clicked, current_text, button_placeholder).
    """

    with st.container():
        text = st.text_area(
            "Ask something about the experiment...",
            key="ai_query_text",
            height=INPUT_AREA_HEIGHT,
            placeholder="Ask something about the experiment...",
            label_visibility="collapsed",
        )

        generating = st.session_state.ai_generating
        char_count = count_characters(text)
        submit_enabled = can_submit_query(text, generating)

        counter_col, button_col = st.columns([3, 1])

        with counter_col:
            render_char_counter(char_count)

        with button_col:
            button_placeholder = st.empty()

            if generating:
                # Only reached if a rerun happens to land mid-generation
                # for some other reason - the normal path locks the
                # button explicitly, see render_ai_assistant() below.
                render_locked_send_button(button_placeholder)
                submit_clicked = False
            else:
                with button_placeholder:
                    submit_clicked = st.button(
                        "Send",
                        key="ai_send_button",
                        disabled=not submit_enabled,
                        use_container_width=True,
                    )

    # Live counter + Enter-to-send, both purely client-side enhancements
    # layered on top of the Python-authoritative validation above.
    render_input_enhancements_script()

    return submit_clicked, text, button_placeholder


# =========================================================
# AI Assistant
# =========================================================

def render_ai_assistant():
    """Top-level entry point, called once per script run.

    Note on the input box's contents: st.session_state.ai_query_text is
    never forcibly reset to "" anywhere in this function, even right
    after a query is sent. Doing that used to be how the box was
    cleared - but since st.text_area only commits the browser's real
    value to session_state on blur/Ctrl+Enter, session_state can be
    "stale" (still holding the just-submitted text) for a while after
    sending, while the user is busy pre-writing their next question in
    the meantime. Explicitly overwriting session_state during that
    window doesn't just correct the stale value - it's a genuine change
    Streamlit pushes back down to the browser, which overwrites
    whatever the user was mid-typing. So clearing is handled purely
    client-side instead (see the click handler in
    render_input_enhancements_script), which fires immediately at send
    time - before there's anything to pre-write yet - and never touches
    session_state at all. The live counter/Send-button JS in that same
    function independently drives off the textarea's real value on
    every keystroke, so the UI stays correct even while session_state
    is temporarily behind.
    """

    initialize_chat_state()

    # -----------------------------------------------------
    # Chat history (its own scroll region)
    # -----------------------------------------------------

    chat_area = render_chat_history()

    # Land on the latest message whenever the popover is (re)opened.
    scroll_chat_to_bottom()

    # -----------------------------------------------------
    # Input (its own scroll region, always editable)
    # -----------------------------------------------------

    submit_clicked, current_text, button_placeholder = render_input_area()

    if not submit_clicked:
        return

    # Backend safety check - authoritative regardless of what the
    # button's frontend disabled state looked like at click time.
    # Blocks: empty text, >2000 chars, and a second submission while
    # st.session_state.ai_generating is already True.
    if not can_submit_query(current_text, st.session_state.ai_generating):
        return

    user_query = current_text

    print(
        "DEBUG:",
        submit_clicked,
        repr(current_text),
        repr(st.session_state.ai_query_text)
    )

    # -----------------------------------------------------
    # Save + display user message, enter GENERATING state
    # -----------------------------------------------------

    st.session_state.messages.append({
        "role": "user",
        "content": user_query,
    })
    st.session_state.ai_generating = True

    # Lock the Send button *right now*, in this same run, before the
    # slow Gemini call below - this is what actually closes the race
    # where a second query could be sent while the first is still
    # "thinking". Waiting for the final st.rerun() to disable it would
    # leave a real, clickable button on screen for the entire
    # generation, which is exactly what let a second submission through
    # before.
    render_locked_send_button(button_placeholder)

    with chat_area:
        render_message(
            "user",
            user_query,
            animate=True,
        )

        assistant_container = st.empty()

    # Scroll once after the user's message has been rendered.
    scroll_chat_to_bottom()

    # -----------------------------------------------------
    # Thinking
    # -----------------------------------------------------

    render_thinking_indicator(assistant_container)

    # Scroll again after the thinking indicator has been rendered.
    scroll_chat_to_bottom()

    # -----------------------------------------------------
    # Gemini response
    # -----------------------------------------------------

    response = ""

    try:
        stream = getResponseFromGemini(
            userQuery=user_query,
            sessionID=SESSION_ID,
        )

        for chunk in stream:

            if chunk.type == "text":
                response += chunk.data

                # No entrance animation while streaming.
                # Otherwise every token would restart the animation.
                render_streaming_message(
                    assistant_container,
                    response,
                )

            elif chunk.type == "error":
                assistant_container.error(str(chunk.data))
                break

            elif chunk.type == "done":
                break

    except Exception as e:
        assistant_container.error(f"AI assistant error: {e}")

    finally:
        # Always leave GENERATING state on done, error, or an
        # unexpected exception - never leave the Send button stuck
        # disabled.
        st.session_state.ai_generating = False

    scroll_chat_to_bottom()

    # -----------------------------------------------------
    # Save final response
    # -----------------------------------------------------

    if response:
        st.session_state.messages.append({
            "role": "assistant",
            "content": response,
        })

    # Fresh run: clears the input box (via the pending-clear flag set
    # above), redraws the Send button enabled, and redraws chat history
    # from st.session_state.messages in one consistent pass.
    st.rerun()
