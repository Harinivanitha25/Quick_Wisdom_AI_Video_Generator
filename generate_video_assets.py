import os
import json
import time
import random
import asyncio
import urllib.parse

import requests
import edge_tts
from faster_whisper import WhisperModel

from PIL import Image, ImageOps
from config import GEMINI_API_KEY, POLLINATION_API_KEY
from build_full_video import slugify

# Every project gets its own folder inside this one (see create_project_dir),
# so a new project can never overwrite an earlier project's files.
PROJECTS_ROOT = "project_output"


def create_project_dir(title, root=PROJECTS_ROOT):
    """Make a fresh folder for one project and return its path, e.g.
    project_output/2026-09-20_143205_quiet_susan_cain. The timestamp keeps
    the name unique (and sorted by creation time), the slug keeps it readable.
    Inside: images/, audio/, scenes/ (rendered clips), then project.json and
    the final video once they're made. No ':' in the name on purpose - ffmpeg's
    subtitle filter chokes on colons in paths."""
    base = f"{root}/{time.strftime('%Y-%m-%d_%H%M%S')}_{slugify(title, max_length=40)}"
    project_dir = base
    suffix = 2
    while os.path.exists(project_dir):
        project_dir = f"{base}_{suffix}"
        suffix += 1
    for sub in ("images", "audio", "scenes"):
        os.makedirs(f"{project_dir}/{sub}")
    return project_dir

GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={GEMINI_API_KEY}"
)

# Status codes worth retrying - temporary/overload issues, not something
# a retry-with-different-input would fix (that'd be a 400, which we don't
# retry).
RETRYABLE_STATUSES = {"UNAVAILABLE", "RESOURCE_EXHAUSTED", "INTERNAL"}

# How long to wait for a single Pollinations request before giving up on it,
# and how many times to retry a request that fails with a connection error,
# timeout, or a 5xx from the server - the same kind of transient failure
# call_gemini() already retries for Gemini.
IMAGE_REQUEST_TIMEOUT = 60
IMAGE_MAX_RETRIES = 4
IMAGE_RETRY_BASE_DELAY = 2


def call_gemini(payload, max_retries=3, base_delay=2):
    """POST to Gemini, automatically retrying with exponential backoff if
    the server reports a temporary overload (503) or similar retryable
    error, instead of failing on the first busy moment."""
    for attempt in range(max_retries):
        response = requests.post(GEMINI_URL, json=payload)
        data = response.json()

        if "error" not in data:
            return data

        status = data["error"].get("status")
        is_last_attempt = attempt == max_retries - 1

        if status in RETRYABLE_STATUSES and not is_last_attempt:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"  Gemini busy ({status}), retrying in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries})...")
            time.sleep(delay)
            continue

        return data  # non-retryable error, or out of retries - let caller handle it


FRAME_SIZES = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
}

# Loaded once and reused for every scene, rather than reloading per scene.
whisper_model = WhisperModel("small", device="cpu", compute_type="int8")


def generate_script(topic, duration_seconds):
    words_needed = int(duration_seconds / 60 * 150)  # ~150 words per minute of narration
    num_scenes = max(2, duration_seconds // 6)  # roughly one scene per 5 seconds

    print(f"Generating script (~{words_needed} words, {num_scenes} scenes)...")

    prompt = f"""You are an expert YouTube Shorts and video marketing specialist in 2026, known for writing scripts that hook viewers instantly, hold retention, and drive channel growth.

Write a script for a YouTube video about: {topic}
Target length: about {words_needed} words total, split into exactly {num_scenes} scenes.
Follow these rules for a high-retention, growth-focused script:

1. HOOK (scene 1): Open with a surprising fact, bold claim, or question that creates a curiosity gap in the first sentence. No greetings, no "in this video" - start mid-thought, like you're already mid-story.
2. PACING: Use short, punchy sentences written for spoken narration, not for reading. Avoid complex clauses - this will be read aloud by text-to-speech and shown as on-screen captions.
3. RETENTION: Each scene should end on a small cliffhanger or lead naturally into the next, so the viewer keeps watching rather than the pacing feeling like a list of disconnected facts.
4. VALUE: Prioritize genuinely known information over generic statements. Specific numbers, comparisons, and concrete details outperform vague claims.
5. LAST SCENE: Add a natural call-to-action scene that says "Follow Quick Wisdom for more"
6. TONE: Confident, conversational, and energetic - like a knowledgeable friend sharing something fascinating.

For each scene's image_prompt, describe an AI image generator prompt that visually matches that exact scene's narration - not a generic illustration. Include: what's literally being described in the narration, a visual style (cinematic, photorealistic, editorial, etc.), composition/framing, lighting, and mood that reinforces the emotional beat of that line.

Return ONLY valid JSON in this exact format, no other text:
{{
  "title": "a short, curiosity-driven video title under 50 characters",
  "scenes": [
    {{"narration": "text to be spoken for this scene", "image_prompt": "description for an AI image generator with style, composition, lighting, and mood."}}
  ]
}}"""

    data = call_gemini({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "low"},
            "responseMimeType": "application/json"
        }
    })

    if "error" in data:
        print("Gemini API error:", data["error"])
        # Stashed on the function itself so callers (like the UI) can show
        # the real reason without changing generate_script's return type,
        # which other code still expects to be "a script dict, or None".
        generate_script.last_error = data["error"]
        return None

    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    script = json.loads(raw_text)
    print(f"Script generated: {script['title']} ({len(script['scenes'])} scenes)")
    return script


