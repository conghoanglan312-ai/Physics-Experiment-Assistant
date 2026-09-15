import streamlit as st
from ai_assist import render_ai_assistant
import os
import base64
import time

try:
    from wrapper import start_live, stop_live
except ImportError:
    start_live = stop_live = None  # dev machine without cv/, camera, etc.

video_path = "app_test/test_footage.mp4"

st.set_page_config(
    page_title="Millikan AI Assistant",
    page_icon="🔬",
    layout="wide"
)

if "ai_open" not in st.session_state:
    st.session_state.ai_open = False

if "live_feed" not in st.session_state:
    st.session_state.live_feed = None       # dict from wrapper.start_live(), or None
if "live_feed_error" not in st.session_state:
    st.session_state.live_feed_error = None

# -------------------------
# Custom Wavy Gradient CSS
# -------------------------

st.markdown(
"""
<style>

.stApp {
    position: relative;
}

.stApp::before {

    content: "";
    position: fixed;
    width: 140%;
    height: 140%;
    top: -20%;
    left: -20%;
    pointer-events: none;
    z-index: 0;

    background:
    radial-gradient(
        ellipse at 20% 25%,
        rgba(56,189,248,0.45),
        transparent 35%
    ),

    radial-gradient(
        ellipse at 80% 35%,
        rgba(99,102,241,0.40),
        transparent 40%
    ),

    radial-gradient(
        ellipse at 50% 85%,
        rgba(129,140,248,0.25),
        transparent 45%
    );
    
    filter: blur(55px);
    animation: glowMove 18s ease-in-out infinite alternate;

}



.stApp > * {
    position: relative;
    z-index: 1;
}

@keyframes glowMove {
    0% {
        transform: translate(-8%, -5%) scale(1);
    }

    50% {
        transform: translate(5%, 8%) scale(1.12);
    }

    100% {
        transform: translate(-5%, 3%) scale(1);
    }
}


</style>
""",
unsafe_allow_html=True
)


# -------------------------
# Title Box
# -------------------------

st.markdown(
"""
<style>

.title-box {
    background: rgba(230,230,230,0.15);
    backdrop-filter: blur(15px);
    -webkit-backdrop-filter: blur(15px);
    border-radius: 20px;
    border: 1px solid rgba(128,128,128,0.25);
    padding: 30px 60px;
    width: fit-content;
    margin: -20px auto 0 auto;
    box-shadow:
        0 8px 30px rgba(0,0,0,0.12);
    text-align: center;
}

.main-title {
    text-align: center;
    color: #6668FA;
    font-size: 55px;
    font-weight: 800;
    margin-top: -10px;
    margin-bottom: 5px;
    text-shadow:
        0 0 20px rgba(99,102,241,0.25);
}

.subtitle {
    text-align: center;
    opacity: 70%;
    font-size: 22px;
    margin-top: -15px;
}

</style>
""",
unsafe_allow_html=True
)


st.markdown(
"""
<div class="title-box">
<div class="main-title">
🔬 Millikan Oil Drop AI Assistant
</div>

<div class="subtitle">
AI-powered laboratory assistant for oil droplet selection
</div>

</div>
""",
unsafe_allow_html=True
)

# -------------------------
# Live Video Feed + Info Panel
# -------------------------

