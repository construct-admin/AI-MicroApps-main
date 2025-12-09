# ------------------------------------------------------------------------------
# Visual Transcripts Generator (v2.2) – Final Refactored + Patched Build
# ------------------------------------------------------------------------------
# Refactor date: 2025-12-09
# Refactored by: Imaad Fakier (OES)
#
# Summary of v2.2 improvements:
# - FIXED: Streamlit Cloud injecting unsupported HTTP proxy → GPT errors solved.
# - FIXED: GPT Assist now works every time (proxy blocking + httpx override).
# - NEW: Auto-generate visual transcript immediately after saving a frame.
# - FIXED: Sidebar scrollbar intermittently disappearing.
# - IMPROVED: Documentation, comments, UX consistency, and robustness.
# - MAINTAINS: All features from v2.1 including rectangle crop, full-frame mode,
#              combined audio+visual transcript export, and in-app editing.
#
# Purpose:
# This micro-application generates accessible visual transcripts aligned to
# Coursera/Berkeley workflows. It allows frame stepping, SRT alignment, vision
# model descriptions, and export to structured .docx.
#
# ------------------------------------------------------------------------------

import os
import io
import cv2
import base64
import tempfile
import hashlib
from collections import OrderedDict
from datetime import timedelta

import numpy as np  # noqa: F401
import streamlit as st
from PIL import Image
from docx import Document
from dotenv import load_dotenv
from streamlit_cropper import st_cropper

# NEW — required for the proxy override patch
import httpx
from openai import OpenAI

# ------------------------------------------------------------------------------
# PAGE SETUP + ENV VARIABLES
# ------------------------------------------------------------------------------
st.set_page_config(page_title="VT Generator", page_icon="🖼️", layout="wide")

# ------------------------------------------------------------------------------
# GLOBAL CSS PATCH: Fix Streamlit sidebar scroll disappearing
# ------------------------------------------------------------------------------
# This addresses the issue Marochelle noted where the sidebar scrolling becomes
# inaccessible after multiple reruns or resizing. We force it to always scroll.
st.markdown(
    """
<style>
/* Force sidebar scroll to always be visible in older Streamlit versions */
section[data-testid="stSidebar"] .css-1y4p8pa {
    overflow-y: scroll !important;
}

/* Streamlit ≥1.33 uses this layout structure */
section[data-testid="stSidebar"] > div:first-child {
    overflow-y: auto !important;
    height: 100vh !important;
}
</style>
""",
    unsafe_allow_html=True,
)

load_dotenv()

APP_TITLE = "Visual Transcripts Generator"
DEFAULT_FPS_FALLBACK = 30
SUPPORTED_VIDEO_EXTS = ["mp4"]
SUPPORTED_SRT_EXTS = ["srt"]
MAX_VIDEO_BYTES = 200 * 1024 * 1024  # 200MB limit
MODEL_NAME = os.getenv("VT_MODEL", "gpt-4o")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ACCESS_CODE_HASH = os.getenv("ACCESS_CODE_HASH")

if not ACCESS_CODE_HASH:
    st.error("⚠️ ACCESS_CODE_HASH missing — set this in Streamlit secrets or .env.")
    st.stop()


# ------------------------------------------------------------------------------
# AUTHENTICATION
# ------------------------------------------------------------------------------
def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def init_state():
    """Initialize all session state variables used across the application."""
    st.session_state.setdefault("authenticated", False)

    # Video state
    st.session_state.setdefault("video_path", None)
    st.session_state.setdefault("fps", DEFAULT_FPS_FALLBACK)
    st.session_state.setdefault("frame_count", 0)
    st.session_state.setdefault("frame_step", 50)  # Marochelle prefers 50
    st.session_state.setdefault("frame_index", 0)
    st.session_state.setdefault("video_ready", False)

    # SRT
    st.session_state.setdefault("subtitles", OrderedDict())

    # Saved annotation objects (each contains an image + metadata)
    st.session_state.setdefault("annotations", [])

    # GPT settings
    st.session_state.setdefault("vt_word_limit", 50)

    # Crop mode toggle
    st.session_state.setdefault("use_rectangle_crop", True)


init_state()

if not st.session_state.authenticated:
    st.title("🔒 Access Restricted")
    with st.form("auth_form"):
        code = st.text_input("Enter access code:", type="password")
        submit = st.form_submit_button("Submit")
        if submit:
            if sha256_hex(code) == ACCESS_CODE_HASH:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Incorrect access code.")
    st.stop()