generate_script.last_error = None


def generate_youtube_metadata(script):
    """Generate a YouTube title (with hashtags, <=100 chars), a description
    (~50 words + 6 hashtags), and 10 tags/keywords - based on the actual
    generated script, not just the original topic, so metadata reflects
    what's really in the video."""
    print("Generating YouTube metadata...")
    full_narration = " ".join(scene["narration"] for scene in script["scenes"])

    prompt = f"""You are a YouTube SEO and growth specialist in 2026.

Here is the script for a video titled "{script['title']}":
{full_narration}

Generate YouTube upload metadata for this video, following YouTube's current best practices:

1. TITLE: A catchy, hook-driven title followed by 5-6 relevant hashtags (must include #quickwisdom #ai #shorts) viewers would actually search for. The ENTIRE title including hashtags must be 100 characters or fewer - count carefully.
2. DESCRIPTION: About 50 words summarizing the video's hook and value (not just restating the title), followed by 6 relevant hashtags on their own line.
3. TAGS: A list of exactly 10 relevant keywords/phrases (not hashtags - plain search terms) that describe the video's topic, for YouTube's tags field.

Return ONLY valid JSON in this exact format, no other text:
{{
  "youtube_title": "title with hashtags, 100 chars or fewer total, no capital letter, only small letter eg: #quickwisdom",
  "description": "~50 word description ending with 6 hashtags (no capital letter, only small letter eg: #quickwisdom) on their own line",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6", "tag7", "tag8", "tag9", "tag10"]
}}"""

    data = call_gemini({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "low"},
            "responseMimeType": "application/json"
        }
    })

    if "error" in data:
        print("Gemini API error (metadata):", data["error"])
        return None

    raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
    metadata = json.loads(raw_text)

    # Safety check: warn (don't crash) if the title still ran over 100 chars.
    if len(metadata.get("youtube_title", "")) > 100:
        print(f"  Warning: generated title is {len(metadata['youtube_title'])} chars, over the 100 limit.")

    print(f"YouTube metadata generated: {metadata.get('youtube_title', '')}")
    return metadata