st.markdown(
"""
<style>

.video-container {
    width: 100%;
    height: 500px;
    background: rgba(230,230,230,0.15);
    backdrop-filter: blur(15px);
    -webkit-backdrop-filter: blur(15px);
    border-radius: 20px;
    border: 1px solid rgba(128,128,128,0.25);
    display: flex;
    justify-content: center;
    align-items: center;
    box-shadow:
    0 8px 30px rgba(0,0,0,0.12);
}


.video-placeholder {
    width: 95%;
    height: 90%;
    background: rgba(0,0,0,0.25);
    border-radius: 15px;
    display: flex;
    justify-content: center;
    align-items: center;
    color: rgba(255,255,255,0.6);
    font-size: 22px;
    letter-spacing: 3px;
}


.feed-title {
    text-align: center;
    margin-top: 10px;
    font-size: 18px;
    font-weight: 600;
    letter-spacing: 4px;
    opacity: 70%;
}

/* The embedded MJPEG feed from the live pipeline. This is a pure display --
   pointer-events: none means no click, drag, or right-click context menu is
   ever possible on it; the only thing it can do is show pixels. */
.live-feed-img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    border-radius: 15px;
    pointer-events: none;
    user-select: none;
}

.feed-status {
    text-align: center;
    margin-top: 6px;
    font-size: 13px;
    letter-spacing: 1px;
    opacity: 60%;
}

.feed-status.live {
    color: #22c55e;
    opacity: 0.9;
}

.feed-status.error {
    color: #ef4444;
    opacity: 0.9;
}

.info-panel {
    height: 500px;
    background: rgba(230,230,230,0.15);
    backdrop-filter: blur(15px);
    -webkit-backdrop-filter: blur(15px);
    border-radius: 20px;
    border: 1px solid rgba(128,128,128,0.25);
    padding: 25px;
    box-shadow:
    0 8px 30px rgba(0,0,0,0.12);
}

.droplet-preview-box {
    width: 100%;
    height: 90px;
    background: rgba(0,0,0,0.25);
    border-radius: 15px;

    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 20px;
    box-sizing: border-box;
    color: rgba(255,255,255,0.6);
    font-size: 14px;
    letter-spacing: 2px;
    margin-bottom: 15px;
}

.droplet-info {
    display: flex;
    flex-direction: column;
    gap: 8px;
}


.droplet-title {
    font-size: 19px;
    font-weight: 700;
    letter-spacing: 2px;
    color: rgba(255,255,255,0.9);
}


.droplet-id {
    font-size: 16px;
    color: rgba(255,255,255,0.75);
    margin-top: -10px;
}

.droplet-indicator {
    width: 55px;
    height: 55px;
    display:flex;
    justify-content:center;
    align-items:center;
}

.droplet-ring {
    width: 55px;
    height: 55px;
    border-radius:50%;
    border: 7px solid grey;
    position:relative;
}

.droplet-ring.suitable {
    border-color: #22c55e;
    box-shadow:
    0 0 15px rgba(34,197,94,0.6);
}

.droplet-ring.unsuitable {
    border-color: #ef4444;
    box-shadow:
    0 0 15px rgba(239,68,68,0.6);
}

.info-item {
    font-size: 18px;
    margin: 5px 0;
}

.info-label {
    font-weight: 700;
    display: inline-block;
    width: 90px;
}

.divider {
    height: 1px;
    background: rgba(128,128,128,0.3);
    margin: 10px 0;
}

.reference-title {
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 1.5px;
    margin-top: -5px;
    margin-bottom: -2px;
}

.reference-item {
    display: flex;
    justify-content: space-between;
    font-size: 14px;
    margin: 2px 0;
    opacity: 0.75;
}
</style>
""",
unsafe_allow_html=True
)

selected_id = 12
droplet_status = "suitable"
velocity = "0.03 grid/s"
charge = "6.24 × 10⁻¹⁹ C"
falltime = "3 s"

oil_density = "981 kg·m⁻³"
gravity = "9.78 m·s⁻²"
air_viscosity = "1.83×10⁻⁵ kg·m⁻¹·s⁻¹"
descent_distance = "1.60×10⁻³ m"
correction_constant = "8.22×10⁻³ m·Pa"
pressure = "1.0098×10⁵ Pa"
plate_distance = "5.00×10⁻³ m"

if selected_id is None:
    status_class = ""
    display_status = "Waiting"
elif droplet_status == "suitable":
    status_class = "suitable"
    display_status = "Suitable"
else:
    status_class = "unsuitable"
    display_status = "Unsuitable"

