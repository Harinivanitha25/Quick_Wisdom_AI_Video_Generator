"""Upload a finished video to YouTube through the YouTube Data API v3.

Setup (once): put client_secret.json (the OAuth "Desktop app" client from
Google Cloud Console) next to this file. On the first upload a browser window
opens to log in and approve access; the login is then saved in token.json so
later uploads don't ask again.

Keep client_secret.json and token.json private - together they give access to
your channel.

Test on its own (uploads your newest project's video as PRIVATE, after asking):
    python youtube_upload.py
"""
import glob
import json
import os
import random
import time

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# Anchored to this file's folder, not the launch folder.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET_FILE = os.path.join(BASE_DIR, "client_secret.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")

# Upload-only permission: the app can add videos but can't read or delete
# anything else on the channel.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

PRIVACY_OPTIONS = ("private", "unlisted", "public")
TITLE_LIMIT = 100
DESCRIPTION_LIMIT = 4900  # YouTube's cap is 5000 bytes; leave headroom for non-ASCII
TAGS_CHAR_LIMIT = 480     # YouTube's cap is 500 characters across all tags
DEFAULT_CATEGORY_ID = "28"  # Science & Technology ("22" = People & Blogs)

CHUNK_SIZE = 8 * 1024 * 1024  # 8 MB per resumable chunk
MAX_CHUNK_RETRIES = 5
RETRYABLE_STATUSES = {500, 502, 503, 504}


class YouTubeUploadError(Exception):
    """A problem with a plain-English message that's safe to show in the UI."""


def get_credentials():
    """Saved login if it's still good, otherwise a browser login."""
    if not os.path.exists(CLIENT_SECRET_FILE):
        raise YouTubeUploadError(
            "client_secret.json was not found next to youtube_upload.py. "
            "Download the OAuth Desktop client file from Google Cloud Console, "
            "rename it to client_secret.json, and put it in the app folder."
        )

    creds = None
    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        except (ValueError, OSError):
            creds = None  # unreadable token file - just log in again

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except RefreshError:
            # Apps still in "Testing" mode have their login expire after about
            # a week, and users can revoke access - fall through to a fresh login.
            print("Saved YouTube login expired - logging in again...")

    print("Opening your browser to log in to YouTube...")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as ex:
        raise YouTubeUploadError(
            f"YouTube login didn't finish ({ex}). If the browser said access was "
            "blocked, add your Google account as a Test user on the OAuth consent screen."
        )
    _save_token(creds)
    return creds


def _save_token(creds):
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def _clean_text(text):
    # YouTube rejects < and > in titles, descriptions and tags.
    return (text or "").replace("<", "").replace(">", "").strip()


def _prepare_tags(tags):
    cleaned, total = [], 0
    for tag in tags or []:
        tag = _clean_text(tag)
        if not tag:
            continue
        if total + len(tag) + 1 > TAGS_CHAR_LIMIT:
            break
        cleaned.append(tag)
        total += len(tag) + 1
    return cleaned


def _friendly_http_error(err):
    """Turn a Google API error into a message a person can act on."""
    reason = ""
    try:
        details = json.loads(err.content.decode("utf-8"))
        reason = details["error"]["errors"][0].get("reason", "")
    except Exception:
        pass

    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        return "YouTube's daily upload limit for this project has been reached. Try again tomorrow."
    if reason == "uploadLimitExceeded":
        return "This YouTube channel has hit its own upload limit. Try again later."
    if reason in ("forbidden", "insufficientPermissions", "youtubeSignupRequired"):
        return ("YouTube refused the upload. Check that the Google account you logged in with "
                "has a YouTube channel, and that YouTube Data API v3 is enabled for the project.")
    if reason == "authError" or err.resp.status == 401:
        return "YouTube login is no longer valid. Delete token.json and try again to log in fresh."
    if reason in ("invalidTitle", "invalidDescription", "invalidTags"):
        return f"YouTube rejected the video details ({reason}). Check the title, description and tags."
    return f"YouTube returned an error ({err.resp.status}{', ' + reason if reason else ''})."


