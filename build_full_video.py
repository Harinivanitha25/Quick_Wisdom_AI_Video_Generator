import json
import re
import subprocess

from render_scene import render_scene

def slugify(text, max_length=60):
    """Turn a title into a safe filename: lowercase, spaces to underscores,
    strip anything that isn't a letter/number/underscore/hyphen."""
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s]+", "_", text)
    return text[:max_length].strip("_") or "untitled"


def get_duration(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return float(result.stdout.strip())


def concatenate_with_transitions(video_paths, output_path,
                                  transition="fade", transition_duration=0.5):
    """Join scenes with a crossfade between each, instead of an instant cut.
    Needs real re-encoding (no -c copy), so this takes longer than a plain
    concat, but produces an actual transition rather than a hard cut."""
    if len(video_paths) == 1:
        # Nothing to transition between - just re-encode isn't even needed,
        # copy the single scene straight through.
        cmd = ["ffmpeg", "-y", "-i", video_paths[0], "-c", "copy", output_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("Single-scene copy failed:")
            print(result.stderr[-2000:])
            return False
        print(f"\nFinal video saved: {output_path}")
        return True

    durations = [get_duration(p) for p in video_paths]

    inputs = []
    for p in video_paths:
        inputs += ["-i", p]

    filter_parts = []
    running_duration = durations[0]
    prev_v, prev_a = "0:v", "0:a"

    for i in range(1, len(video_paths)):
        offset = max(0, running_duration - transition_duration)
        v_label, a_label = f"v{i}", f"a{i}"

        filter_parts.append(
            f"[{prev_v}][{i}:v]xfade=transition={transition}:"
            f"duration={transition_duration}:offset={offset:.3f}[{v_label}]"
        )
        filter_parts.append(
            f"[{prev_a}][{i}:a]acrossfade=d={transition_duration}[{a_label}]"
        )

        running_duration = running_duration + durations[i] - transition_duration
        prev_v, prev_a = v_label, a_label

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"[{prev_v}]",
        "-map", f"[{prev_a}]",
        "-c:v", "libx264",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("Transition build failed:")
        print(result.stderr[-2000:])
        return False

    print(f"\nFinal video saved: {output_path}")
    return True


def concatenate_videos(video_paths, output_path):
    list_path = "project_output/concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for path in video_paths:
            # Paths in the concat list are resolved relative to concat_list.txt's
            # own folder (project_output/), so strip that prefix here to avoid
            # it being doubled up (project_output/project_output/...).
            filename = path.replace("project_output/", "").replace(chr(92), "/")
            f.write(f"file '{filename}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_path,
        "-c", "copy",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("Concatenation failed:")
        print(result.stderr[-2000:])
        return False

    print(f"\nFinal video saved: {output_path}")
    return True


def build_full_video(project_path="project_output/project.json"):
    with open(project_path) as f:
        project = json.load(f)

    # Pulled from the project file (set when the assets were generated) so
    # this always matches the actual image dimensions, instead of guessing.
    video_width = project.get("video_width", 1080)
    video_height = project.get("video_height", 1920)
    print(f"Rendering at {video_width}x{video_height} ({project.get('frame_size', 'unknown')})")

    scene_videos = []

    for i, scene in enumerate(project["scenes"]):
        print(f"Rendering scene {i + 1}/{len(project['scenes'])}...")
        output_path = f"project_output/scene_{i}.mp4"

        ok = render_scene(
            image_path=scene["image_path"],
            audio_path=scene["audio_path"],
            captions=scene["captions"],
            output_path=output_path,
            video_width=video_width,
            video_height=video_height,
            max_group_size=6,
        )

        if ok:
            scene_videos.append(output_path)
        else:
            print(f"  Skipping scene {i} due to render failure.")

    if not scene_videos:
        print("No scenes rendered successfully, stopping.")
        return

    print(f"\nJoining {len(scene_videos)} scenes with transitions...")
    filename = slugify(project["title"])
    output_path = f"project_output/{filename}.mp4"
    concatenate_with_transitions(scene_videos, output_path, transition="fade", transition_duration=0.5)


if __name__ == "__main__":
    build_full_video()