video_col, info_col = st.columns([7,3])
with video_col:
    live = st.session_state.live_feed

    if live is not None and live["process"].poll() is not None:
        # The pipeline process died on its own (camera unplugged, --blind
        # window closed with q, etc.) -- don't keep pointing the UI at a feed
        # that's no longer being fed.
        st.session_state.live_feed = None
        st.session_state.live_feed_error = "The live session ended."
        live = None

    if live is not None:
        # Cache-bust so a Streamlit rerun makes the browser open a fresh
        # connection to the MJPEG stream rather than reusing a stale one.
        feed_url = f"{live['url']}?t={int(time.time())}"
        st.markdown(
        f"""
        <div class="video-container">
            <img class="live-feed-img" src="{feed_url}" alt="Live microscopic feed">
        </div>

        <div class="feed-title">
            [ LIVE MICROSCOPIC VIEW ]
        </div>
        <div class="feed-status live">
            ● LIVE -- device {live['device']}
        </div>
        """,
        unsafe_allow_html=True
        )

    elif os.path.exists(video_path):
        with open(video_path, "rb") as f:
            video_bytes = f.read()

        video_base64 = base64.b64encode(video_bytes).decode()

        st.markdown(
        f"""
        <div class="video-container">
            <video 
                autoplay 
                muted 
                loop
                playsinline
                style="width:100%; height:100%; object-fit:cover;">
                <source src="data:video/mp4;base64,{video_base64}" type="video/mp4">
            </video>
        </div>

        <div class="feed-title">
            [ LIVE MICROSCOPIC VIEW ]
        </div>
        """,
        unsafe_allow_html=True
        )

    else:
        print(f"Video file '{video_path}' not found. Displaying placeholder instead.")
        st.markdown(
        """
        <div class="video-container">
            <div class="video-placeholder">
                MICROSCOPIC CAMERA FEED
            </div>
        </div>

        <div class="feed-title">
            [ LIVE MICROSCOPIC VIEW ]
        </div>
        """,
        unsafe_allow_html=True
        )

    if start_live is None:
        st.caption("Live camera controls unavailable: `wrapper` couldn't be imported here.")
    else:
        btn_col1, btn_col2, _ = st.columns([1, 1, 3])
        if live is None:
            if btn_col1.button("▶ Start live feed", use_container_width=True):
                st.session_state.live_feed_error = None
                with st.spinner("Running bench check and starting capture..."):
                    try:
                        st.session_state.live_feed = start_live()
                    except Exception as e:
                        st.session_state.live_feed_error = str(e)
                st.rerun()
        else:
            if btn_col1.button("■ Stop live feed", use_container_width=True):
                stop_live(live)
                st.session_state.live_feed = None
                st.rerun()

        if st.session_state.live_feed_error:
            st.markdown(
                f'<div class="feed-status error">{st.session_state.live_feed_error}</div>',
                unsafe_allow_html=True,
            )

with info_col:
    st.markdown(
    f"""
    <div class="info-panel">
        <div class="droplet-preview-box">
            <div class="droplet-info">
                <div class="droplet-title">
                    SELECTED DROPLET
                </div>
                <div class="droplet-id">
                    ID: #{selected_id if selected_id is not None else "--"}
                </div>
            </div>
            <div class="droplet-indicator">
                <div class="droplet-ring {status_class}">
                </div>
            </div>
        </div>
        <div class="info-item">
            <span class="info-label">STATUS</span>
            : {display_status}
        </div>
        <div class="info-item">
            <span class="info-label">FALL TIME</span>
            : {falltime}
        </div>
        <div class="info-item">
            <span class="info-label">VELOCITY</span>
            : {velocity}
        </div>
        <div class="divider"></div>
        <div class="reference-title">
            REFERENCE DATA
        </div>
        <div class="reference-item">
            <span>Oil Density</span>
            <span>{oil_density}</span>
        </div>
        <div class="reference-item">
            <span>Gravity</span>
            <span>{gravity}</span>
        </div>
        <div class="reference-item">
            <span>Air Viscosity</span>
            <span>{air_viscosity}</span>
        </div>
        <div class="reference-item">
            <span>Descent Distance</span>
            <span>{descent_distance}</span>
        </div>
        <div class="reference-item">
            <span>Correction Constant</span>
            <span>{correction_constant}</span>
        </div>
        <div class="reference-item">
            <span>Atmospheric Pressure</span>
            <span>{pressure}</span>
        </div>
        <div class="reference-item">
            <span>Plate Distance</span>
            <span>{plate_distance}</span>
        </div>
    </div>

    <div class="feed-title">
        [ DROPLET ANALYSIS ]
    </div>
    """,
    unsafe_allow_html=True
    )

