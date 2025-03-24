import streamlit as st
import cv2
import numpy as np
import os
import tempfile
import base64
import requests
from PIL import Image
from docx import Document
from datetime import timedelta

# Set Streamlit theme
st.set_page_config(page_title="VT Generator", page_icon="🖼️", layout="wide")

# Sidebar setup
st.sidebar.title("Saved Frames & Transcripts")
st.session_state.setdefault("saved_frames", [])
st.session_state.setdefault("saved_subtitles", [])
st.session_state.setdefault("frame_index", 0)
st.session_state.setdefault("frame_subtitle_map", {})
st.session_state.setdefault("subtitles", {})

# Upload Video and SRT File
video_file = st.file_uploader("Upload Video File (MP4)", type=["mp4"])
srt_file = st.file_uploader("Upload Subtitle File (SRT)", type=["srt"])

# Function to parse SRT files
def parse_srt(file):
    subtitles = {}
    lines = file.read().decode("utf-8").split("\n")
    index, start_time = None, None
    for line in lines:
        line = line.strip()
        if line.isdigit():
            index = int(line)
        elif "-->" in line:
            start_time = line.split(" --> ")[0]
            start_time = sum(float(x) * 60 ** i for i, x in enumerate(reversed(start_time.replace(',', '.').split(':'))))
        elif line:
            if index is not None and start_time is not None:
                subtitles[start_time] = line
    return subtitles

# Convert seconds to timestamp format
def seconds_to_timestamp(seconds):
    td = timedelta(seconds=int(seconds))
    ms = int((seconds - int(seconds)) * 1000)
    return f"{str(td)}.{ms:03d}"

# Process video and SRT
if video_file and srt_file and st.button("Process Video & Transcript"):
    temp_video_path = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4").name
    with open(temp_video_path, "wb") as f:
        f.write(video_file.read())

    st.session_state["subtitles"] = parse_srt(srt_file)
    cap = cv2.VideoCapture(temp_video_path)
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    st.session_state["fps"] = fps  # Store FPS globally
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Extract frames every N frames
    frame_skip = 10
    st.session_state["frame_skip"] = frame_skip
    st.session_state["frames"] = []
    current_frame = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if current_frame % frame_skip == 0:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            st.session_state["frames"].append(Image.fromarray(frame_rgb))
        current_frame += 1
    cap.release()

    # Map subtitles to frames
    st.session_state["frame_subtitle_map"] = {
        int(start_time * (fps // frame_skip)): text
        for start_time, text in st.session_state["subtitles"].items()
    }

# Display transcript
st.sidebar.subheader("Transcript")
for timestamp, text in st.session_state["subtitles"].items():
    st.sidebar.write(f"**{timestamp}**: {text}")

# Frame Navigation
total_frames = len(st.session_state.get("frames", [])) - 1
if total_frames >= 0:
    frame_index = st.slider("Select Frame", 0, total_frames, st.session_state["frame_index"], key="frame_slider")
    st.session_state["frame_index"] = frame_index
    st.image(st.session_state["frames"][frame_index], caption=f"Frame {frame_index}")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("Previous Frame"):
            st.session_state["frame_index"] = max(0, frame_index - 1)
    with col2:
        if st.button("Next Frame"):
            st.session_state["frame_index"] = min(total_frames, frame_index + 1)
    if st.button("Save Index"):
        st.session_state["saved_frames"].append(st.session_state["frames"][frame_index])
        st.session_state["saved_subtitles"].append(
            st.session_state["frame_subtitle_map"].get(frame_index, "No Subtitle")
        )

# Show saved frames
for i, (frame, subtitle) in enumerate(zip(st.session_state["saved_frames"], st.session_state["saved_subtitles"])):
    st.sidebar.image(frame, caption=f"Saved Frame {i}")
    st.sidebar.write(subtitle)

# Function to encode image as base64
def encode_image(image):
    buffered = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    image.save(buffered, format="JPEG")
    with open(buffered.name, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")

# Transcription using OpenAI's API
if "transcriptions" not in st.session_state:
    st.session_state["transcriptions"] = {}

for i, (frame, subtitle) in enumerate(zip(st.session_state["saved_frames"], st.session_state["saved_subtitles"])):
    if st.sidebar.button(f"Transcribe Frame {i}"):
        st.sidebar.write(f"Processing transcription for Frame {i}...")

        GPT_API_KEY = os.getenv("OPENAI_API_KEY")
        if not GPT_API_KEY:
            st.sidebar.error("Missing OPENAI_API_KEY")
            break

        base64_image = encode_image(frame)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GPT_API_KEY}"
        }
        payload = {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "What’s in this image?"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                ]}
            ],
            "max_tokens": 300
        }

        response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
        try:
            gpt_response = response.json()
            transcription = gpt_response['choices'][0]['message']['content']
            st.sidebar.text_area(f"GPT Response for Frame {i}", transcription)
            st.session_state["transcriptions"][i] = transcription
        except Exception as e:
            st.sidebar.error("Failed to process GPT response")
            st.sidebar.write(response.text)

    # ✅ Insert GPT at real timestamp
    if i in st.session_state["transcriptions"]:
        if st.sidebar.button(f"Insert into Transcript {i}"):
            fps = st.session_state.get("fps", 30)
            frame_skip = st.session_state.get("frame_skip", 10)
            frame_index = st.session_state["saved_frames"].index(frame)
            seconds = frame_index * frame_skip / fps
            timestamp = seconds_to_timestamp(seconds)

            gpt_text = f"[GPT]: {st.session_state['transcriptions'][i]}"
            if timestamp in st.session_state["subtitles"]:
                st.session_state["subtitles"][timestamp] += f"\n{gpt_text}"
            else:
                st.session_state["subtitles"][timestamp] = gpt_text

            st.sidebar.success(f"Inserted GPT response at {timestamp}")

# Download full transcript
def download_transcript():
    doc = Document()
    doc.add_heading("Visual Transcript", level=1)
    for timestamp, text in st.session_state["subtitles"].items():
        doc.add_paragraph(f"{timestamp}: {text}")
    temp_doc_path = tempfile.NamedTemporaryFile(delete=False, suffix=".docx").name
    doc.save(temp_doc_path)
    with open(temp_doc_path, "rb") as doc_file:
        st.sidebar.download_button("Download Transcript", doc_file, file_name="visual_transcript.docx", mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

st.sidebar.subheader("Download Options")
download_transcript()
