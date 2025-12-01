# ------------------------------------------------------------------------------
# Refactor date: 2025-12-01
# Refactored by: Imaad Fakier
# Purpose: Ultimate Visual Transcripts Generator aligned to Coursera/Berkeley use case.
# ------------------------------------------------------------------------------
"""
Visual Transcripts Generator (v2)
---------------------------------
Streamlit entrypoint for OES' Visual Transcripts micro-app.

Design goals (from Marochelle + Christo huddles):
- SRT file remains REQUIRED as the timeline backbone (Berkeley workflow).
- Video is navigated in frame "steps" (e.g. every 50 / 200 frames) instead of every frame.
- Users can select and save specific frames for which they want visual transcripts.
- Each saved frame is:
    • time-stamped
    • aligned to the nearest SRT caption
    • editable directly IN-APP (no forced Word-only editing)
- GPT-4o Vision is used to assist with visual descriptions, respecting a word limit.
- Final output is a .docx "Visual Transcript" document ordered by timeline.

This file follows the same documentation and style standards as the
Alt-Text Generator refactor for consistency across OES GenAI micro-apps.
"""

import os
import io
import cv2
import base64
import tempfile
import hashlib
from collections import OrderedDict
from datetime import timedelta

import numpy as np  # noqa: F401 (reserved for future image ops / cropping)
import streamlit as st
from PIL import Image
from docx import Document
from dotenv import load_dotenv
from streamlit_cropper import st_cropper

# ------------------------------------------------------------------------------
# Page setup and environment loading
# ------------------------------------------------------------------------------
st.set_page_config(page_title="VT Generator", page_icon="🖼️", layout="wide")
load_dotenv()  # Loads .env file variables into environment

APP_TITLE = "Visual Transcripts Generator"
DEFAULT_FPS_FALLBACK = 30
SUPPORTED_VIDEO_EXTS = ["mp4"]
SUPPORTED_SRT_EXTS = ["srt"]
MODEL_NAME = os.getenv("VT_MODEL", "gpt-4o")  # Allow override via .env


# ------------------------------------------------------------------------------
# Authentication helpers
# ------------------------------------------------------------------------------
def sha256_hex(s: str) -> str:
    """Hash a provided access code using SHA-256. Used for secure auth comparison."""
    return hashlib.sha256(s.encode()).hexdigest()


ACCESS_CODE_HASH = os.getenv("ACCESS_CODE_HASH")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not ACCESS_CODE_HASH:
    st.error(
        "⚠️ Hashed access code not found. Please set ACCESS_CODE_HASH "
        "in your environment or Streamlit secrets."
    )
    st.stop()


# ------------------------------------------------------------------------------
# Session State initialization
# ------------------------------------------------------------------------------
def init_state() -> None:
    """Initialize session_state keys for consistent runtime behavior."""
    st.session_state.setdefault("authenticated", False)

    # Video / timing
    st.session_state.setdefault("video_path", None)
    st.session_state.setdefault("fps", DEFAULT_FPS_FALLBACK)
    st.session_state.setdefault("frame_count", 0)
    st.session_state.setdefault("frame_step", 50)  # how many frames to jump each step
    st.session_state.setdefault("frame_index", 0)
    st.session_state.setdefault("video_ready", False)

    # SRT + subtitles
    st.session_state.setdefault("subtitles", OrderedDict())

    # Saved annotations (each item is a dict: image, frame_index, seconds, timestamp, subtitle, visual_text)
    st.session_state.setdefault("annotations", [])

    # GPT settings
    st.session_state.setdefault("vt_word_limit", 80)


init_state()

# ------------------------------------------------------------------------------
# Access control gate
# ------------------------------------------------------------------------------
if not st.session_state.authenticated:
    st.title("🔒 Access Restricted")
    with st.form("auth_form", clear_on_submit=False):
        access_code_input = st.text_input("Enter Access Code:", type="password")
        submitted = st.form_submit_button("Submit")
    if submitted:
        if sha256_hex(access_code_input) == ACCESS_CODE_HASH:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect access code. Please try again.")
    st.stop()