# ------------------------------------------------------------------------------
# UTILITY FUNCTIONS
# ------------------------------------------------------------------------------


def seconds_to_timestamp(seconds: float) -> str:
    """Convert seconds → HH:MM:SS.mmm (as required by transcript guidelines)."""
    td = timedelta(seconds=int(seconds))
    ms = int((seconds - int(seconds)) * 1000)
    return f"{str(td)}.{ms:03d}"


def parse_srt_bytes(srt_bytes: bytes) -> OrderedDict:
    """Parse SRT into {start_seconds → caption} pairs."""
    text = srt_bytes.decode("utf-8", errors="ignore").replace("\r\n", "\n")
    blocks, block = [], []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            block.append(stripped)
        else:
            if block:
                blocks.append(block)
                block = []
    if block:
        blocks.append(block)

    parsed = []
    for b in blocks:
        timing_line = None
        for x in b:
            if "-->" in x:
                timing_line = x
                break
        if not timing_line:
            continue

        start_str = timing_line.split("-->")[0].strip().replace(",", ".")
        parts = start_str.split(":")

        try:
            if len(parts) == 3:
                h, m, s = parts
                secs = int(h) * 3600 + int(m) * 60 + float(s)
            else:
                m, s = parts
                secs = int(m) * 60 + float(s)
        except:
            continue

        caption = " ".join(line for line in b[b.index(timing_line) + 1 :] if line)
        parsed.append((secs, caption))

    parsed.sort(key=lambda x: x[0])
    return OrderedDict(parsed)


def pil_to_base64_jpg(img: Image.Image) -> str:
    """Convert PIL image → Base64 JPEG for GPT-4o Vision."""
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ------------------------------------------------------------------------------
# OPENAI CLIENT (WITH STREAMLIT PROXY BYPASS)
# ------------------------------------------------------------------------------
def get_openai_client():
    """
    Create a safe OpenAI client that *blocks Streamlit Cloud's proxy injection*.

    Without this, Streamlit passes `proxies={...}` to HTTPX, which the OpenAI SDK
    rejects ("unexpected argument: proxies").
    """
    if not OPENAI_API_KEY:
        st.error("Missing OPENAI_API_KEY.")
        raise RuntimeError("Missing OpenAI key.")

    # Critical: override Streamlit's proxy injection
    transport = httpx.HTTPTransport(proxy=None)
    http_client = httpx.Client(
        transport=transport,
        follow_redirects=True,
    )

    return OpenAI(api_key=OPENAI_API_KEY, http_client=http_client)


def describe_image_with_gpt(img: Image.Image, base_prompt: str, word_limit: int):
    """Call GPT-4o Vision to generate a concise description for the selected frame."""
    client = get_openai_client()
    base64_image = pil_to_base64_jpg(img)

    full_prompt = (
        f"{base_prompt}\n\n"
        f"IMPORTANT: Limit your response to ~{word_limit} words. "
        f"Use neutral, descriptive, accessibility-friendly language."
    )

    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": full_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            },
                        },
                    ],
                }
            ],
            max_tokens=300,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        st.error(f"GPT Assist failed: {e}")
        return ""


# ------------------------------------------------------------------------------
# UI – Header
# ------------------------------------------------------------------------------
st.title(APP_TITLE)
st.caption("Refactored with auto-GPT generation, crop modes, and improved stability.")

# ------------------------------------------------------------------------------
# SIDEBAR SETTINGS
# ------------------------------------------------------------------------------
with st.sidebar.expander("⚙️ Settings", expanded=True):

    st.session_state.frame_step = st.number_input(
        "Frame step (jump size)",
        min_value=1,
        max_value=1000,
        value=int(st.session_state.frame_step),
        help="A step of 50 means moving 50 frames per slider movement.",
    )

    st.session_state.vt_word_limit = st.slider(
        "Word limit per visual description",
        min_value=20,
        max_value=200,
        value=st.session_state.vt_word_limit,
    )

    st.session_state.use_rectangle_crop = st.checkbox(
        "Use rectangle crop mode",
        value=st.session_state.use_rectangle_crop,
        help="Disable to use full-frame mode (similar to previous VT demo).",
    )