def generate_image(prompt, filename, width, height,
                   max_retries=IMAGE_MAX_RETRIES, base_delay=IMAGE_RETRY_BASE_DELAY):
    """Fetch one image from Pollinations, retrying on connection resets,
    timeouts, and 5xx errors (all transient - a busy or momentarily
    unreachable server, not a bad prompt), with the same kind of
    exponential backoff call_gemini() uses. A 4xx/bad prompt isn't
    retried, since asking again won't change the result."""
    encoded = urllib.parse.quote(prompt)
    url = f"https://gen.pollinations.ai/image/{encoded}"
    params = {"width": width, "height": height, "model": "flux"}
    headers = {"Authorization": f"Bearer {POLLINATION_API_KEY}"}

    for attempt in range(max_retries):
        is_last_attempt = attempt == max_retries - 1

        try:
            response = requests.get(url, params=params, headers=headers, timeout=IMAGE_REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as ex:
            if is_last_attempt:
                print(f"  Image generation failed for prompt: {prompt[:50]}...")
                print(f"  Connection error: {ex}")
                return False
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"  Image request failed ({type(ex).__name__}), retrying in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries})...")
            time.sleep(delay)
            continue

        if response.status_code >= 500 and not is_last_attempt:
            delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
            print(f"  Image server busy ({response.status_code}), retrying in {delay:.1f}s "
                  f"(attempt {attempt + 1}/{max_retries})...")
            time.sleep(delay)
            continue

        if response.status_code != 200 or "image" not in response.headers.get("Content-Type", ""):
            print(f"  Image generation failed for prompt: {prompt[:50]}...")
            print("  Status:", response.status_code, "Content-Type:", response.headers.get("Content-Type"))
            return False

        break  # got a real 200 image response

    with open(filename, "wb") as f:
        f.write(response.content)

    # Force the generated image to the exact target aspect ratio.
    with Image.open(filename) as img:
        img = img.convert("RGB")
        img = ImageOps.fit(
            img,
            (width, height),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
        img.save(filename, format="PNG")
    return True


async def generate_voice(text, filename, voice="en-US-AvaMultilingualNeural"):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(filename)


def generate_captions(audio_path):
    segments, _ = whisper_model.transcribe(audio_path, word_timestamps=True)
    words = []
    for segment in segments:
        for word in segment.words:
            words.append({"start": word.start, "end": word.end, "word": word.word})
    return words


def process_script(script, frame_size="9:16", youtube_metadata=None, project_dir=None):
    """Generate images, voices and captions for every scene, saving them
    (and project.json) inside project_dir. If no folder is given, a new one
    is created, so separate runs never share or overwrite files."""
    width, height = FRAME_SIZES[frame_size]

    if project_dir is None:
        project_dir = create_project_dir(script["title"])
    os.makedirs(f"{project_dir}/images", exist_ok=True)
    os.makedirs(f"{project_dir}/audio", exist_ok=True)
    print(f"Project folder: {project_dir}")

    project = {
        "title": script["title"],
        "frame_size": frame_size,
        "video_width": width,
        "video_height": height,
        "youtube_metadata": youtube_metadata,
        "scenes": [],
    }
    total = len(script["scenes"])

    for i, scene in enumerate(script["scenes"]):
        print(f"Processing scene {i + 1}/{total}...")

        image_path = f"{project_dir}/images/scene_{i}.png"
        audio_path = f"{project_dir}/audio/scene_{i}.mp3"

        print("  Generating image...")
        ok = generate_image(scene["image_prompt"], image_path, width, height)
        if ok:
            print("  Image generated")
        else:
            image_path = None  # generate_image already printed why it failed

        print("  Generating voice...")
        asyncio.run(generate_voice(scene["narration"], audio_path))
        print("  Voice generated")

        print("  Generating captions...")
        captions = generate_captions(audio_path)
        print(f"  Captions generated ({len(captions)} words)")

        project["scenes"].append({
            "narration": scene["narration"],
            "image_prompt": scene["image_prompt"],
            "image_path": image_path,
            "audio_path": audio_path,
            "captions": captions,
        })

        # Be polite to Pollinations' rate limit between image calls.
        if i < total - 1:
            print("  Waiting 6s before the next image (rate limit)...")
            time.sleep(6)

    with open(f"{project_dir}/project.json", "w") as f:
        json.dump(project, f, indent=2)

    print("\nDone! Project saved to", f"{project_dir}/project.json")
    return project


if __name__ == "__main__":
    topic = "storyline of chinese drama 'The Early Spring' 2026"
    duration_seconds = 30  # shorts - 35 sec, video - 75
    frame_size = "9:16"  # or "16:9"

    print(f"Generating script for: {topic} ({duration_seconds}s, {frame_size})")
    script = generate_script(topic, duration_seconds)
    print(script)

    if script is None:
        print("Script generation failed, stopping.")
    else:
        print("Script generated:", script["title"])
    
        print("Generating YouTube metadata...")
        youtube_metadata = generate_youtube_metadata(script)
        if youtube_metadata:
            print("Title:", youtube_metadata["youtube_title"])
            print("Description:", youtube_metadata["description"])
            print("Tags:", youtube_metadata["tags"])

        process_script(script, frame_size=frame_size, youtube_metadata=youtube_metadata)