# ------------------------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------------------------
def seconds_to_timestamp(seconds: float) -> str:
    """Convert seconds float to HH:MM:SS.mmm formatted timestamp."""
    td = timedelta(seconds=int(seconds))
    ms = int((seconds - int(seconds)) * 1000)
    return f"{str(td)}.{ms:03d}"


def parse_srt_bytes(srt_bytes: bytes) -> OrderedDict:
    """Parse SRT file bytes into an OrderedDict of {start_seconds: caption}.

    - Handles multi-line captions.
    - Normalizes commas to dots in timestamps.
    - Ensures time-ordered output.
    """
    text = srt_bytes.decode("utf-8", errors="ignore").replace("\r\n", "\n")
    blocks, block = [], []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            block.append(line)
        else:
            if block:
                blocks.append(block)
                block = []
    if block:
        blocks.append(block)

    parsed = []
    for b in blocks:
        if len(b) < 2:
            continue

        timing_line, text_lines = None, []
        for i, line in enumerate(b):
            if "-->" in line:
                timing_line = line
                text_lines = b[i + 1 :]
                break

        if not timing_line:
            continue

        start_str = timing_line.split("-->")[0].strip().replace(",", ".")
        parts = start_str.split(":")
        try:
            if len(parts) == 3:
                h, m, s = parts
                start_seconds = int(h) * 3600 + int(m) * 60 + float(s)
            else:
                m, s = parts
                start_seconds = int(m) * 60 + float(s)
        except Exception:
            continue

        caption = " ".join(t.strip() for t in text_lines if t.strip())
        parsed.append((start_seconds, caption))

    parsed.sort(key=lambda x: x[0])
    return OrderedDict(parsed)


def find_nearest_subtitle(seconds: float, subtitles: OrderedDict) -> str:
    """Return the subtitle text whose start time is <= seconds and closest to it.

    If no earlier subtitle exists, return 'No subtitle'.
    """
    if not subtitles:
        return "No subtitle"

    keys = list(subtitles.keys())
    # find rightmost key <= seconds
    candidate = None
    for k in keys:
        if k <= seconds:
            candidate = k
        else:
            break
    if candidate is None:
        return "No subtitle"
    return subtitles.get(candidate, "No subtitle")


def pil_to_base64_jpg(pil_img: Image.Image) -> str:
    """Convert PIL image to base64-encoded JPEG string for GPT-4o vision input."""
    buf = io.BytesIO()
    pil_img.save(buf, format="JPEG", quality=95)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


# ------------------------------------------------------------------------------
# OpenAI integration (SDK v1)
# ------------------------------------------------------------------------------
_openai_client = None


def get_openai_client():
    """Initialize and memoize OpenAI client using API key from environment."""
    global _openai_client
    if _openai_client is None:
        if not OPENAI_API_KEY:
            st.sidebar.error("Missing OPENAI_API_KEY in environment")
            raise RuntimeError("OPENAI_API_KEY missing")
        from openai import OpenAI  # type: ignore

        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def describe_image_with_gpt(
    pil_img: Image.Image,
    base_prompt: str,
    word_limit: int,
    max_tokens: int = 300,
) -> str:
    """Send an image to GPT-4o for description using vision API.

    Parameters:
        pil_img:     The frame captured from the video (or cropped region).
        base_prompt: Instructional prompt (accessibility-focused).
        word_limit:  Soft word cap communicated to the model.
        max_tokens:  Upper bound for token count.

    Returns:
        String content of GPT response.
    """
    base64_image = pil_to_base64_jpg(pil_img)
    client = get_openai_client()

    full_prompt = (
        f"{base_prompt}\n\n"
        f"IMPORTANT: Keep your response under approximately {word_limit} words. "
        f"Use clear, concise language suitable for a screen reader."
    )

    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": full_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                    },
                ],
            }
        ],
        max_tokens=max_tokens,
    )
    content = resp.choices[0].message.content or ""
    return content.strip()


