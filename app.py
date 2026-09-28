import asyncio
import contextlib
import difflib
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

import flet as ft
import webbrowser

# When launched without a console (pythonw), stop ffmpeg/ffprobe from
# flashing a black window every time they run.
if sys.platform == "win32":
    _orig_popen_init = subprocess.Popen.__init__

    def _quiet_popen_init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        _orig_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _quiet_popen_init

    
# YouTube upload needs the google-api-python-client / google-auth-oauthlib
# packages and client_secret.json. If the packages are missing, the Upload
# button shows a message instead of crashing the app.
try:
    from youtube_upload import upload_video, YouTubeUploadError
except ImportError:
    upload_video = None

    class YouTubeUploadError(Exception):
        pass

# Anchored to this file's own location, not whatever folder the app happened
# to be launched from. The backend writes to "project_output/..." relative to
# the working directory, so switching to this folder first keeps it and the
# library (which reads PROJECT_DIR below) looking at the same place.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)

# In-app video playback needs the separate flet-video package
# (pip install flet-video). If it isn't installed, the player screen falls
# back to a message + "open in system player" button instead of crashing.
try:
    import flet_video as ftv
except ImportError:
    ftv = None

from generate_video_assets import (
    generate_script, process_script, generate_youtube_metadata, create_project_dir,
)
from build_full_video import build_full_video, slugify

# --- Design tokens (sage green / dark) --------------------------------
BG = "#10140F"
SURFACE = "#1B221A"
BORDER = "#2A3224"
BORDER_STRONG = "#3A4534"
TEXT = "#EDEFEA"
TEXT_MUTED = "#8B9483"
SAGE = "#8FA878"
TEXT_ON_SAGE = "#10140F"

DURATION_PRESETS = [35, 50, 60, 75, 90]

# Every project lives in its own folder inside PROJECT_DIR:
#   project_output/<date>_<time>_<title>/  images/  audio/  scenes/  project.json  <title>.mp4
PROJECT_DIR = os.path.join(APP_DIR, "project_output")


def open_in_system_player(path):
    """Open a file with whatever the OS considers the default app for it -
    used for 'Play video' rather than embedding a video player control,
    since that needs a separate Flet extension package we haven't tested."""
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path])
    else:
        subprocess.run(["xdg-open", path])


# --- Project folders ---------------------------------------------------
EMPTY_YOUTUBE_METADATA = {"youtube_title": "", "description": "", "tags": []}


def project_json_path(project_dir):
    return os.path.join(project_dir, "project.json")


def load_project(project_dir):
    with open(project_json_path(project_dir), encoding="utf-8") as f:
        project = json.load(f)
    if not project.get("youtube_metadata"):
        project["youtube_metadata"] = dict(EMPTY_YOUTUBE_METADATA)
    return project


def save_project(project, project_dir):
    with open(project_json_path(project_dir), "w", encoding="utf-8") as f:
        json.dump(project, f, indent=2)


def realign_captions(old_words, new_texts):
    """Rebuild a scene's caption list after a free-text edit, keeping every
    word's real start/end (from Whisper's transcription of the actual TTS
    audio) wherever the word is unchanged, so timing stays correct instead
    of drifting. Uses difflib to line up old_words (list of {start, end,
    word}) against new_texts (list of plain strings from the edited box):
    matched stretches keep their original timestamps exactly; only an
    inserted/replaced stretch gets new timestamps, interpolated across the
    time span its old words covered (proportional to each new word's
    length) so the result stays monotonic and lines up with nearby words
    that didn't change."""
    old_norm = [w["word"].strip().lower() for w in old_words]
    new_norm = [t.strip().lower() for t in new_texts]
    matcher = difflib.SequenceMatcher(a=old_norm, b=new_norm, autojunk=False)

    rebuilt = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                old_word = old_words[i1 + k]
                rebuilt.append({
                    "start": old_word["start"], "end": old_word["end"],
                    "word": new_texts[j1 + k],
                })
            continue

        new_slice = new_texts[j1:j2]
        if not new_slice:
            continue  # a delete: those old words are simply dropped

        # Anchor the replaced/inserted span to the timing around it.
        if i2 > i1:
            # A replacement: spans the old words actually being replaced.
            span_start = old_words[i1]["start"]
            span_end = old_words[i2 - 1]["end"]
        else:
            # A pure insertion (i1 == i2, no old words consumed): use the
            # real gap between the previous word's end and the next word's
            # start, never a word's own start twice over, so the inserted
            # word can't end up sharing a timestamp with its neighbor.
            span_start = old_words[i1 - 1]["end"] if i1 > 0 else 0.0
            span_end = old_words[i1]["start"] if i1 < len(old_words) else span_start + 0.4 * len(new_slice)

        span_end = max(span_end, span_start + 0.05 * len(new_slice))
        total_chars = sum(max(1, len(t)) for t in new_slice)
        cursor = span_start
        for t in new_slice:
            portion = (max(1, len(t)) / total_chars) * (span_end - span_start)
            word_end = cursor + portion
            rebuilt.append({"start": cursor, "end": word_end, "word": t})
            cursor = word_end

    # Guarantee strictly increasing start times even when the original
    # transcription had zero gap between two adjacent words (so an inserted
    # word had no real room to land in): nudge each word forward by the
    # smallest amount needed rather than letting it share a timestamp with
    # (or start before) the word right before it.
    min_gap = 0.02
    for k in range(1, len(rebuilt)):
        min_start = rebuilt[k - 1]["start"] + min_gap
        if rebuilt[k]["start"] < min_start:
            rebuilt[k]["start"] = min_start
        if rebuilt[k]["end"] <= rebuilt[k]["start"]:
            rebuilt[k]["end"] = rebuilt[k]["start"] + min_gap

    return rebuilt


def find_project_video(project_dir, project):
    """The finished video inside a project folder, or None if it hasn't been
    built yet. (Only looks at the folder's top level - scenes/ holds the
    intermediate clips, which aren't the finished video.)"""
    name = project.get("video_file")
    if name and os.path.exists(os.path.join(project_dir, name)):
        return os.path.join(project_dir, name)
    for filename in sorted(os.listdir(project_dir)):
        if filename.endswith(".mp4"):
            return os.path.join(project_dir, filename)
    return None


def delete_project_assets(project_dir):
    """Delete the entire project folder and every asset inside it."""
    if not project_dir or not os.path.isdir(project_dir):
        return False
    try:
        # Safety check: only delete folders inside our project_output directory.
        project_root = os.path.abspath(PROJECT_DIR)
        target = os.path.abspath(project_dir)
        if os.path.dirname(target) != project_root:
            print(f"Refusing to delete unexpected path: {target}")
            return False
        shutil.rmtree(target)
        print(f"Deleted project and all assets: {target}")
        return True
    except OSError as ex:
        print(f"Could not delete project: {ex}")
        return False


