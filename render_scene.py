import json
import subprocess


def seconds_to_srt_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds - int(seconds)) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def split_into_groups(captions, max_group_size=3, max_chars=19):
    """Build groups of up to max_group_size words, but close a group early
    (even below max_group_size) if adding the next word would push the
    combined text past max_chars characters. Also always closes a group
    right after sentence-ending punctuation."""
    sentence_enders = (".", "!", "?")
    groups = []
    current = []

    for word in captions:
        text = word["word"].strip()
        candidate = current + [word]
        candidate_text = " ".join(w["word"].strip() for w in candidate)

        too_many_words = len(candidate) > max_group_size
        too_long = len(candidate_text) > max_chars and len(current) >= 1

        if (too_many_words or too_long) and current:
            groups.append(current)
            current = [word]
        else:
            current.append(word)

        if text.endswith(sentence_enders):
            groups.append(current)
            current = []

    if current:
        groups.append(current)

    return groups


def captions_to_srt(captions, srt_path, max_group_size=3, max_chars=19):
    """Progressive reveal: within each group, each new word appears at the
    moment it's spoken, building up the line (e.g. 'This' -> 'This is'),
    then the next group starts fresh."""
    groups = split_into_groups(captions, max_group_size=max_group_size, max_chars=max_chars)
    entry_num = 1

    with open(srt_path, "w", encoding="utf-8") as f:
        for group in groups:
            for j, word in enumerate(group):
                cumulative_text = " ".join(w["word"].strip() for w in group[:j + 1])
                start = word["start"]
                # Ends right when the next word starts (or at this word's own
                # end if it's the last word in the group).
                end = group[j + 1]["start"] if j + 1 < len(group) else word["end"]

                f.write(f"{entry_num}\n{seconds_to_srt_time(start)} --> {seconds_to_srt_time(end)}\n{cumulative_text}\n\n")
                entry_num += 1


def render_scene(image_path, audio_path, captions, output_path,
                  font_name="Impact", max_group_size=3, max_chars=19):
    srt_path = output_path.replace(".mp4", ".srt")
    captions_to_srt(captions, srt_path, max_group_size=max_group_size, max_chars=max_chars)

    # FFmpeg subtitle filter needs forward slashes and escaped colons on Windows.
    srt_filter_path = srt_path.replace("\\", "/").replace(":", "\\:")

    # Alignment=2 keeps captions bottom-anchored (not floating mid-frame if a line
    # is short), MarginV pushes them up from the very bottom edge in pixels.
    style = (
        f"FontName={font_name},FontSize=17,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,BorderStyle=1,Outline=1,Shadow=1,"
        "Alignment=2,MarginV=80"
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