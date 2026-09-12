# AI Video Generator

A tool that turns a topic and a target duration into a short video complete with an AI-written script, AI-generated images, an AI voiceover, and
synced captions using free APIs so it costs little to nothing to run.


## What it does

1. Writes a script, split into scenes, using Google's Gemini AI.
2. Generates an image for each scene using Pollinations AI.
3. Generates a spoken voiceover for each scene using Microsoft's free text-to-speech.
4. Times out word-by-word captions for each voice line using Whisper.
5. Renders each scene into a short video clip with the image, voice, and captions burned in.
6. Joins all the scene clips into one final video.


## What you need

- Python 3.10 or newer
- FFmpeg (a free video-processing tool) installed on your computer
- A Gemini API key (from Google AI Studio)
- A Pollinations API key (from auth.pollinations.ai)


## Project files

- `generate_video_assets.py` | Writes the script and generates all images, voice, and captions for a topic.
- `render_scene.py` | Turns one scene's image + voice + captions into a video clip.
- `build_full_video.py` | Renders every scene and joins them into one final video.