# -------------------------
# Guide
# -------------------------

st.markdown(
"""
<style>

.guide-title {
    font-size: 28px;
    font-weight: 700;
    margin-top: 50px;
    margin-bottom: 10px;
}

/* Remove visible box */
[data-testid="stExpander"] details {
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    box-shadow: none !important;
}

/* Add transparent box */
[data-testid="stExpander"] {
    background: rgba(255, 255, 255, 0.15) !important;
    border: none !important;
    box-shadow: 0 8px 30px rgba(0,0,0,0.12); !important;
    border-radius: 0 !important;
}

/* Header */
[data-testid="stExpander"] details > summary {
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
    padding: 10px 4px !important;
}

/* Header text */
[data-testid="stExpander"] details > summary p {
    font-size: 18px !important;
    font-weight: 600 !important;
    margin: 0 !important;
}

/* Content */
[data-testid="stExpander"] details > div {
    background: rgba(128, 128, 128, 0.12) !important;
    border: none !important;
    box-shadow: none !important;
}

[data-testid="stExpander"] ul {
    margin-top: -12px !important;
}

[data-testid="stExpander"] ol {
    margin-top: -12px !important;
}

</style>
""",
unsafe_allow_html=True
)

st.markdown(
"""
<div class="guide-title">
    📖 GUIDE
</div>
""",
unsafe_allow_html=True
)

with st.expander("What Kind of Oil Droplet is Suitable?"):
    st.markdown("""
    A suitable oil droplet should be neither **too small** nor **too large**.
    > **TOO SMALL** - strongly affected by Brownian motion.  
    **TOO LARGE** - falls too quickly, increasing measurement error.

    A suitable oil droplet should:
    - Preferably carry fewer than **10 electrons**.
    - Have a falling speed of approximately **0.2–0.5 grid/s** at 0 V.
    - Have a balance voltage of approximately **150–250 V**.
    """)

with st.expander("Selecting a Suitable Oil Droplet"):
    st.markdown("""
    1. Press “0V/Work” and set the initial working voltage to approximately 150–250 V.
    2. Spray the oil droplets and adjust the focus until most droplets are visible.
    3. Wait approximately 10 seconds for the number of visible droplets to decrease.
    4. Select a droplet that rises slowly as a temporary target.
    5. Switch “0V/Work” so that the voltage between the plates becomes 0 V.
    6. Among the temporary target droplets, select one with a falling speed of 0.2–0.5 grid/s.
    7. Adjust the focus until the selected droplet appears small and bright.

    > **TIPS AND TRICK**  
    To screen droplets with an appropriate charge more quickly:  
    > 1. Set the initial working voltage to approximately **350–400 V**.  
    > 2. Droplets carrying more charge will rise and leave the field of view more quickly.  
    > 3. Select droplets with a slower rising speed as potential target droplets.  
    > 4. Gradually reduce the working voltage to **150–250 V** until the droplet's rising speed slows and reaches a balanced state.
    """)

with st.expander("Precautions"):
    st.markdown("""
    1. The mechanical position of the CCD box, screws, and camera lens cannot be changed, otherwise it will affect the image distance and imaging angle.
    2. Pay attention to adjusting the oil inlet switch to avoid the influence of external air flow on oil droplet measurement.
    3. There is high voltage in the instrument, so avoid touching the electrodes with your hands.
    4. Before the experiment, the two plates must be adjusted to be parallel to ensure that the electric field is vertical and parallel to gravity.
    5. It is recommended to select an oil droplet with a balance voltage of about 150-250V and a drop time of 16-40s for 1.6 mm. Such an oil droplt has a moderate charge and is suitable for experimental measurement.
    """)