def list_library_projects():
    """[(modified_time, video_path, project), ...] for every project with a
    finished video, newest first. Projects that were generated but never
    built stay on disk but don't appear here."""
    items = []
    if not os.path.isdir(PROJECT_DIR):
        return items
    for name in os.listdir(PROJECT_DIR):
        project_dir = os.path.join(PROJECT_DIR, name)
        if not os.path.isfile(project_json_path(project_dir)):
            continue
        try:
            project = load_project(project_dir)
        except (OSError, ValueError):
            continue
        video_path = find_project_video(project_dir, project)
        if video_path:
            items.append((os.path.getmtime(video_path), video_path, project))
    items.sort(key=lambda item: item[0], reverse=True)
    return items


def adopt_legacy_videos():
    """Earlier versions kept every project in one shared folder, where each new
    project overwrote the last one's images and voices. Any finished video
    still sitting loose in project_output/ is moved into a project folder of
    its own (with its saved title/metadata and thumbnail) so the library
    treats it like any other project. Its original images/audio were already
    overwritten, so only the video and its metadata carry over."""
    if not os.path.isdir(PROJECT_DIR):
        return

    try:
        with open(os.path.join(PROJECT_DIR, "project.json"), encoding="utf-8") as f:
            shared = json.load(f)
    except (OSError, ValueError):
        shared = {}

    for filename in os.listdir(PROJECT_DIR):
        video_path = os.path.join(PROJECT_DIR, filename)
        if not (os.path.isfile(video_path) and filename.endswith(".mp4") and not filename.startswith("scene_")):
            continue

        stem = os.path.splitext(filename)[0]
        meta_file = os.path.join(PROJECT_DIR, stem + ".meta.json")
        thumb_file = os.path.join(PROJECT_DIR, stem + ".thumb.png")

        meta = {}
        meta_loaded = False
        if os.path.isfile(meta_file):
            try:
                with open(meta_file, encoding="utf-8") as f:
                    meta = json.load(f)
                meta_loaded = True
            except (OSError, ValueError):
                pass
        if not meta and shared and slugify(shared.get("title", "")) == stem:
            meta = shared

        stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(os.path.getmtime(video_path)))
        base = os.path.join(PROJECT_DIR, f"{stamp}_{stem}"[:80])
        project_dir, suffix = base, 2
        while os.path.exists(project_dir):
            project_dir = f"{base}_{suffix}"
            suffix += 1

        os.makedirs(os.path.join(project_dir, "images"))
        try:
            shutil.move(video_path, os.path.join(project_dir, filename))
        except OSError:  # e.g. the file is open in a player - try again next time
            shutil.rmtree(project_dir, ignore_errors=True)
            continue

        try:
            if os.path.isfile(thumb_file):
                shutil.move(thumb_file, os.path.join(project_dir, "images", "scene_0.png"))
            if meta_loaded:
                os.remove(meta_file)
        except OSError:
            pass

        save_project(
            {
                "title": meta.get("title") or stem.replace("_", " ").title(),
                "frame_size": meta.get("frame_size"),
                "youtube_metadata": meta.get("youtube_metadata") or dict(EMPTY_YOUTUBE_METADATA),
                "scenes": [],
                "video_file": filename,
            },
            project_dir,
        )


def probe_video_size(path):
    """(width, height) of a video via ffprobe, or None if it can't be read."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, text=True,
        )
        width, height = result.stdout.strip().split("x")
        return int(width), int(height)
    except Exception:
        return None

def make_video_player(video_path, max_w, max_h, autoplay=True):
    """A video player sized to fit inside max_w x max_h while keeping the
    video's own aspect ratio. Falls back to an install hint if flet-video
    isn't available. Returns the sized container."""
    width, height = probe_video_size(video_path) or (1080, 1920)
    fit_scale = min(max_w / width, max_h / height)
    box_w, box_h = round(width * fit_scale), round(height * fit_scale)

    if ftv is not None:
        player = ftv.Video(expand=True, playlist=[ftv.VideoMedia(video_path)], autoplay=autoplay)
    else:
        player = ft.Column(
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Text("In-app playback needs the flet-video package.", size=13, color=TEXT_MUTED),
                ft.Text("pip install flet-video", size=13, color=SAGE, selectable=True),
            ],
        )

    return ft.Container(
        width=box_w, height=box_h, bgcolor="#000000", border_radius=12,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS, content=player,
    )

# --- Live log ----------------------------------------------------------
# The generation/build code reports progress with print() (Gemini errors,
# "Processing scene 2/6...", "Rendered: ...", etc.). QueueWriter captures that
# output while a job runs so the UI can show the same lines, and still passes
# everything through to the real terminal too.
LOG_MAX_LINES = 400
PLAYER_MAX_W = 900
PLAYER_MAX_H = 640

# Library card layout. Every part has a fixed size, so a card looks the same
# whether its title is one word or a full sentence: the title always gets
# exactly two lines of room (longer titles end in "..."), the file/date line
# is one line, and the buttons sit at the same spot on every card.
LIB_CARD_WIDTH = 260
LIB_CARD_HEIGHT = 310
LIB_CARD_PADDING = 12
LIB_INNER_WIDTH = LIB_CARD_WIDTH - 2 * LIB_CARD_PADDING - 2  # minus the 1px border
LIB_THUMB_HEIGHT = 140
LIB_TITLE_HEIGHT = 42
LIB_META_HEIGHT = 35
LIB_BUTTON_HEIGHT = 40


def classify_log_line(line):
    low = line.lower().strip()
    if any(k in low for k in ("error", "failed", "failure", "traceback", "warning", "skipping")):
        return ft.Colors.RED_300
    if any(k in low for k in ("busy", "retrying", "waiting")):
        return ft.Colors.AMBER_300
    if low.startswith(("processing scene", "rendering scene", "joining", "generating script",
                       "generating youtube")):
        return TEXT
    if any(k in low for k in ("generated", "rendered", "saved", "done")):
        return SAGE
    return TEXT_MUTED


class QueueWriter(io.TextIOBase):
    def __init__(self, line_queue, passthrough=None):
        self._queue = line_queue
        self._passthrough = passthrough
        self._buffer = ""
        self._lock = threading.Lock()

    def write(self, text):
        if self._passthrough is not None:
            try:
                self._passthrough.write(text)
            except Exception:
                pass
        with self._lock:
            self._buffer += text
            *complete, self._buffer = self._buffer.split("\n")
            for line in complete:
                if line.strip():
                    self._queue.put(line.rstrip())
        return len(text)

    def flush(self):
        if self._passthrough is not None:
            try:
                self._passthrough.flush()
            except Exception:
                pass
        with self._lock:
            if self._buffer.strip():
                self._queue.put(self._buffer.rstrip())
            self._buffer = ""


