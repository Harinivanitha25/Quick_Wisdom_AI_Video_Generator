import glob
import json
import os
import re
import subprocess

from render_scene import render_scene

# Silence + held-frame padding added to the end of every scene except the
# last, so scenes get real breathing room instead of running straight into
# each other (or, previously, overlapping during the crossfade). The 0.5s
# crossfade below still blends smoothly into this padding, so the fully
# silent/still portion the viewer perceives is a bit less than this number -
# tuned to land in the 1-1.5s range once that's accounted for.
SCENE_GAP_SECONDS = 1.2


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


def add_watermark(input_path, output_path, watermark_path, video_width,
                  watermark_width=120, opacity=0.65,top_margin=80,right_margin=40, ):
    """Add a small semi-transparent watermark in the top-right corner."""
    if not os.path.exists(watermark_path):
        print(f"Watermark not found: {watermark_path}")
        return False

    filter_complex = (
        f"[1:v]format=rgba,scale={watermark_width}:-1,"
        f"colorchannelmixer=aa={opacity}[wm];"
        f"[0:v][wm]overlay=x=W-w-{right_margin}:y={top_margin}:format=auto[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-i", watermark_path,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "medium",
        "-c:a", "aac",
        "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        output_path,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("Watermark failed:")
        print(result.stderr[-3000:])
        return False

    print(f"Watermark added: {output_path}")
    return True


def concatenate_videos(video_paths, output_path):
    list_dir = os.path.dirname(output_path) or "."
    list_path = f"{list_dir}/concat_list.txt"
    with open(list_path, "w", encoding="utf-8") as f:
        for path in video_paths:
            # Paths in the concat list are resolved relative to concat_list.txt's
            # own folder, so write each clip's path relative to that folder.
            filename = os.path.relpath(path, list_dir).replace(chr(92), "/")
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


def build_full_video(project_path):
    """Render every scene of one project and join them into its final video.
    project_path is that project's project.json; everything is written inside
    the folder it lives in. Returns the final video's path, or None if the
    build failed."""
    project_dir = os.path.dirname(project_path)
    os.makedirs(f"{project_dir}/scenes", exist_ok=True)

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
        output_path = f"{project_dir}/scenes/scene_{i}.mp4"
        is_last_scene = i == len(project["scenes"]) - 1

        ok = render_scene(
            image_path=scene["image_path"],
            audio_path=scene["audio_path"],
            captions=scene["captions"],
            output_path=output_path,
            video_width=video_width,
            video_height=video_height,
            max_group_size=6,
            gap_seconds=0.0 if is_last_scene else SCENE_GAP_SECONDS,
        )

        if ok:
            scene_videos.append(output_path)
        else:
            print(f"  Skipping scene {i} due to render failure.")

    if not scene_videos:
        print("No scenes rendered successfully, stopping.")
        return None

    print(f"\nJoining {len(scene_videos)} scenes with transitions...")
    filename = slugify(project["title"])
    output_path = f"{project_dir}/{filename}.mp4"
    ok = concatenate_with_transitions(scene_videos, output_path, transition="fade", transition_duration=0.5)
    if not ok:
        return None

    # Option 1: one fixed watermark is added to the completed video.
    # Put your transparent PNG at: assets/watermark.png next to build_full_video.py
    watermark_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "assets", "watermark.png"
    )
    watermarked_path = f"{project_dir}/{filename}_watermarked.mp4"

    watermark_ok = add_watermark(
        input_path=output_path,
        output_path=watermarked_path,
        watermark_path=watermark_path,
        video_width=video_width,
        watermark_width=140 if video_width <= 1080 else 200,
        opacity=1,
        top_margin=80 if video_width <= 1080 else 60,
        right_margin=40,
    )

    if watermark_ok:
        # Replace the unwatermarked final video with the watermarked version.
        os.replace(watermarked_path, output_path)
        print(f"\nFinal video with top-right watermark saved: {output_path}")
        return output_path

    # If the watermark image is missing, keep the normal final video.
    print("Watermark was not added; keeping the normal final video.")
    return output_path


if __name__ == "__main__":
    # Run standalone: build the newest project (folder names start with a
    # timestamp, so the last one alphabetically is the most recent).
    projects = sorted(glob.glob("project_output/*/project.json"))
    if projects:
        build_full_video(projects[-1].replace(chr(92), "/"))
    else:
        print("No projects found in project_output/.")