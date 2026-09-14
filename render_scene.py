import json
import re
import string
import subprocess

# Common short/function words that should never become the "hero" word,
# even if nothing else in the group qualifies.
STOPWORDS = {
    "i'm", "i", "a", "an", "the", "is", "are", "was", "were", "to", "of",
    "in", "on", "for", "and", "or", "but", "it's", "that", "this", "my",
    "your", "old", "so", "as", "at", "be", "do", "did", "has", "have",
}

# The font/margin numbers below (100, 130, 120, 420) were tuned by eye for
# a video at this height. Every size scales proportionally from here, so
# any other resolution (a different vertical size, or landscape 16:9)
# automatically gets correctly-proportioned text instead of needing its
# own separate hardcoded numbers.
REFERENCE_HEIGHT = 1920
MIN_SCALE = 0.80

BASE_FONTSIZE = 100
BASE_HERO_FONTSIZE = 130
BASE_NUMBER_FONTSIZE = 100
BASE_MARGIN_LR = 120
BASE_MARGIN_V = 420


def seconds_to_ass_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:d}:{minutes:02d}:{secs:05.2f}"


def split_into_groups(captions, max_group_size=6):
    """Build groups of up to max_group_size words. Also always closes a
    group right after sentence-ending punctuation, regardless of size."""
    sentence_enders = (".", "!", "?")
    groups = []
    current = []

    for word in captions:
        text = word["word"].strip()
        current.append(word)

        if len(current) >= max_group_size or text.endswith(sentence_enders):
            groups.append(current)
            current = []

    if current:
        groups.append(current)

    return groups


def is_number_word(text):
    return bool(re.search(r"\d", text))


def choose_hero_word(group):
    """Pick the word to render big/bold: the longest word that isn't a
    number and isn't a common short/function word. Returns the word dict,
    or None if nothing in the group qualifies."""
    candidates = []
    for w in group:
        text = w["word"].strip()
        clean = text.strip(string.punctuation).lower()
        if is_number_word(text):
            continue
        if clean in STOPWORDS or len(clean) < 4:
            continue
        candidates.append(w)

    if not candidates:
        return None
    return max(candidates, key=lambda w: len(w["word"].strip()))


def style_word(text, is_number, is_hero, scale):
    """Wrap a word in ASS inline override tags for its type, then reset
    back to the Default style so the next word isn't affected. Font sizes
    are scaled to match the current video's resolution."""
    if is_hero:
        size = round(BASE_HERO_FONTSIZE * scale)
        return f"{{\\fnInter\\fs{size}\\b1\\i1}}{text}{{\\r}}"
    if is_number:
        size = round(BASE_NUMBER_FONTSIZE * scale)
        return f"{{\\fnInter\\i1\\fs{size}\\1c&HE0F5FF&}}{text}{{\\r}}"
    return text


def captions_to_ass(captions, ass_path, video_width, video_height,
                     max_group_size=6):
    groups = split_into_groups(captions, max_group_size=max_group_size)

    # One scale factor drives every size below. MIN_SCALE keeps it from
    # shrinking captions past a readable floor on shorter/moderate videos.
    scale = max(MIN_SCALE, video_height / REFERENCE_HEIGHT)
    fontsize = round(BASE_FONTSIZE * scale)
    margin_lr = round(BASE_MARGIN_LR * scale)
    if video_height == 1920:
        margin_v = 420
    else:
        margin_v = 180

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Inter,{fontsize},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,2,2,2,{margin_lr},{margin_lr},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)

        for group in groups:
            hero = choose_hero_word(group)
            hero_id = id(hero) if hero else None

            for j, word in enumerate(group):
                revealed = group[: j + 1]
                parts = []
                for w in revealed:
                    text = w["word"].strip()
                    parts.append(style_word(
                        text,
                        is_number=is_number_word(text),
                        is_hero=(id(w) == hero_id),
                        scale=scale,
                    ))
                line = " ".join(parts)

                start = word["start"]
                end = revealed[-1]["end"] if j + 1 == len(group) else group[j + 1]["start"]

                f.write(
                    f"Dialogue: 0,{seconds_to_ass_time(start)},{seconds_to_ass_time(end)},"
                    f"Default,,0,0,0,,{line}\n"
                )


def render_scene(image_path, audio_path, captions, output_path,
                  video_width, video_height, max_group_size=6):
    ass_path = output_path.replace(".mp4", ".ass")
    captions_to_ass(
        captions, ass_path,
        video_width=video_width, video_height=video_height,
        max_group_size=max_group_size,
    )

    # FFmpeg's subtitle filters need forward slashes and escaped colons on Windows.
    ass_filter_path = ass_path.replace("\\", "/").replace(":", "\\:")

    # Force every scene to the exact target resolution, regardless of what
    # size the source image actually came back as (Pollinations sometimes
    # returns dimensions slightly off from what was requested, e.g. rounded
    # to a multiple of 8/64) - without this, scenes can end up with
    # mismatched sizes that break the crossfade transitions between them.
    scale_pad = (
        f"scale={video_width}:{video_height}:force_original_aspect_ratio=decrease,"
        f"pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", image_path,
        "-i", audio_path,
        "-vf", f"{scale_pad},ass='{ass_filter_path}'",
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("FFmpeg failed:")
        print(result.stderr[-2000:])
        return False

    print(f"Rendered: {output_path}")
    return True


if __name__ == "__main__":
    with open("project_output/project.json") as f:
        project = json.load(f)

    scene = project["scenes"][0]  # first scene only, for this test

    render_scene(
        image_path=scene["image_path"],
        audio_path=scene["audio_path"],
        captions=scene["captions"],
        output_path="project_output/scene_0_test.mp4",
        video_width=project.get("video_width", 1080),
        video_height=project.get("video_height", 1920),
    )