# -------------------------
# AI Assistant
# -------------------------

# =========================================================
# AI ASSISTANT / POPOVER CSS
#
# IMPORTANT:
# Keep this CSS in ui.py, outside the popover.
#
# Streamlit renders st.markdown() inside a popover in a
# different DOM subtree. Injecting the chat <style> from
# ai_assist.py makes the CSS unreliable. The selectors below
# are therefore installed once at page level.
# =========================================================

st.markdown(
"""
<style>

/* =========================================================
   CHAT MESSAGE ANIMATION
   ========================================================= */

@keyframes chatFlowIn {
    from {
        opacity: 0;
        transform: translateY(14px);
    }

    to {
        opacity: 1;
        transform: translateY(0);
    }
}


/* =========================================================
   THINKING ANIMATION
   ========================================================= */

@keyframes thinking {
    0%, 60%, 100% {
        transform: translateY(0);
        opacity: 0.35;
    }

    30% {
        transform: translateY(-5px);
        opacity: 1;
    }
}


/* =========================================================
   CHAT MESSAGE
   ========================================================= */

.chat-message {
    display: flex;
    width: 100%;
    margin-bottom: 12px;
    padding: 0 2px;
    box-sizing: border-box;
}

.chat-message.chat-animate {
    animation: chatFlowIn 0.35s ease-out;
}


/* =========================================================
   USER MESSAGE
   ========================================================= */

.chat-message.user {
    justify-content: flex-end;
}

.chat-message.user .message-bubble {
    background: #6366f1;
    color: white;

    border-radius: 18px 18px 4px 18px;

    padding: 10px 15px;

    max-width: 75%;

    word-break: break-word;
    overflow-wrap: anywhere;
}


/* =========================================================
   ASSISTANT MESSAGE
   ========================================================= */

.chat-message.assistant {
    justify-content: flex-start;
}

.chat-message.assistant .message-bubble {
    background: rgba(128, 128, 128, 0.12);

    color: inherit;

    border: 1px solid rgba(128, 128, 128, 0.20);

    border-radius: 18px 18px 18px 4px;

    padding: 10px 15px;

    max-width: 75%;

    word-break: break-word;
    overflow-wrap: anywhere;

    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
}


/* =========================================================
   LATEX / MATHML
   ========================================================= */

.message-bubble math {
    color: inherit;
    font-size: 1.05em;
}

.message-bubble math[display="block"] {
    display: block;
    margin: 10px 0;
    text-align: center;
    overflow-x: auto;
    overflow-y: hidden;
}

.message-bubble .math-fallback {
    font-family: "SFMono-Regular", Consolas, monospace;
    opacity: 0.85;
}


/* =========================================================
   MARKDOWN (HEADERS / LISTS / INLINE CODE)
   ========================================================= */

.message-bubble h1,
.message-bubble h2,
.message-bubble h3,
.message-bubble h4,
.message-bubble h5,
.message-bubble h6 {
    margin: 10px 0 6px 0;
    line-height: 1.3;
    font-weight: 700;
}

.message-bubble h1 { font-size: 1.3em; }
.message-bubble h2 { font-size: 1.2em; }
.message-bubble h3 { font-size: 1.12em; }
.message-bubble h4,
.message-bubble h5,
.message-bubble h6 { font-size: 1.05em; }

.message-bubble ul,
.message-bubble ol {
    margin: 6px 0;
    padding-left: 20px;
}

.message-bubble li {
    margin: 3px 0;
}

.message-bubble code {
    background: rgba(128, 128, 128, 0.25);
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 0.9em;
    font-family: "SFMono-Regular", Consolas, monospace;
}


/* =========================================================
   AI ICON
   ========================================================= */

.ai-icon {
    width: 30px;
    height: 30px;

    border-radius: 50%;

    display: flex;
    align-items: center;
    justify-content: center;

    margin-right: 8px;

    background: rgba(99, 102, 241, 0.15);

    color: #6366f1;

    font-size: 14px;

    flex-shrink: 0;
}


/* =========================================================
   ASSISTANT ROW
   ========================================================= */

.assistant-row {
    display: flex;
    align-items: flex-start;
    width: 100%;
    min-width: 0;
}


/* =========================================================
   THINKING DOTS
   ========================================================= */

.thinking-dots {
    display: flex;
    align-items: center;
    gap: 4px;
    height: 20px;
}

.thinking-dot {
    width: 6px;
    height: 6px;

    border-radius: 50%;

    background: rgba(255, 255, 255, 0.7);

    animation: thinking 1.4s infinite ease-in-out;
}

.thinking-dot:nth-child(1) {
    animation-delay: 0s;
}

.thinking-dot:nth-child(2) {
    animation-delay: 0.2s;
}

.thinking-dot:nth-child(3) {
    animation-delay: 0.4s;
}


/* =========================================================
   POPOVER BUTTON
   ========================================================= */

div[data-testid="stPopover"] {
    position: fixed !important;

    right: 30px !important;
    bottom: 30px !important;

    width: 65px !important;
    height: 65px !important;

    margin: 0 !important;
    padding: 0 !important;

    z-index: 10000 !important;
}

div[data-testid="stPopover"] button {
    position: relative !important;

    width: 65px !important;
    height: 65px !important;

    min-width: 65px !important;
    min-height: 65px !important;

    max-width: 65px !important;
    max-height: 65px !important;

    margin: 0 !important;
    padding: 0 !important;

    border-radius: 50% !important;

    box-sizing: border-box !important;

    background: rgba(102, 104, 250, 0.80) !important;

    backdrop-filter: blur(15px) !important;
    -webkit-backdrop-filter: blur(15px) !important;

    border: 1px solid rgba(255, 255, 255, 0.20) !important;

    box-shadow:
        0 8px 30px rgba(0, 0, 0, 0.15) !important;

    display: flex !important;
    align-items: center !important;
    justify-content: center !important;

    font-size: 30px !important;
}

div[data-testid="stPopover"] button:hover {
    transform: scale(1.08) !important;

    border-radius: 50% !important;

    background: rgba(102, 104, 250, 0.90) !important;

    box-shadow:
        0 10px 35px rgba(99, 102, 241, 0.35) !important;
}

div[data-testid="stPopover"] button p {
    font-size: 30px !important;
    line-height: 1 !important;

    margin: 0 !important;
    padding: 0 !important;
}


/* =========================================================
   ASK AI MESSAGE
   ========================================================= */

.ai-message {
    position: fixed;

    right: 30px;
    bottom: 105px;

    z-index: 9999;

    background: rgba(230, 230, 230, 0.20);

    backdrop-filter: blur(15px);
    -webkit-backdrop-filter: blur(15px);

    border: 1px solid rgba(128, 128, 128, 0.25);

    border-radius: 18px;

    padding: 12px 18px;

    font-size: 15px;
    font-weight: 500;

    box-shadow:
        0 8px 30px rgba(0, 0, 0, 0.12);

    white-space: nowrap;

    animation: aiBubble 15s ease-in-out infinite;
}

.ai-sparkle {
    margin-right: 5px;
}

@keyframes aiBubble {
    0% {
        opacity: 0;
        transform: translateY(10px) scale(0.9);
    }

    8% {
        opacity: 1;
        transform: translateY(0) scale(1);
    }

    30% {
        opacity: 1;
        transform: translateY(0) scale(1);
    }

    38% {
        opacity: 0;
        transform: translateY(10px) scale(0.9);
    }

    100% {
        opacity: 0;
        transform: translateY(10px) scale(0.9);
    }
}


/* =========================================================
   POPOVER PANEL
   ========================================================= */

div[data-testid="stPopoverBody"] {
    width: 400px !important;
    height: 760px !important;

    padding: 0 !important;
    margin: 0 !important;

    box-sizing: border-box !important;

    border-radius: 22px !important;

    background: rgba(230, 230, 230, 0.20) !important;

    backdrop-filter: blur(20px) !important;
    -webkit-backdrop-filter: blur(20px) !important;

    border: 1px solid rgba(128, 128, 128, 0.25) !important;

    box-shadow:
        0 15px 50px rgba(0, 0, 0, 0.18) !important;

    overflow: hidden !important;
}


/* =========================================================
   POPOVER INTERNAL STREAMLIT CONTENT
   ========================================================= */

div[data-testid="stPopoverBody"] > div {
    width: 100% !important;
    max-width: none !important;

    box-sizing: border-box !important;
}

div[data-testid="stPopoverBody"] [data-testid="stVerticalBlock"] {
    width: 100% !important;
    max-width: none !important;

    box-sizing: border-box !important;
}


/* =========================================================
   AI HEADER
   ========================================================= */

.ai-panel-header {
    width: 100%;

    height: 70px;

    box-sizing: border-box;

    display: flex;
    align-items: center;

    padding: 0 20px;

    border-bottom:
        1px solid rgba(128, 128, 128, 0.20);
}

.ai-panel-title {
    display: flex;
    align-items: center;
    gap: 12px;
}

.ai-panel-icon {
    width: 38px;
    height: 38px;

    border-radius: 50%;

    display: flex;
    align-items: center;
    justify-content: center;

    background: rgba(99, 102, 241, 0.15);

    color: #6668FA;

    font-size: 18px;
}

.ai-title {
    font-size: 17px;
    font-weight: 700;
}

.ai-subtitle {
    font-size: 12px;
    opacity: 0.60;
    margin-top: 2px;
}


/* =========================================================
   CHAT CONTAINER
   ========================================================= */

/*
   This targets only the actual Streamlit height container
   created by st.container(height=390).
*/

div[data-testid="stPopoverBody"]
div[data-testid="stVerticalBlockBorderWrapper"] {
    width: 100% !important;
    max-width: none !important;

    box-sizing: border-box !important;
}


/*
   Header (70px) + chat history (360px, its own internal scroll,
   unchanged) + input row (~150-180px including the textarea, the
   counter/button row, and Streamlit's own widget spacing) comfortably
   fit inside this budget with a large safety margin, so nothing here
   ever needs to scroll or get clipped - the popover's overflow: hidden
   (set above) simply never has anything to hide. This also stops the
   inner wrapper Streamlit generates from picking up its own default
   overflow (the original cause of the header being scrollable).
*/

div[data-testid="stPopoverBody"] > div {
    overflow: hidden !important;
}

/* Streamlit's own "Press ⌘+Enter to apply" hint under the textarea -
   hidden because ai_assist.py implements its own Enter-to-send /
   Shift+Enter-for-newline behavior instead. */
div[data-testid="stPopoverBody"] [data-testid="InputInstructions"] {
    display: none !important;
}


/* =========================================================
   USER INPUT (custom text area replacing st.chat_input)
   ========================================================= */

/*
   st.chat_input doesn't expose a disabled state, a custom send
   button, or a live character counter, so the input row is a
   plain st.text_area + st.button instead. Keep it constrained
   to the panel instead of the page, fix its height, and stop
   the user from dragging it taller - once typed content exceeds
   that height the textarea gets its own native vertical
   scrollbar for free.
*/

div[data-testid="stPopoverBody"] textarea {
    width: calc(100% - 30px) !important;

    margin-left: 15px !important;
    margin-right: 15px !important;

    resize: none !important;

    font-size: 14px !important;
    line-height: 1.4 !important;
}

div[data-testid="stPopoverBody"] [data-testid="stTextArea"] {
    margin-left: 15px !important;
    margin-right: 15px !important;

    width: calc(100% - 30px) !important;
}


/* =========================================================
   CHARACTER COUNTER
   ========================================================= */

.ai-char-counter {
    font-size: 12px;

    /* Neutral gray-ish in both themes: dim the inherited text
       color with opacity instead of hardcoding a light-mode-only
       gray, matching the pattern already used elsewhere in this
       file (.ai-subtitle, .reference-item, etc). */
    color: currentColor;
    opacity: 0.55;

    text-align: right;
    padding-right: 4px;

    white-space: nowrap;
}

.ai-char-counter.over-limit {
    opacity: 1;
    color: #ef4444;
    font-weight: 700;
}


/* =========================================================
   SEND BUTTON
   ========================================================= */

div[data-testid="stPopoverBody"] [data-testid="stButton"] {
    margin-left: 15px !important;
    margin-right: 15px !important;
    margin-top: 8px !important;
}

div[data-testid="stPopoverBody"] [data-testid="stButton"] button {
    background: #6668FA !important;
    color: white !important;
    border: 1px solid #6668FA !important;
    border-radius: 10px !important;
    box-shadow: none !important;
    opacity: 1 !important;
}

div[data-testid="stPopoverBody"] [data-testid="stButton"] button:hover:not(:disabled) {
    background: rgba(102, 104, 250, 0.85) !important;
    border-color: rgba(102, 104, 250, 0.85) !important;
}

/* Gray-ish, per the spec: this affects ONLY the Send button itself,
   never any other element on the page. */
div[data-testid="stPopoverBody"] [data-testid="stButton"] button:disabled {
    background: rgba(128, 128, 128, 0.35) !important;
    border-color: rgba(128, 128, 128, 0.35) !important;
    color: rgba(255, 255, 255, 0.6) !important;
    opacity: 1 !important;
    cursor: not-allowed !important;
}

/* Non-interactive stand-in swapped into the Send button's placeholder
   the instant a query is accepted, before the slow Gemini call - see
   render_locked_send_button() in ai_assist.py. Styled to match the
   real button's disabled look exactly so there's no visible jump. */
.ai-send-btn-locked {
    display: flex;
    align-items: center;
    justify-content: center;

    width: 100%;
    box-sizing: border-box;

    padding: 0.5rem 0;

    border-radius: 10px;
    border: 1px solid rgba(128, 128, 128, 0.35);

    background: rgba(128, 128, 128, 0.35);
    color: rgba(255, 255, 255, 0.6);

    font-size: 14px;
    font-weight: 600;
    line-height: 1.4;

    cursor: not-allowed;
}


/* =========================================================
   MOBILE
   ========================================================= */

@media (max-width: 600px) {

    div[data-testid="stPopover"] {
        right: 15px !important;
        bottom: 15px !important;
    }

    .ai-message {
        right: 15px;
        bottom: 90px;
    }

    div[data-testid="stPopoverBody"] {
        width: calc(100vw - 30px) !important;
        height: 70vh !important;
    }

    .chat-message.user .message-bubble,
    .chat-message.assistant .message-bubble {
        max-width: 85%;
    }
}

</style>
""",
unsafe_allow_html=True,
)


# =========================================================
# ASK AI MESSAGE
# =========================================================

st.markdown(
"""
<div class="ai-message">
    <span class="ai-sparkle">✨</span>
    Ask AI about the experiment
</div>
""",
unsafe_allow_html=True,
)


# =========================================================
# AI POPOVER
# =========================================================

with st.popover("🛸", width=400):

    st.markdown(
                """
                <div class="ai-panel-header">
                    <div class="ai-panel-title">
                        <div class="ai-panel-icon">
                            ✦
                        </div>
                        <div>
                            <div class="ai-title">
                                AI Assistant
                            </div>
                            <div class="ai-subtitle">
                                Millikan Oil Drop Experiment
                            </div>
                        </div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True
                )

    render_ai_assistant()