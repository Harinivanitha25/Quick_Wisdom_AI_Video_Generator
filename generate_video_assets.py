import os
import json
import time
import asyncio
import urllib.parse

import requests
import edge_tts
from faster_whisper import WhisperModel

from config import GEMINI_API_KEY, POLLINATION_API_KEY

PROJECT_DIR = "project_output"
os.makedirs(f"{PROJECT_DIR}/images", exist_ok=True)
os.makedirs(f"{PROJECT_DIR}/audio", exist_ok=True)

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"
)

# Loaded once and reused for every scene, rather than reloading per scene.
whisper_model = WhisperModel("small", device="cpu", compute_type="int8")


def generate_script(topic, duration_seconds):
    words_needed = int(duration_seconds / 60 * 150)  # ~150 words per minute of narration
    num_scenes = max(3, duration_seconds // 5)  # roughly one scene per 8 seconds

    prompt = f"""Write a YouTube video script about: {topic}
                 Target length: about {words_needed} words total, split into {num_scenes} scenes. Add "Follow Quick Wisdom for more" as last scene. 
                 Return ONLY valid JSON in this exact format, no other text:
{{
  "title": "video title",
  "scenes": [
    {{"narration": "text to be spoken for this scene", "image_prompt": "description for an AI image generator with style, composition, lighting, and mood."}}
  ]
}}"""

    response = requests.post(GEMINI_URL, json={
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "low"},
            "responseMimeType": "application/json"
        }
    })

    data = response.json()
    if "error" in data:
        print("Gemini API error:", data["error"])
        return None

    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw_text)


def generate_image(prompt, filename):
    encoded = urllib.parse.quote(prompt)
    url = f"https://gen.pollinations.ai/image/{encoded}"
    params = {"width": 720, "height": 1280, "model": "flux"}
    headers = {"Authorization": f"Bearer {POLLINATION_API_KEY}"}

    response = requests.get(url, params=params, headers=headers)

    if response.status_code != 200 or "image" not in response.headers.get("Content-Type", ""):
        print(f"  Image generation failed for prompt: {prompt[:50]}...")
        print("  Status:", response.status_code, "Content-Type:", response.headers.get("Content-Type"))
        return False

    with open(filename, "wb") as f:
        f.write(response.content)
    return True


async def generate_voice(text, filename, voice="en-IN-NeerjaNeural"):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(filename)


def generate_captions(audio_path):
    segments, _ = whisper_model.transcribe(audio_path, word_timestamps=True)
    words = []
    for segment in segments:
        for word in segment.words:
            words.append({"start": word.start, "end": word.end, "word": word.word})
    return words


def process_script(script):
    project = {"title": script["title"], "scenes": []}
    total = len(script["scenes"])

    for i, scene in enumerate(script["scenes"]):
        print(f"Processing scene {i + 1}/{total}...")

        image_path = f"{PROJECT_DIR}/images/scene_{i}.png"
        audio_path = f"{PROJECT_DIR}/audio/scene_{i}.mp3"

        ok = generate_image(scene["image_prompt"], image_path)
        if not ok:
            image_path = None

        asyncio.run(generate_voice(scene["narration"], audio_path))
        captions = generate_captions(audio_path)

        project["scenes"].append({
            "narration": scene["narration"],
            "image_prompt": scene["image_prompt"],
            "image_path": image_path,
            "audio_path": audio_path,
            "captions": captions,
        })

        # Be polite to Pollinations' rate limit between image calls.
        if i < total - 1:
            time.sleep(6)

    with open(f"{PROJECT_DIR}/project.json", "w") as f:
        json.dump(project, f, indent=2)

    print("\nDone! Project saved to", f"{PROJECT_DIR}/project.json")
    return project


if __name__ == "__main__":
    topic = "UN adopts new world map"
    duration_seconds = 30

    print(f"Generating script for: {topic} ({duration_seconds}s)")
    script = generate_script(topic, duration_seconds)

    if script is None:
        print("Script generation failed, stopping.")
    else:
        print("Script generated:", script["title"])
        process_script(script)