def upload_video(video_path, title, description="", tags=None, privacy="private",
                 category_id=DEFAULT_CATEGORY_ID):
    """Upload one video. Returns (video_id, url). Raises YouTubeUploadError with
    a readable message on any problem. Progress is print()ed, so when this is
    run through the app's run_with_log it shows up live in the UI."""
    if not video_path or not os.path.exists(video_path):
        raise YouTubeUploadError(f"Video file not found: {video_path}")
    if privacy not in PRIVACY_OPTIONS:
        raise YouTubeUploadError(f"Privacy must be one of {', '.join(PRIVACY_OPTIONS)}.")

    title = _clean_text(title)
    if not title:
        raise YouTubeUploadError("The YouTube title is empty.")
    if len(title) > TITLE_LIMIT:
        raise YouTubeUploadError(
            f"The YouTube title is {len(title)} characters; the limit is {TITLE_LIMIT}."
        )

    creds = get_credentials()
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title,
            "description": _clean_text(description)[:DESCRIPTION_LIMIT],
            "tags": _prepare_tags(tags),
            "categoryId": str(category_id),
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(video_path, mimetype="video/*", chunksize=CHUNK_SIZE, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    print(f"Uploading to YouTube ({size_mb:.1f} MB, {privacy})...")

    response, retries, last_percent = None, 0, -1
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                percent = int(status.progress() * 100)
                if percent != last_percent:
                    print(f"  Upload progress: {percent}%")
                    last_percent = percent
            retries = 0
        except HttpError as err:
            if err.resp.status in RETRYABLE_STATUSES and retries < MAX_CHUNK_RETRIES:
                retries += 1
                delay = 2 ** retries + random.uniform(0, 1)
                print(f"  YouTube busy ({err.resp.status}), retrying in {delay:.1f}s "
                      f"(attempt {retries}/{MAX_CHUNK_RETRIES})...")
                time.sleep(delay)
                continue
            raise YouTubeUploadError(_friendly_http_error(err))
        except (ConnectionError, TimeoutError, OSError) as ex:
            if retries < MAX_CHUNK_RETRIES:
                retries += 1
                delay = 2 ** retries + random.uniform(0, 1)
                print(f"  Connection problem ({type(ex).__name__}), retrying in {delay:.1f}s "
                      f"(attempt {retries}/{MAX_CHUNK_RETRIES})...")
                time.sleep(delay)
                continue
            raise YouTubeUploadError(f"Lost connection while uploading: {ex}")

    video_id = response.get("id")
    if not video_id:
        raise YouTubeUploadError("YouTube accepted the upload but didn't return a video ID.")

    url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"Upload complete: {url}")
    return video_id, url


if __name__ == "__main__":
    # Safe manual test: newest project, uploaded as PRIVATE, only after you type y.
    projects = sorted(glob.glob(os.path.join(BASE_DIR, "project_output", "*", "project.json")))
    if not projects:
        raise SystemExit("No projects found in project_output/.")

    project_json = projects[-1]
    project_dir = os.path.dirname(project_json)
    with open(project_json, encoding="utf-8") as f:
        project = json.load(f)

    video_file = project.get("video_file")
    if video_file and os.path.exists(os.path.join(project_dir, video_file)):
        video_path = os.path.join(project_dir, video_file)
    else:
        mp4s = sorted(p for p in glob.glob(os.path.join(project_dir, "*.mp4")))
        if not mp4s:
            raise SystemExit(f"No finished video found in {project_dir}.")
        video_path = mp4s[0]

    meta = project.get("youtube_metadata") or {}
    test_title = meta.get("youtube_title") or project.get("title", "")

    print("About to upload as PRIVATE:")
    print("  File:  ", video_path)
    print("  Title: ", test_title, f"({len(test_title)} chars)")
    if input("Type y to upload: ").strip().lower() != "y":
        raise SystemExit("Cancelled.")

    try:
        upload_video(
            video_path, test_title, meta.get("description", ""),
            meta.get("tags", []), privacy="private",
        )
    except YouTubeUploadError as ex:
        print("Upload failed:", ex)
