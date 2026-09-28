# AI Video Generator

A desktop app that turns a topic and a target duration into a short video complete with an AI-written script, AI-generated images, an AI voiceover, and synced captions. You can review and edit everything before the final render, then upload the finished video to YouTube straight from the app.

## What it does

1. Writes a script, split into scenes, using Google's Gemini AI. You can edit the narration and image descriptions before anything expensive runs.
2. Generates an image for each scene using Pollinations AI.
3. Generates a spoken voiceover for each scene using Microsoft's free text-to-speech.
4. Times word-by-word captions for each voice line using Whisper.
5. Lets you review every scene and correct any caption word in a simple text box. Unchanged words keep their exact timing.
6. Renders each scene into a short video clip with the image, voice, and captions burned in, with a short pause after each scene.
7. Joins all the scene clips into one final video with crossfade transitions and a watermark.
8. Generates YouTube metadata (title with hashtags, description, tags), which you can edit, and uploads the video to your channel.


## What you need

- Python 3.10 or newer
- FFmpeg (a free video-processing tool) installed on your computer and available on your PATH
- A Gemini API key (from Google AI Studio)
- A Pollinations API key (from auth.pollinations.ai)
- Python packages: `flet`, `edge-tts`, `faster-whisper`, `requests`, `Pillow`
- Optional: `flet-video` for playing videos inside the app (without it, the player shows a message and an "open in system player" button)
- Optional, for YouTube upload: `google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2`

The Whisper model is downloaded automatically the first time captions are generated.


## The app screens

- **Library** lists every finished video with its first scene as the thumbnail. Each card has buttons to play the video, edit its YouTube details, and delete the project with all its files.
- **New project** takes a topic, a duration (type seconds or pick a preset), and a frame size: 9:16 for Shorts or 16:9 for regular videos.
- **Script review** shows the generated script so you can edit it before images, voice, and captions are generated.
- **Generating and building screens** show the same progress messages the command line prints (script, images, voice, captions, scene rendering, errors, retries) in a live log box.
- **Scene review** shows one card per scene with its image, narration, and an editable captions box.
- **Build success** confirms the finished video and its location.
- **Final review** lets you edit the YouTube title (with a live character counter out of 100), description, and tags, then upload.

### Project folders

Every project gets its own folder, so a new project never overwrites an earlier one:

```
project_output/<date>_<time>_<title>/
    images/        scene images
    audio/         scene voiceovers
    scenes/        rendered scene clips
    project.json   script, captions, YouTube details, upload link
    <title>.mp4    the final video
```

## Uploading to YouTube

Uploading uses the YouTube Data API v3.

1. In Google Cloud Console, create a project and enable **YouTube Data API v3**.
2. Set up the OAuth consent screen as an External app and add your own Google account as a test user.
3. Create an OAuth client ID of type **Desktop app**, download the JSON file, rename it `client_secret.json`, and put it next to `app.py`.
4. Click **Upload to YouTube** on the final review screen. The first time, a browser window opens so you can log in and approve access. The login is saved in `token.json` for next time.


## Project files

- `app.py` | The desktop app: library, project creation, script and scene review, caption editing, building, and YouTube details and upload.
- `generate_video_assets.py` | Writes the script and YouTube metadata, and generates all images, voice, and captions for a topic.
- `render_scene.py` | Turns one scene's image + voice + captions into a video clip, with an optional pause at the end.
- `build_full_video.py` | Renders every scene, joins them with transitions, and adds the watermark.
- `youtube_upload.py` | Logs in to YouTube and uploads a finished video.
- `make_icon.py` | Turns `logo.png` into `logo.ico` for the desktop shortcut.
- `config.py` | Your API keys (you create this; keep it private).
- `assets/watermark.png` | The watermark image (optional).