# ------------------------------------------------------------------------------
# UI: Main layout and functional flow
# ------------------------------------------------------------------------------
st.title(APP_TITLE)
st.caption(
    "Refactored with SRT-first workflow, frame stepping, in-app editing, and GPT-4o vision."
)

# ------------------------------------------------------------------------------
# Global settings (frame step, word limit)
# ------------------------------------------------------------------------------
with st.sidebar.expander("⚙️ Settings", expanded=True):
    st.write("Tune how you navigate the video and how verbose GPT responses are.")

    st.session_state.frame_step = st.number_input(
        "Frame step (how many frames to skip between positions)",
        min_value=1,
        max_value=1000,
        value=int(st.session_state.frame_step),
        step=1,
    )

    st.session_state.vt_word_limit = st.slider(
        "Approximate word limit for each visual description",
        min_value=20,
        max_value=200,
        value=int(st.session_state.vt_word_limit),
        step=10,
    )

# ------------------------------------------------------------------------------
# Uploaders for video and SRT
# ------------------------------------------------------------------------------
col_u1, col_u2 = st.columns([2, 1])
with col_u1:
    video_file = st.file_uploader(
        "🎬 Upload Video File (MP4)", type=SUPPORTED_VIDEO_EXTS
    )
with col_u2:
    srt_file = st.file_uploader(
        "📝 Upload Subtitle File (SRT – required)", type=SUPPORTED_SRT_EXTS
    )

# Video preview and temporary storage
if video_file is not None:
    with st.expander("▶️ Click to preview uploaded video"):
        st.video(video_file)

    # Only rewrite temp file if this is a new upload (by name/size)
    temp_video_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
    with open(temp_video_path, "wb") as f:
        f.write(video_file.read())
    st.session_state.video_path = temp_video_path

