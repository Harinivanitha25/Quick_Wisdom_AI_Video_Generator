import json
import subprocess


def seconds_to_srt_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def group_words(captions, group_size=3):
    """Combine consecutive words into chunks, e.g. 3 words shown together
    for the span from the first word's start to the last word's end."""
    groups = []
    for i in range(0, len(captions), group_size):
        chunk = captions[i:i + group_size]
        groups.append({
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
            "word": " ".join(w["word"].strip() for w in chunk),
        })
    return groups


def captions_to_srt(captions, srt_path, group_size=3):
    grouped = group_words(captions, group_size)
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, word in enumerate(grouped, start=1):
            start = seconds_to_srt_time(word["start"])
            end = seconds_to_srt_time(word["end"])
            text = word["word"]
            f.write(f"{i}\n{start} --> {end}\n{text}\n\n")


def render_scene(image_path, audio_path, captions, output_path,
                  font_name="Impact", group_size=2):
    srt_path = output_path.replace(".mp4", ".srt")
    captions_to_srt(captions, srt_path, group_size=group_size)

    # FFmpeg subtitle filter needs forward slashes and escaped colons on Windows.
    srt_filter_path = srt_path.replace("\\", "/").replace(":", "\\:")

    # Alignment=2 keeps captions bottom-anchored (not floating mid-frame if a line
    # is short), MarginV pushes them up from the very bottom edge in pixels.
    style = (
        f"FontName={font_name},FontSize=17,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=1,Alignment=2,MarginV=60"
    )

    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",
        "-i", image_path,
        "-i", audio_path,
        "-vf", f"subtitles='{srt_filter_path}':force_style='{style}'",
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
        print(result.stderr[-2000:])  # last part of the error log, usually the useful bit
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
    )