# ------------------------------------------------------------------------------
# FILE UPLOADERS
# ------------------------------------------------------------------------------
col1, col2 = st.columns([2, 1])
video_file = col1.file_uploader("🎬 Upload video (MP4)", type=SUPPORTED_VIDEO_EXTS)
srt_file = col2.file_uploader("📝 Upload SRT file", type=SUPPORTED_SRT_EXTS)

if video_file:
    if video_file.size > MAX_VIDEO_BYTES:
        st.error("Video > 200MB — please compress and re-upload.")
        video_file = None
    else:
        with st.expander("▶️ Preview uploaded video"):
            st.video(video_file)

        # Save to temp file
        temp_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
        with open(temp_path, "wb") as f:
            f.write(video_file.read())
        st.session_state.video_path = temp_path

# ------------------------------------------------------------------------------
# PROCESS VIDEO + SUBTITLES
# ------------------------------------------------------------------------------
if st.button("🚀 Process video + subtitles"):
    if not video_file or not srt_file:
        st.error("Upload BOTH video and SRT.")
    else:
        st.session_state.subtitles = parse_srt_bytes(srt_file.read())

        cap = cv2.VideoCapture(st.session_state.video_path)
        st.session_state.fps = int(cap.get(cv2.CAP_PROP_FPS) or DEFAULT_FPS_FALLBACK)
        st.session_state.frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.release()

        st.session_state.video_ready = True
        st.session_state.frame_index = 0
        st.session_state.annotations = []

        st.success("Video + SRT processed successfully.")

# ------------------------------------------------------------------------------
# SIDEBAR — RAW SRT VIEW
# ------------------------------------------------------------------------------
st.sidebar.subheader("📜 SRT Timeline")
subs = st.session_state.subtitles
if subs:
    for start_sec, text in subs.items():
        st.sidebar.write(f"**{seconds_to_timestamp(start_sec)}**")
        st.sidebar.caption(text)
else:
    st.sidebar.info("Nothing loaded yet.")