class LogPanel:
    """A scrolling, auto-following console box. `lines` keeps the raw text so
    a failure screen can rebuild the same log after the running screen is gone."""

    def __init__(self, width=640, height=260, lines=None):
        self.lines = []
        self.view = ft.ListView(spacing=2, auto_scroll=True, expand=True)
        self.control = ft.Container(
            width=width, height=height, padding=12, border_radius=8,
            bgcolor="#0B0E0A", border=ft.Border.all(0.5, BORDER),
            content=self.view,
        )
        self.extend(lines or [])

    def extend(self, new_lines):
        for line in new_lines:
            self.lines.append(line)
            self.view.controls.append(
                ft.Text(line, size=12, font_family="Consolas", selectable=True,
                        color=classify_log_line(line))
            )
        del self.lines[:-LOG_MAX_LINES]
        overflow = len(self.view.controls) - LOG_MAX_LINES
        if overflow > 0:
            del self.view.controls[:overflow]


def main(page: ft.Page):
    page.title = "AI Video Generator"
    page.window.width = 1280
    page.window.height = 860
    page.window.maximized = True
    page.padding = 0
    page.bgcolor = BG
    page.theme_mode = ft.ThemeMode.DARK
    page.scroll = ft.ScrollMode.AUTO

    def show_screen(view: ft.Control, top_align: bool = False):
        page.vertical_alignment = ft.MainAxisAlignment.START if top_align else ft.MainAxisAlignment.CENTER
        page.horizontal_alignment = ft.CrossAxisAlignment.START if top_align else ft.CrossAxisAlignment.CENTER
        page.controls.clear()
        page.controls.append(view)
        page.update()

    def label(text: str):
        return ft.Text(text, size=14, color=TEXT_MUTED)

    # The project currently being created (script -> media -> review -> build).
    # Set when media generation starts; screens 4 and the build read it.
    state = {"project_dir": None}

    async def run_with_log(log, func, *args):
        """Run a blocking function in a worker thread and, while it runs,
        stream everything it print()s into `log` (a LogPanel). Exceptions
        from the function are re-raised here, like a normal await."""
        line_queue = queue.Queue()
        writer = QueueWriter(line_queue, passthrough=sys.stdout)

        def drain():
            lines = []
            while True:
                try:
                    lines.append(line_queue.get_nowait())
                except queue.Empty:
                    break
            if lines:
                log.extend(lines)
                page.update()

        with contextlib.redirect_stdout(writer):
            task = asyncio.ensure_future(asyncio.to_thread(func, *args))
            while not task.done():
                drain()
                await asyncio.sleep(0.15)
            writer.flush()
        drain()
        return task.result()

    # --- Screen 0: Library ---------------------------------------------
    def library_screen():
        adopt_legacy_videos()

        def make_play_handler(path, title):
            def handler(e):
                show_screen(player_screen(path, title))
            return handler

        def make_edit_handler(path, project):
            def handler(e):
                show_screen(final_review_screen(project, path))
            return handler

        def make_delete_handler(video_path):
            project_dir = os.path.dirname(video_path)

            def handler(e):
                def close_dialog(_=None):
                    page.pop_dialog()
                    page.update()

                def confirm_delete(_):
                    close_dialog()
                    if delete_project_assets(project_dir):
                        # Rebuild the library so the deleted video disappears immediately.
                        show_screen(library_screen(), top_align=True)
                    else:
                        page.snack_bar = ft.SnackBar(
                            ft.Text("Could not delete this video's assets."),
                            bgcolor=ft.Colors.RED_900,
                        )
                        page.snack_bar.open = True
                        page.update()

                dialog = ft.AlertDialog(
                    modal=True,
                    title=ft.Text("Delete video?", color=TEXT),
                    content=ft.Text(
                        "This will permanently delete the video and all of its assets "
                        "(images, audio, scenes, and project data).",
                        color=TEXT_MUTED,
                    ),
                    actions=[
                        ft.TextButton("Cancel", on_click=close_dialog),
                        ft.Button(
                            "Delete",
                            style=ft.ButtonStyle(bgcolor=ft.Colors.RED_800, color=ft.Colors.WHITE),
                            on_click=confirm_delete,
                        ),
                    ],
                    actions_alignment=ft.MainAxisAlignment.END,
                )
                page.show_dialog(dialog)
                page.update()

            return handler

        cards = []
        for modified, video_path, project in list_library_projects():
            filename = os.path.basename(video_path)
            title = project.get("title") or filename
            created = time.strftime("%d %b %Y, %H:%M", time.localtime(modified))

            thumb_path = os.path.join(os.path.dirname(video_path), "images", "scene_0.png")
            if not os.path.exists(thumb_path):
                thumb_path = None
            thumbnail = ft.Container(
                width=LIB_INNER_WIDTH, height=LIB_THUMB_HEIGHT, bgcolor=BG, border_radius=6,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS, alignment=ft.Alignment.CENTER,
                content=(
                    ft.Image(src=thumb_path, width=LIB_INNER_WIDTH, height=LIB_THUMB_HEIGHT, fit=ft.BoxFit.COVER)
                    if thumb_path
                    else ft.Icon(ft.Icons.MOVIE, color=TEXT_MUTED, size=28)
                ),
            )

            cards.append(
                ft.Container(
                    bgcolor=SURFACE, border_radius=10, border=ft.Border.all(0.5, BORDER),
                    padding=LIB_CARD_PADDING, width=LIB_CARD_WIDTH, height=LIB_CARD_HEIGHT,
                    content=ft.Column(
                        spacing=8,
                        controls=[
                            thumbnail,
                            ft.Container(
                                height=LIB_TITLE_HEIGHT, alignment=ft.Alignment.TOP_LEFT,
                                content=ft.Text(title, size=14, color=TEXT, weight=ft.FontWeight.W_500,
                                                max_lines=2, overflow=ft.TextOverflow.ELLIPSIS),
                            ),
                            ft.Container(
                                height=LIB_META_HEIGHT, alignment=ft.Alignment.TOP_LEFT,
                                content=ft.Text(f"{filename}  -  {created}", size=11, color=TEXT_MUTED,
                                                max_lines=3, overflow=ft.TextOverflow.ELLIPSIS),
                            ),
                            ft.Row(
                                spacing=8,
                                controls=[
                                    ft.Button(
                                        content=ft.Row(
                                            controls=[ft.Icon(ft.Icons.PLAY_ARROW, size=16)],
                                            spacing=4,
                                        ),
                                        style=ft.ButtonStyle(bgcolor=SAGE, color=TEXT_ON_SAGE),
                                        height=LIB_BUTTON_HEIGHT,
                                        on_click=make_play_handler(video_path, title),
                                    ),
                                    ft.Button(
                                        content=ft.Row(
                                            controls=[ft.Icon(ft.Icons.EDIT, size=16)],
                                            spacing=4,
                                        ),
                                        style=ft.ButtonStyle(bgcolor=BG, color=TEXT),
                                        height=LIB_BUTTON_HEIGHT,
                                        on_click=make_edit_handler(video_path, project),
                                    ),
                                    ft.Button(
                                        content=ft.Row(
                                            controls=[ft.Icon(ft.Icons.DELETE_OUTLINE, size=16)],
                                            spacing=4,
                                        ),
                                        style=ft.ButtonStyle(bgcolor=BG, color=ft.Colors.RED_300),
                                        height=LIB_BUTTON_HEIGHT,
                                        on_click=make_delete_handler(video_path),
                                    ),
                                ],
                            ),
                        ],
                    ),
                )
            )

        if not cards:
            cards.append(
                ft.Text("No finished videos yet - generate one to see it here.", size=13, color=TEXT_MUTED)
            )

        def on_new_project_click(e):
            show_screen(new_project_screen())

        return ft.Container(
            padding=32,
            content=ft.Column(
                spacing=20,
                controls=[
                    ft.Row(
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        controls=[
                            ft.Text("Your videos", size=25, weight=ft.FontWeight.W_500, color=TEXT),
                            ft.Button(
                                content=ft.Row(
                                    controls=[ft.Icon(ft.Icons.ADD, size=16), ft.Text("New project", size=15)],
                                    spacing=6,
                                ),
                                style=ft.ButtonStyle(bgcolor=SAGE, color=TEXT_ON_SAGE),
                                on_click=on_new_project_click,
                            ),
                        ],
                    ),
                    ft.Row(controls=cards, spacing=14, run_spacing=14, wrap=True),
                ],
            ),
        )

    # --- In-app player (opened by the library's Play button) -----------
    def player_screen(video_path, title):
        player_box = make_video_player(video_path, PLAYER_MAX_W, PLAYER_MAX_H)
        box_w = player_box.width

        def on_back_click(e):
            show_screen(library_screen(), top_align=True)

        def on_system_player_click(e):
            open_in_system_player(video_path)

        return ft.Container(
            width=max(box_w + 64, 560), padding=24, border_radius=16, bgcolor=BG,
            border=ft.Border.all(0.5, BORDER),
            content=ft.Column(
                spacing=16,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(
                        spacing=12,
                        controls=[
                            ft.Button(
                                content=ft.Row(
                                    controls=[ft.Icon(ft.Icons.ARROW_BACK, size=16), ft.Text("Library", size=14)],
                                    spacing=6,
                                ),
                                style=ft.ButtonStyle(bgcolor=SURFACE, color=TEXT),
                                on_click=on_back_click,
                            ),
                        ],
                    ),
                    player_box,
                    ft.Button(
                        content=ft.Row(
                            controls=[ft.Icon(ft.Icons.OPEN_IN_NEW, size=14), ft.Text("Open in system player", size=13)],
                            spacing=6,
                        ),
                        style=ft.ButtonStyle(bgcolor=BG, color=TEXT_MUTED),
                        on_click=on_system_player_click,
                    ),
                ],
            ),
        )

    # --- Screen 2 / 2b: Generating (reused for script, then assets) ---
    def generating_screen(title_text: str, status_text: str, log: LogPanel):
        return ft.Container(
            width=720,
            padding=40,
            border_radius=16,
            bgcolor=BG,
            border=ft.Border.all(0.5, BORDER),
            content=ft.Column(
                spacing=20,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.ProgressRing(width=44, height=44, stroke_width=4, color=SAGE),
                    ft.Text(title_text, size=22, weight=ft.FontWeight.W_500, color=TEXT),
                    ft.Text(status_text, size=15, color=TEXT_MUTED, text_align=ft.TextAlign.CENTER),
                    log.control,
                ],
            ),
        )

    def generation_failed_screen(error_message: str, log_lines=None):
        def on_retry_click(e):
            show_screen(new_project_screen())

        controls = [
            ft.Text("Something went wrong", size=20, weight=ft.FontWeight.W_500, color=ft.Colors.RED_300),
            ft.Text(error_message, size=14, color=TEXT_MUTED, selectable=True),
        ]
        if log_lines:
            controls += [label("Log"), LogPanel(width=648, height=220, lines=log_lines).control]
        controls.append(
            ft.Button(
                "Back to new project",
                style=ft.ButtonStyle(bgcolor=SAGE, color=TEXT_ON_SAGE),
                on_click=on_retry_click,
            )
        )

        return ft.Container(
            width=720,
            padding=36,
            border_radius=16,
            bgcolor=BG,
            border=ft.Border.all(0.5, BORDER),
            content=ft.Column(spacing=20, controls=controls),
        )

    # --- Screen 3: Script review (editable, before expensive steps) ---
    def script_review_screen(script, frame_size):
        scene_field_pairs = []  # (narration_field, image_prompt_field) per scene

        scene_cards = []
        for i, scene in enumerate(script["scenes"]):
            narration_field = ft.TextField(
                value=scene["narration"],
                multiline=True,
                min_lines=2,
                max_lines=4,
                bgcolor=BG,
                border_color=BORDER,
                focused_border_color=SAGE,
                color=TEXT,
                width=775,
                text_size=13,
                border_radius=6,
            )
            image_prompt_field = ft.TextField(
                value=scene["image_prompt"],
                multiline=True,
                min_lines=1,
                max_lines=3,
                bgcolor=BG,
                border_color=BORDER,
                focused_border_color=SAGE,
                color=TEXT_MUTED,
                width=775,
                text_size=12,
                border_radius=6,
            )
            scene_field_pairs.append((narration_field, image_prompt_field))

            scene_cards.append(
                ft.Container(
                    bgcolor=SURFACE,
                    border_radius=10,
                    border=ft.Border.all(0.5, BORDER),
                    padding=16,
                    width=790,
                    content=ft.Column(
                        spacing=8,
                        controls=[
                            ft.Text(f"Scene {i + 1}", size=12, color=SAGE, weight=ft.FontWeight.W_500),
                            label("Narration"),
                            narration_field,
                            label("Image description"),
                            image_prompt_field,
                        ],
                    ),
                )
            )

        status_text = ft.Text("", size=13, color=ft.Colors.RED_300, visible=False)

        async def on_continue_click(e):
            # Pull edited values back into the script dict before moving on.
            for (narration_field, image_prompt_field), scene in zip(scene_field_pairs, script["scenes"]):
                narration = (narration_field.value or "").strip()
                image_prompt = (image_prompt_field.value or "").strip()
                if not narration or not image_prompt:
                    status_text.value = "Every scene needs both narration and an image description."
                    status_text.visible = True
                    page.update()
                    return
                scene["narration"] = narration
                scene["image_prompt"] = image_prompt

            status_text.visible = False
            page.update()

            log = LogPanel()
            show_screen(generating_screen(
                "Generating your video",
                "Images, voice, captions, and metadata - this takes a bit longer...",
                log,
            ))

            try:
                # A new folder per project, so this never overwrites the
                # images/voices of a video made earlier.
                project_dir = create_project_dir(script["title"])
                state["project_dir"] = project_dir
                project = await run_with_log(log, process_script, script, frame_size, None, project_dir)
                youtube_metadata = await run_with_log(log, generate_youtube_metadata, script)
                project["youtube_metadata"] = youtube_metadata
                # process_script() already saved project.json once, but that
                # was before youtube_metadata existed - save again so the
                # file on disk (which Screen 4 reads from) is complete.
                save_project(project, project_dir)
            except Exception as ex:
                show_screen(generation_failed_screen(f"Unexpected error: {ex}", log.lines))
                return

            show_screen(review_edit_screen())

        continue_button = ft.Button(
            content=ft.Row(
                controls=[
                    ft.Text("Looks good, generate media", size=15),
                    ft.Icon(ft.Icons.ARROW_FORWARD, size=18),
                ],
                spacing=10,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            style=ft.ButtonStyle(bgcolor=SAGE, color=TEXT_ON_SAGE),
            height=52,
            on_click=on_continue_click,
        )

        return ft.Container(
            width=820,
            padding=32,
            border_radius=16,
            bgcolor=BG,
            border=ft.Border.all(0.5, BORDER),
            content=ft.Column(
                spacing=16,
                controls=[
                    ft.Text(script["title"], size=24, weight=ft.FontWeight.W_500, color=TEXT),
                    ft.Text(f"{len(script['scenes'])} scenes - edit before generating media", size=13, color=TEXT_MUTED),
                    ft.Column(controls=scene_cards, spacing=12, scroll=ft.ScrollMode.AUTO, height=440),
                    status_text,
                    continue_button,
                ],
            ),
        )

    # --- Screen 1: New Project ---------------------------------------
    def new_project_screen():
        topic_field = ft.TextField(
            hint_text="e.g. surprising facts about octopuses",
            bgcolor=SURFACE,
            border_color=BORDER,
            focused_border_color=SAGE,
            color=TEXT,
            text_size=15,
            border_radius=8,
            width=660,
            height=52,
        )

        duration_field = ft.TextField(
            value="35",
            width=160,
            height=52,
            text_size=16,
            keyboard_type=ft.KeyboardType.NUMBER,
            bgcolor=SURFACE,
            border_color=SAGE,
            focused_border_color=SAGE,
            color=TEXT,
            border_radius=8,
        )

        duration_error = ft.Text(
            "Enter a duration of at least 5 seconds.",
            color=ft.Colors.RED_300,
            size=13,
            visible=False,
        )

        # Quick-pick chips: clicking one fills the exact-seconds field,
        # rather than replacing it - the field itself is always the
        # source of truth and stays freely editable.
        chip_refs = {}

        def style_chips():
            current = duration_field.value
            for value, chip in chip_refs.items():
                selected = str(value) == current
                chip.bgcolor = SAGE if selected else "transparent"
                chip.border = ft.Border.all(1, SAGE if selected else BORDER)
                chip.content.color = TEXT_ON_SAGE if selected else TEXT_MUTED

        def on_chip_click(value):
            def handler(e):
                duration_field.value = str(value)
                style_chips()
                page.update()
            return handler

        chips = []
        for preset in DURATION_PRESETS:
            chip = ft.Container(
                content=ft.Text(f"{preset}s", size=14),
                padding=ft.Padding.symmetric(horizontal=14, vertical=8),
                border_radius=20,
                on_click=on_chip_click(preset),
            )
            chip_refs[preset] = chip
            chips.append(chip)
        style_chips()

        def on_duration_change(e):
            style_chips()
            page.update()

        duration_field.on_change = on_duration_change

        # Frame size: two selectable cards, managed manually rather than
        # a default RadioGroup so the styling matches the approved mockup.
        frame_size = {"value": "9:16"}
        frame_9_16 = ft.Container(
            content=ft.Text("9:16 - Shorts", size=15, color=TEXT),
            padding=16,
            border_radius=10,
            bgcolor=SURFACE,
            expand=1,
            alignment=ft.Alignment.CENTER,
        )
        frame_16_9 = ft.Container(
            content=ft.Text("16:9 - Videos", size=15, color=TEXT_MUTED),
            padding=16,
            border_radius=10,
            bgcolor=SURFACE,
            expand=1,
            alignment=ft.Alignment.CENTER,
        )

        def style_frame_cards():
            frame_9_16.border = ft.Border.all(
                1.5 if frame_size["value"] == "9:16" else 0.5,
                SAGE if frame_size["value"] == "9:16" else BORDER,
            )
            frame_9_16.content.color = TEXT if frame_size["value"] == "9:16" else TEXT_MUTED
            frame_16_9.border = ft.Border.all(
                1.5 if frame_size["value"] == "16:9" else 0.5,
                SAGE if frame_size["value"] == "16:9" else BORDER,
            )
            frame_16_9.content.color = TEXT if frame_size["value"] == "16:9" else TEXT_MUTED

        def select_frame(value):
            def handler(e):
                frame_size["value"] = value
                style_frame_cards()
                page.update()
            return handler

        frame_9_16.on_click = select_frame("9:16")
        frame_16_9.on_click = select_frame("16:9")
        style_frame_cards()

        topic_error = ft.Text(
            "Enter a topic first.",
            color=ft.Colors.RED_300,
            size=13,
            visible=False,
        )
        def on_back_click(e):
                    show_screen(library_screen(), top_align=True)

        def on_topic_change(e):
            if topic_error.visible and topic_field.value.strip():
                topic_error.visible = False
                page.update()

        topic_field.on_change = on_topic_change

        async def on_generate_click(e):
            topic = (topic_field.value or "").strip()
            duration_text = (duration_field.value or "").strip()

            valid = True
            if not topic:
                topic_error.visible = True
                valid = False
            else:
                topic_error.visible = False

            duration_seconds = None
            if not duration_text.isdigit() or int(duration_text) < 5:
                duration_error.visible = True
                valid = False
            else:
                duration_error.visible = False
                duration_seconds = int(duration_text)

            page.update()
            if not valid:
                return

            selected_frame_size = frame_size["value"]

            log = LogPanel()
            show_screen(generating_screen("Writing your script", "Calling Gemini - usually a few seconds...", log))

            try:
                script = await run_with_log(log, generate_script, topic, duration_seconds)
            except Exception as ex:
                show_screen(generation_failed_screen(f"Unexpected error: {ex}", log.lines))
                return

            if script is None:
                real_error = generate_script.last_error
                if real_error:
                    detail = real_error.get("message", str(real_error))
                    status = real_error.get("status", "")
                    error_text = f"Gemini error ({status}): {detail}"
                else:
                    error_text = "Gemini couldn't generate a script, for an unknown reason."
                show_screen(generation_failed_screen(error_text, log.lines))
                return

            show_screen(script_review_screen(script, selected_frame_size))
        back_button = ft.Button("Back to library", style=ft.ButtonStyle(bgcolor=BORDER, color=TEXT), on_click=on_back_click)
        generate_button = ft.Button(
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.AUTO_AWESOME, size=18),
                    ft.Text("Generate script", size=15),
                ],
                spacing=10,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
            style=ft.ButtonStyle(bgcolor=SAGE, color=TEXT_ON_SAGE),
            height=52,
            on_click=on_generate_click,
        )

        card = ft.Container(
            width=760,
            padding=40,
            border_radius=16,
            bgcolor=BG,
            border=ft.Border.all(0.5, BORDER),
            content=ft.Column(
                spacing=22,
                controls=[
                    back_button,
                    ft.Text("Create a New Video", size=35, weight=ft.FontWeight.W_500, color=SAGE),
                    ft.Column(
                        spacing=8,
                        controls=[label("Enter your Topic"), topic_field, topic_error],
                    ),
                    ft.Column(
                        spacing=8,
                        controls=[
                            label("Duration (seconds)"),
                            duration_field,
                            ft.Row(controls=chips, spacing=8),
                            duration_error,
                        ],
                    ),
                    ft.Column(
                        spacing=8,
                        controls=[label("Frame size"), ft.Row(controls=[frame_9_16, frame_16_9], spacing=10)],
                    ),
                    generate_button,
                ],
            ),
        )

        return card

    # --- Screen 4: Review (title + scene reference, then build) -------
    def review_edit_screen():
        project_dir = state["project_dir"]
        project = load_project(project_dir)

        title_field = ft.TextField(
            value=project["title"],
            bgcolor=SURFACE, border_color=BORDER, focused_border_color=SAGE,
            color=TEXT, text_size=13, border_radius=8, height=44,width=450,
        )

        # Words are edited straight into project["scenes"][i]["captions"][j]
        # ("word" dict entries) the moment a tap-to-edit field is committed,
        # so no separate pull-back-into-project step is needed on Save/Build.
        # Fixed card geometry so every scene card is exactly the same size:
        # the image fills the card's inner width, the narration and caption
        # boxes have fixed heights (3 lines each), so nothing depends on how
        # long a scene's text happens to be.
        CARD_W = 280
        CARD_PAD = 12
        INNER_W = CARD_W - 2 * CARD_PAD
        NARRATION_H = 48
        CAPTION_HEADER_H = 24
        CAPTION_BOX_H = 88
        CAPTION_NOTE_H = 14
        CAPTIONS_BLOCK_H = CAPTION_HEADER_H + CAPTION_BOX_H + CAPTION_NOTE_H + 2 * 6
        # Same aspect ratio as the project's video, so 9:16 and 16:9 projects
        # both show the whole frame at the card's full inner width.
        frame_w = project.get("video_width") or 1080
        frame_h = project.get("video_height") or 1920
        IMAGE_H = round(INNER_W * frame_h / frame_w)
        CARD_H = CARD_PAD * 2 + IMAGE_H + 16 + NARRATION_H + CAPTIONS_BLOCK_H + 3 * 6 + 6

        def build_caption_preview(scene):
            """Simple free-text caption editor: the whole caption line for the
            scene in one editable box, plus a Reset link back to the
            originally-generated words. Edits are re-split on whitespace and
            realigned against the original words (see realign_captions), so
            an unchanged word keeps its real transcribed timing exactly, and
            only a genuinely added/changed word gets interpolated timing."""
            captions = scene.get("captions", [])
            if not captions:
                return ft.Container(height=CAPTIONS_BLOCK_H)

            # Deep-copied snapshot of the originally-generated words, so Reset
            # always restores this scene's pristine captions even after
            # several edits in the same sitting.
            original_captions = [dict(w) for w in captions]
            original_text = " ".join(w["word"].strip() for w in original_captions)

            caption_field = ft.TextField(
                value=original_text, width=INNER_W,
                multiline=True, min_lines=3, max_lines=3,
                text_size=14, color=TEXT,
                bgcolor=BG, border_color=BORDER, focused_border_color=SAGE,
                border_radius=8, content_padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            )

            def commit_text(e):
                new_words = (caption_field.value or "").split()
                if not new_words:
                    # An emptied-out box keeps the current captions rather
                    # than committing blank, since no caption words would
                    # break the subtitle rendering.
                    caption_field.value = " ".join(w["word"].strip() for w in scene["captions"])
                    page.update()
                    return
                old = scene.get("captions") or original_captions
                scene["captions"] = realign_captions(old, new_words)

            caption_field.on_blur = commit_text
            caption_field.on_submit = commit_text

            def on_reset(e):
                scene["captions"] = [dict(w) for w in original_captions]
                caption_field.value = original_text
                page.update()

            return ft.Column(
                spacing=6,
                controls=[
                    ft.Row(
                        height=CAPTION_HEADER_H,
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Text("Captions", size=12, color=SAGE, weight=ft.FontWeight.W_500),
                            ft.TextButton(
                                "Reset", on_click=on_reset,
                                style=ft.ButtonStyle(color=TEXT_MUTED, padding=0),
                            ),
                        ],
                    ),
                    ft.Container(content=caption_field, height=CAPTION_BOX_H, width=INNER_W),
                    ft.Container(
                        height=CAPTION_NOTE_H,
                        content=ft.Text(
                            "Words stay lined up with the timing automatically.",
                            size=10, color=TEXT_MUTED, max_lines=1,
                        ),
                    ),
                ],
            )

        scene_cards = []
        for i, scene in enumerate(project["scenes"]):
            image_preview = (
                ft.Image(
                    src=scene["image_path"], width=INNER_W, height=IMAGE_H,
                    border_radius=6, fit=ft.BoxFit.COVER,
                )
                if scene.get("image_path")
                else ft.Container(width=INNER_W, height=IMAGE_H, bgcolor=BG, border_radius=6)
            )

            captions_block = build_caption_preview(scene)

            scene_cards.append(
                ft.Container(
                    bgcolor=SURFACE, border_radius=10, border=ft.Border.all(0.5, BORDER),
                    padding=CARD_PAD, width=CARD_W, height=CARD_H,
                    content=ft.Column(
                        spacing=6,
                        controls=[
                            image_preview,
                            ft.Text(f"Scene {i + 1}", size=11, color=SAGE),
                            ft.Container(
                                height=NARRATION_H, width=INNER_W,
                                content=ft.Text(
                                    scene["narration"], size=11, color=TEXT_MUTED,
                                    max_lines=3, overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ),
                            captions_block,
                        ],
                    ),
                )
            )

        status_text = ft.Text("", size=13, color=SAGE, visible=False)

        def on_save_click(e):
            project["title"] = title_field.value
            save_project(project, project_dir)
            status_text.value = "Saved."
            status_text.visible = True
            page.update()

        async def on_build_click(e):
            project["title"] = title_field.value
            save_project(project, project_dir)

            log = LogPanel()
            show_screen(building_screen(log))
            try:
                built_path = await run_with_log(log, build_full_video, project_json_path(project_dir))
            except Exception as ex:
                show_screen(build_failed_screen(f"Unexpected error: {ex}", log.lines))
                return

            if not built_path or not os.path.exists(built_path):
                show_screen(build_failed_screen(
                    "The build didn't produce a video. The log below has the actual ffmpeg error.",
                    log.lines,
                ))
                return

            video_path = os.path.abspath(built_path)

            # Remember which file is this project's finished video - the
            # library finds it from here.
            project["video_file"] = os.path.basename(video_path)
            save_project(project, project_dir)

            show_screen(build_success_screen(project, video_path))

        def on_back_click(e):
            show_screen(library_screen(), top_align=True)

        save_button = ft.Button("Save changes", style=ft.ButtonStyle(bgcolor=SURFACE, color=TEXT), on_click=on_save_click)
        build_button = ft.Button(
            content=ft.Row(
                controls=[ft.Text("Build final video", size=14)],
                spacing=8, alignment=ft.MainAxisAlignment.CENTER,
            ),
            style=ft.ButtonStyle(bgcolor=SAGE, color=TEXT_ON_SAGE), height=46,
            on_click=on_build_click,
        )
        back_button = ft.Button("Back to library", style=ft.ButtonStyle(bgcolor=BG, color=TEXT_MUTED), on_click=on_back_click)

        return ft.Container(
            padding=24,
            content=ft.Column(
                spacing=16,
                controls=[
                    back_button,
                    ft.Text(f"{len(project['scenes'])} scenes", size=12, color=TEXT_MUTED),
                    ft.Row(controls=scene_cards, spacing=12, run_spacing=12, wrap=True, vertical_alignment=ft.CrossAxisAlignment.START),
                    ft.Row(controls=[save_button, build_button, status_text], spacing=12),
                ],
            ),
        )

    # --- Build success: shown right after rendering, before the final review ---
    def build_success_screen(project, video_path):
        # Smaller than the library player so the Continue button stays on
        # screen for both 9:16 and 16:9 videos.
        PREVIEW_MAX_W = 640
        PREVIEW_MAX_H = 440
        player_box = make_video_player(video_path, PREVIEW_MAX_W, PREVIEW_MAX_H, autoplay=False)

        def on_continue_click(e):
            show_screen(final_review_screen(project, video_path))

        def on_system_player_click(e):
            open_in_system_player(video_path)

        continue_button = ft.Button(
            content=ft.Row(
                controls=[ft.Text("Continue", size=14), ft.Icon(ft.Icons.ARROW_FORWARD, size=16)],
                spacing=8, alignment=ft.MainAxisAlignment.CENTER,
            ),
            style=ft.ButtonStyle(bgcolor=SAGE, color=TEXT_ON_SAGE), height=46,
            on_click=on_continue_click,
        )

        return ft.Container(
            width=max(player_box.width + 64, 700), padding=32, border_radius=16, bgcolor=BG,
            border=ft.Border.all(0.5, BORDER),
            content=ft.Column(
                spacing=16,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.CHECK_CIRCLE, color=SAGE, size=32),
                    ft.Text("Video built successfully", size=22, weight=ft.FontWeight.W_500, color=TEXT),
                    ft.Text(video_path, size=12, color=TEXT_MUTED, selectable=True),
                    player_box,
                    ft.Row(
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=12,
                        controls=[
                            ft.Button(
                                content=ft.Row(
                                    controls=[ft.Icon(ft.Icons.OPEN_IN_NEW, size=14),
                                              ft.Text("Open in system player", size=13)],
                                    spacing=6,
                                ),
                                style=ft.ButtonStyle(bgcolor=SURFACE, color=TEXT_MUTED),
                                on_click=on_system_player_click,
                            ),
                            continue_button,
                        ],
                    ),
                ],
            ),
        )

    # --- Post-build: play video + editable metadata + upload stub -----
        # --- Post-build: play video + editable metadata + upload to YouTube ---
    def final_review_screen(project, video_path):
        project_dir = os.path.dirname(video_path)

        yt_title_field = ft.TextField(
            value=project["youtube_metadata"].get("youtube_title", ""),
            bgcolor=SURFACE, border_color=BORDER, focused_border_color=SAGE,
            color=TEXT, text_size=13, border_radius=8, height=44, width=735,
        )
        description_field = ft.TextField(
            value=project["youtube_metadata"].get("description", ""),
            multiline=True, min_lines=2, max_lines=6,
            bgcolor=SURFACE, border_color=BORDER, focused_border_color=SAGE, width=735,
            color=TEXT, text_size=13, border_radius=8,
        )
        tags_field = ft.TextField(
            value=", ".join(project["youtube_metadata"].get("tags", [])),
            bgcolor=SURFACE, border_color=BORDER, focused_border_color=SAGE,
            multiline=True, min_lines=2, max_lines=4,
            color=TEXT, text_size=13, border_radius=8, width=735,
        )

        TITLE_LIMIT = 100  # YouTube's title limit
        title_count = ft.Text("", size=11, color=TEXT_MUTED)

        def update_title_count(e=None):
            length = len(yt_title_field.value or "")
            title_count.value = f"{length} / {TITLE_LIMIT} characters"
            title_count.color = ft.Colors.RED_300 if length > TITLE_LIMIT else TEXT_MUTED
            if e is not None:
                page.update()

        yt_title_field.on_change = update_title_count
        update_title_count()

        status_text = ft.Text("", size=13, color=SAGE, visible=False)
        upload_status = ft.Text("", size=12, color=TEXT_MUTED, visible=False)
        upload_log = LogPanel(width=735, height=120)
        upload_log.control.visible = False
        link_text = ft.Text("", size=13, color=SAGE, selectable=True, visible=False)

        # Visibility chips (same style as the duration chips on the new-project screen).
        privacy = {"value": "private"}
        privacy_chips = {}

        def style_privacy_chips():
            for value, chip in privacy_chips.items():
                selected = privacy["value"] == value
                chip.bgcolor = SAGE if selected else "transparent"
                chip.border = ft.Border.all(1, SAGE if selected else BORDER)
                chip.content.color = TEXT_ON_SAGE if selected else TEXT_MUTED

        def on_privacy_click(value):
            def handler(e):
                privacy["value"] = value
                style_privacy_chips()
                page.update()
            return handler

        chip_row_controls = []
        for value in ("private", "unlisted", "public"):
            chip = ft.Container(
                content=ft.Text(value.capitalize(), size=13),
                padding=ft.Padding.symmetric(horizontal=14, vertical=6),
                border_radius=20,
                on_click=on_privacy_click(value),
            )
            privacy_chips[value] = chip
            chip_row_controls.append(chip)
        style_privacy_chips()

        def collect_metadata():
            project["youtube_metadata"]["youtube_title"] = yt_title_field.value
            project["youtube_metadata"]["description"] = description_field.value
            project["youtube_metadata"]["tags"] = [t.strip() for t in tags_field.value.split(",") if t.strip()]
            save_project(project, project_dir)

        def on_back_click(e):
            show_screen(library_screen(), top_align=True)

        def on_save_metadata_click(e):
            collect_metadata()
            status_text.value = "Metadata saved."
            status_text.visible = True
            page.update()

        def on_open_link_click(e):
            url = project.get("youtube_url")
            if url:
                webbrowser.open(url)

        def show_upload_error(message):
            upload_status.value = message
            upload_status.color = ft.Colors.RED_300
            upload_status.visible = True
            upload_button.disabled = False
            page.update()

        async def on_upload_click(e):
            title = (yt_title_field.value or "").strip()
            if upload_video is None:
                show_upload_error("Uploading needs: pip install google-api-python-client "
                                  "google-auth-oauthlib google-auth-httplib2")
                return
            if not title:
                show_upload_error("Enter a YouTube title first.")
                return
            if len(title) > TITLE_LIMIT:
                show_upload_error(f"The title is {len(title)} characters; YouTube allows {TITLE_LIMIT}.")
                return

            collect_metadata()  # upload exactly what's in the boxes, and keep it saved
            meta = project["youtube_metadata"]

            upload_button.disabled = True
            upload_status.visible = False
            upload_log.control.visible = True
            page.update()

            try:
                video_id, url = await run_with_log(
                    upload_log, upload_video, video_path,
                    meta["youtube_title"], meta["description"], meta["tags"], privacy["value"],
                )
            except YouTubeUploadError as ex:
                show_upload_error(f"Upload failed: {ex}")
                return
            except Exception as ex:
                show_upload_error(f"Unexpected error: {ex}")
                return

            project["youtube_video_id"] = video_id
            project["youtube_url"] = url
            save_project(project, project_dir)
            show_uploaded(url)
            page.update()

        back_button = ft.Button("Back to library", style=ft.ButtonStyle(bgcolor=BORDER, color=TEXT), on_click=on_back_click)
        save_meta_button = ft.Button("Save metadata", style=ft.ButtonStyle(bgcolor=SURFACE, color=TEXT), on_click=on_save_metadata_click)
        upload_button = ft.Button(
            content=ft.Row(
                controls=[ft.Icon(ft.Icons.CLOUD_UPLOAD, size=16), ft.Text("Upload to YouTube", size=14)],
                spacing=8, alignment=ft.MainAxisAlignment.CENTER,
            ),
            style=ft.ButtonStyle(bgcolor=SAGE, color=TEXT_ON_SAGE), height=46,
            on_click=on_upload_click,
        )
        open_link_button = ft.Button(
            "Open on YouTube", style=ft.ButtonStyle(bgcolor=SURFACE, color=TEXT),
            visible=False, on_click=on_open_link_click,
        )

        def show_uploaded(url):
            link_text.value = url
            link_text.visible = True
            open_link_button.visible = True
            upload_button.disabled = True
            upload_status.value = "Uploaded. You can change details later in YouTube Studio."
            upload_status.color = SAGE
            upload_status.visible = True

        # A video that was already uploaded shows its link instead of a fresh upload.
        if project.get("youtube_url"):
            show_uploaded(project["youtube_url"])

        return ft.Container(
            width=800, padding=32, border_radius=16, bgcolor=BG,
            border=ft.Border.all(0.5, BORDER),
            content=ft.Column(
                spacing=16,
                controls=[
                    back_button,
                    ft.Divider(color=BORDER),
                    ft.Column(spacing=4, controls=[label("YouTube title"), yt_title_field, title_count]),
                    ft.Column(spacing=4, controls=[label("Description"), description_field]),
                    ft.Column(spacing=4, controls=[label("Tags (comma separated)"), tags_field]),
                    ft.Row(controls=[save_meta_button, status_text], spacing=12),
                    ft.Divider(color=BORDER),
                    ft.Column(
                        spacing=8,
                        controls=[
                            label("Visibility"),
                            ft.Row(controls=chip_row_controls, spacing=8),
                            ft.Text(
                                "Until your Google project passes YouTube's audit, uploads stay "
                                "private whatever you choose here.",
                                size=11, color=TEXT_MUTED,
                            ),
                        ],
                    ),
                    upload_button,
                    upload_status,
                    upload_log.control,
                    ft.Row(controls=[link_text, open_link_button], spacing=12,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
                ],
            ),
        )
    # --- Building / failed screens --------------------------------------
    def building_screen(log: LogPanel):
        return ft.Container(
            width=720, padding=40, border_radius=16, bgcolor=BG,
            border=ft.Border.all(0.5, BORDER),
            content=ft.Column(
                spacing=20, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.ProgressRing(width=44, height=44, stroke_width=4, color=SAGE),
                    ft.Text("Building your video", size=22, weight=ft.FontWeight.W_500, color=TEXT),
                    ft.Text("Rendering each scene, then joining with transitions...",
                            size=14, color=TEXT_MUTED, text_align=ft.TextAlign.CENTER),
                    log.control,
                ],
            ),
        )

    def build_failed_screen(error_message, log_lines=None):
        def on_back_click(e):
            show_screen(review_edit_screen())

        controls = [
            ft.Text("Build failed", size=20, weight=ft.FontWeight.W_500, color=ft.Colors.RED_300),
            ft.Text(error_message, size=13, color=TEXT_MUTED, selectable=True),
        ]
        if log_lines:
            controls += [label("Log"), LogPanel(width=648, height=220, lines=log_lines).control]
        controls.append(
            ft.Button("Back to review", style=ft.ButtonStyle(bgcolor=SAGE, color=TEXT_ON_SAGE), on_click=on_back_click)
        )

        return ft.Container(
            width=720, padding=36, border_radius=16, bgcolor=BG,
            border=ft.Border.all(0.5, BORDER),
            content=ft.Column(spacing=16, controls=controls),
        )

    show_screen(library_screen(), top_align=True)


ft.run(main)