# Process button (extract metadata and parse SRT)
if st.button("🚀 Process video + subtitles"):
    if video_file is None or srt_file is None:
        st.error("Please upload BOTH a video file and an SRT file before processing.")
    else:
        st.session_state["subtitles"] = parse_srt_bytes(srt_file.read())

        cap = cv2.VideoCapture(st.session_state.video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or DEFAULT_FPS_FALLBACK
        st.session_state["fps"] = int(round(fps))
        st.session_state["frame_count"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        cap.release()

        st.session_state["video_ready"] = True
        st.session_state["frame_index"] = 0
        st.session_state["annotations"] = []

        st.success(
            f"Processed video ({st.session_state.frame_count} frames @ "
            f"{st.session_state.fps} fps) and SRT subtitles."
        )

# ------------------------------------------------------------------------------
# Sidebar transcript listing (raw SRT view)
# ------------------------------------------------------------------------------
st.sidebar.subheader("📜 Raw Subtitle Timeline (SRT)")
if st.session_state["subtitles"]:
    for ts_sec, text in st.session_state["subtitles"].items():
        st.sidebar.write(f"**{seconds_to_timestamp(ts_sec)}**")
        st.sidebar.caption(text)
else:
    st.sidebar.info("Upload and process an SRT to view its timeline here.")

# ------------------------------------------------------------------------------
# Frame navigation and capture (main pane)
# ------------------------------------------------------------------------------
if st.session_state.get("video_ready", False) and st.session_state.video_path:
    total_frames = st.session_state["frame_count"]
    step = max(1, int(st.session_state["frame_step"]))
    max_step_index = max(0, (total_frames - 1) // step)

    st.markdown("### 🎞 Frame Navigation")

    col_nav_1, col_nav_2 = st.columns([3, 1])
    with col_nav_1:
        step_index = st.slider(
            "Select frame position (stepping by configured frame step)",
            min_value=0,
            max_value=max_step_index,
            value=st.session_state["frame_index"],
        )
    with col_nav_2:
        st.write(f"Total frames: `{total_frames}`")
        st.write(f"Frame step: `{step}`")

    # Convert "step index" back to actual frame number
    frame_number = step_index * step
    frame_number = min(frame_number, max(0, total_frames - 1))
    st.session_state["frame_index"] = step_index

    cap = cv2.VideoCapture(st.session_state["video_path"])
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
    ret, frame = cap.read()
    cap.release()

    current_pil_image = None

    if ret and frame is not None:
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        current_pil_image = Image.fromarray(frame_rgb)

        # Allow user to crop/select a region of the frame
        st.markdown("### Select region to use for visual transcript")

        cropped_img = st_cropper(
            current_pil_image,
            realtime_update=True,
            box_color="#FF0000",  # Red selection box
            aspect_ratio=None,  # Allows free-form selection
        )

        # Show the cropped output below the cropper
        st.image(cropped_img, caption="Cropped Region", use_column_width=True)

        # Use cropped image instead of full frame
        current_pil_image = cropped_img

        current_seconds = frame_number / max(st.session_state["fps"], 1)
        current_timestamp = seconds_to_timestamp(current_seconds)
        st.info(f"Timestamp: `{current_timestamp}`")

    else:
        st.warning("Could not read this frame. Try a different position.")

    # Navigation buttons
    nav_col1, nav_col2, nav_col3 = st.columns(3)
    with nav_col1:
        if st.button("⏮ Previous step"):
            st.session_state["frame_index"] = max(0, step_index - 1)
            st.rerun()
    with nav_col2:
        if st.button("⏭ Next step"):
            st.session_state["frame_index"] = min(max_step_index, step_index + 1)
            st.rerun()
    with nav_col3:
        if current_pil_image is not None and st.button("💾 Save this frame"):
            fps = st.session_state.get("fps", DEFAULT_FPS_FALLBACK)
            seconds = frame_number / max(fps, 1.0)
            timestamp = seconds_to_timestamp(seconds)
            subtitle_text = find_nearest_subtitle(
                seconds, st.session_state["subtitles"]
            )

            annotation = {
                "frame_index": frame_number,
                "seconds": seconds,
                "timestamp": timestamp,
                "subtitle": subtitle_text,
                "image": current_pil_image,  # ← this now includes the cropped region
                "visual_text": "",
            }

            st.session_state["annotations"].append(annotation)
            st.success(f"Saved frame {frame_number} at {timestamp} for annotation.")
            st.rerun()


# ------------------------------------------------------------------------------
# Sidebar: Saved frames + in-app editing + GPT vision assistance
# ------------------------------------------------------------------------------
# NOTE:
#   - This panel is placed directly in st.sidebar (NO expanders around it),
#     because sidebar expanders suppress vertical scrolling.
#   - Each annotation block (image + text + GPT assist) is displayed in order.
#   - All widgets receive stable session_state keys to avoid UI jitter.
# ------------------------------------------------------------------------------

with st.sidebar:  # Ensures proper scroll behavior
    st.subheader("🖼 Saved Frames & Visual Transcripts")

    # If no frames have been saved yet, show guidance text.
    if not st.session_state["annotations"]:
        st.info(
            "Use the main panel to navigate the video and click 'Save this frame' "
            "to start building your visual transcript."
        )

    else:
        # Base prompt for GPT-4o vision assistance
        base_prompt = (
            "You are helping create visual descriptions for a course's accessibility "
            "materials. Describe only the key visual elements and on-screen text that "
            "are important for understanding the learning content. Write in a neutral, "
            "descriptive tone suitable for screen readers."
        )

        # Loop through all saved frame annotations
        for i, ann in enumerate(st.session_state["annotations"]):
            st.markdown("---")

            # Display the saved frame image
            st.image(
                ann["image"],
                caption=f"Frame {ann['frame_index']} @ {ann['timestamp']}",
                use_column_width=True,
            )

            # Show nearest SRT subtitle if applicable
            if ann["subtitle"] and ann["subtitle"] != "No subtitle":
                st.caption(f"**SRT**: {ann['subtitle']}")
            else:
                st.caption("_No matching subtitle for this time._")

            # KEY for the text area — keeps user edits persistent across reruns
            text_key = f"vt_text_{i}"

            # Bootstrap session_state for text area (only once)
            if text_key not in st.session_state:
                st.session_state[text_key] = ann.get("visual_text", "")

            # Editable visual transcript text area
            st.write("Visual transcript (editable):")
            st.text_area(
                label="",
                key=text_key,
                height=120,
            )

            # Sync text area → annotation object
            ann["visual_text"] = st.session_state[text_key]

            # Two-column row: GPT Assist + Remove
            btn_cols = st.columns([1, 1])

            # ------------------------------------------------------------------
            # GPT-4o Vision Assistance for this frame
            # ------------------------------------------------------------------
            with btn_cols[0]:
                if st.button(f"✨ GPT assist #{i+1}", key=f"gpt_btn_{i}"):
                    try:
                        with st.spinner("Calling GPT-4o vision…"):
                            response = describe_image_with_gpt(
                                ann["image"],
                                base_prompt=base_prompt,
                                word_limit=int(st.session_state["vt_word_limit"]),
                            )
                        # Update UI + annotation
                        st.session_state[text_key] = response
                        ann["visual_text"] = response
                        st.success("Updated from GPT-4o.")
                    except Exception as e:
                        st.error(f"Error calling GPT: {e}")

            # ------------------------------------------------------------------
            # Remove this saved frame + its text field
            # ------------------------------------------------------------------
            with btn_cols[1]:
                if st.button(f"🗑 Remove #{i+1}", key=f"del_btn_{i}"):
                    # Remove annotation and its associated text area state
                    del st.session_state["annotations"][i]
                    if text_key in st.session_state:
                        del st.session_state[text_key]

                    st.warning(f"Removed frame #{i+1} from annotations.")
                    st.rerun()  # Refresh sidebar immediately to reflect change


# ------------------------------------------------------------------------------
# Export transcript to .docx
# ------------------------------------------------------------------------------
def build_docx_from_annotations(annotations: list) -> str:
    """Generate a .docx transcript preserving time order.

    For each saved frame, we include:
    - Timestamp
    - (Optional) SRT subtitle
    - Visual transcript text
    """
    # Sort by time just in case user saved out-of-order
    sorted_anns = sorted(annotations, key=lambda a: a["seconds"])

    doc = Document()
    doc.add_heading("Visual Transcript", level=1)

    if not sorted_anns:
        doc.add_paragraph("No visual annotations were captured.")
    else:
        for ann in sorted_anns:
            ts = ann["timestamp"]
            subtitle = ann.get("subtitle") or ""
            visual = ann.get("visual_text") or ""

            p = doc.add_paragraph()
            p.add_run(f"[{ts}]").bold = True
            if subtitle:
                doc.add_paragraph(f"SRT: {subtitle}")
            if visual:
                doc.add_paragraph(f"Visual: {visual}")
            doc.add_paragraph("")  # spacer

    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".docx").name
    doc.save(out_path)
    return out_path


st.sidebar.subheader("📥 Download")
if st.sidebar.button("Generate .docx transcript"):
    if not st.session_state["annotations"]:
        st.sidebar.info("Nothing to export yet – save at least one frame first.")
    else:
        path = build_docx_from_annotations(st.session_state["annotations"])
        with open(path, "rb") as fh:
            st.sidebar.download_button(
                "Download Visual Transcript (.docx)",
                data=fh,
                file_name="visual_transcript.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )

# ------------------------------------------------------------------------------
# Logout control
# ------------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.button(
    "Logout",
    on_click=lambda: st.session_state.update({"authenticated": False}),
    key="logout_button",
)