# ------------------------------------------------------------------------------
# MAIN PANEL: FRAME NAVIGATION
# ------------------------------------------------------------------------------
if st.session_state.video_ready and st.session_state.video_path:

    total = st.session_state.frame_count
    step = max(1, int(st.session_state.frame_step))
    max_idx = max(0, (total - 1) // step)

    st.markdown("### 🎞 Frame Navigation")
    c1, c2 = st.columns([3, 1])

    with c1:
        idx = st.slider(
            "Frame index (in step units)",
            min_value=0,
            max_value=max_idx,
            value=st.session_state.frame_index,
        )
    with c2:
        st.write(f"Total frames: `{total}`")
        st.write(f"Step: `{step}`")

    st.session_state.frame_index = idx
    frame_num = min(idx * step, total - 1)

    cap = cv2.VideoCapture(st.session_state.video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
    ret, frame = cap.read()
    cap.release()

    if not ret:
        st.warning("Could not read frame.")
    else:
        pil_frame = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

        if st.session_state.use_rectangle_crop:
            st.markdown("### Select crop region")
            crop = st_cropper(
                pil_frame,
                realtime_update=True,
                box_color="#FF0000",
                aspect_ratio=None,
            )
            st.image(crop, caption="Cropped output", use_column_width=True)
            selected_img = crop
        else:
            st.markdown("### Full frame selected")
            st.image(pil_frame, use_column_width=True)
            selected_img = pil_frame

        seconds = frame_num / st.session_state.fps
        timestamp = seconds_to_timestamp(seconds)
        st.info(f"Timestamp: `{timestamp}`")

        # ---------------------------------
        # NAV BUTTONS
        # ---------------------------------
        nc1, nc2, nc3 = st.columns(3)
        with nc1:
            if st.button("⏮ Previous"):
                st.session_state.frame_index = max(0, idx - 1)
                st.rerun()
        with nc2:
            if st.button("⏭ Next"):
                st.session_state.frame_index = min(max_idx, idx + 1)
                st.rerun()

        # ---------------------------------
        # SAVE FRAME (WITH AUTO-GPT)
        # ---------------------------------
        with nc3:
            if st.button("💾 Save this frame"):

                subtitles = st.session_state.subtitles
                subtitle_text = "No subtitle"
                subtitle_start = None

                if subtitles:
                    for key in subtitles.keys():
                        if key <= seconds:
                            subtitle_start = key
                            subtitle_text = subtitles[key]
                        else:
                            break

                ann = {
                    "frame_index": frame_num,
                    "seconds": seconds,
                    "timestamp": timestamp,
                    "subtitle": subtitle_text,
                    "subtitle_start": subtitle_start,
                    "image": selected_img,
                    "visual_text": "",
                }

                st.session_state.annotations.append(ann)
                ann_idx = len(st.session_state.annotations) - 1
                ann_key = f"vt_text_{ann_idx}"

                # ---- AUTO-GPT GENERATION (NEW) ----
                try:
                    with st.spinner("Generating visual description..."):
                        base_prompt = (
                            "You are describing learning content for accessibility. "
                            "Focus on key visual details only. Neutral tone."
                        )
                        text = describe_image_with_gpt(
                            selected_img,
                            base_prompt,
                            st.session_state.vt_word_limit,
                        )
                        st.session_state.annotations[ann_idx]["visual_text"] = text
                        st.session_state[ann_key] = text
                except Exception as e:
                    st.error(f"Auto-generation failed: {e}")

                st.success(f"Saved + auto-generated description for frame {frame_num}.")
                st.rerun()

# ------------------------------------------------------------------------------
# SIDEBAR — ANNOTATIONS PANEL
# ------------------------------------------------------------------------------
with st.sidebar:
    st.subheader("🖼 Saved Frames")

    if not st.session_state.annotations:
        st.info("Save frames to begin building your transcript.")
    else:
        base_prompt = (
            "Create a concise visual description for accessibility: "
            "neutral tone, key visual elements only."
        )

        for i, ann in enumerate(st.session_state.annotations):
            st.markdown("---")
            st.image(ann["image"], caption=f"{ann['timestamp']}", use_column_width=True)
            if ann["subtitle"] and ann["subtitle"] != "No subtitle":
                st.caption(f"SRT: {ann['subtitle']}")

            key = f"vt_text_{i}"
            if key not in st.session_state:
                st.session_state[key] = ann["visual_text"]

            st.text_area("Description:", key=key, height=120)
            ann["visual_text"] = st.session_state[key]

            c1, c2 = st.columns(2)
            with c1:
                if st.button(f"✨ GPT Assist #{i+1}", key=f"assist_{i}"):
                    try:
                        with st.spinner("Calling GPT..."):
                            resp = describe_image_with_gpt(
                                ann["image"],
                                base_prompt,
                                st.session_state.vt_word_limit,
                            )
                            st.session_state[key] = resp
                            ann["visual_text"] = resp
                            st.success("Updated.")
                    except Exception as e:
                        st.error(f"GPT Assist failed: {e}")
            with c2:
                if st.button(f"🗑 Remove #{i+1}", key=f"del_{i}"):
                    del st.session_state.annotations[i]
                    if key in st.session_state:
                        del st.session_state[key]
                    st.rerun()


# ------------------------------------------------------------------------------
# EXPORT DOCX – COMBINED AUDIO + VISUAL
# ------------------------------------------------------------------------------
def build_docx(annotations, subtitles):
    """Generate combined audio + visual transcript in timeline order."""
    doc = Document()
    doc.add_heading("Combined Visual and Audio Transcript", level=1)
    doc.add_paragraph("Timestamps shown in HH:MM:SS.mmm format.")

    visuals_by_start = {}
    for ann in annotations:
        visuals_by_start.setdefault(ann["subtitle_start"], []).append(ann)

    for start_sec, caption in subtitles.items():
        ts = seconds_to_timestamp(start_sec)

        p = doc.add_paragraph()
        p.add_run(f"[{ts}] ").bold = True
        p.add_run(caption)

        if start_sec in visuals_by_start:
            for ann in visuals_by_start[start_sec]:
                v = ann["visual_text"].strip()
                if v:
                    vp = doc.add_paragraph()
                    vp.add_run("Visual: ").bold = True
                    vp.add_run(v)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx").name
    doc.save(tmp)
    return tmp


st.sidebar.subheader("📥 Download")
if st.sidebar.button("Generate transcript (.docx)"):
    path = build_docx(st.session_state.annotations, st.session_state.subtitles)
    with open(path, "rb") as f:
        st.sidebar.download_button(
            "Download combined transcript",
            f,
            file_name="visual_transcript.docx",
        )

# ------------------------------------------------------------------------------
# LOGOUT
# ------------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.button(
    "Logout", on_click=lambda: st.session_state.update({"authenticated": False})
)
