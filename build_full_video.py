import json
import subprocess

from render_scene import render_scene


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

    scene_videos = []

    for i, scene in enumerate(project["scenes"]):
        print(f"Rendering scene {i + 1}/{len(project['scenes'])}...")
        output_path = f"project_output/scene_{i}.mp4"

        ok = render_scene(
            image_path=scene["image_path"],
            audio_path=scene["audio_path"],
            captions=scene["captions"],
            output_path=output_path,
            font_name="Impact",
            max_group_size=3,
            max_chars=19,
        )

        if ok:
            scene_videos.append(output_path)
        else:
            print(f"  Skipping scene {i} due to render failure.")

    if not scene_videos:
        print("No scenes rendered successfully, stopping.")
        return

    print(f"\nConcatenating {len(scene_videos)} scenes...")
    concatenate_videos(scene_videos, "project_output/final_video.mp4")


if __name__ == "__main__":
    build_full_video()