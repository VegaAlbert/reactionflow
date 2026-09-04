"""
Step 2 of testing the TikTok integration: publish a local video file directly to your
TikTok profile using the Content Posting API (Direct Post), via the FILE_UPLOAD flow.

Requirements: pip install -r requirements.txt
Fill in scripts/.env first, including TIKTOK_ACCESS_TOKEN (from tiktok_login.py + the
Netlify callback page).

Usage:
    python tiktok_post.py "../test/test_video.mp4"
"""
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.environ["TIKTOK_ACCESS_TOKEN"]

INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"


def post_video(video_path: str, title: str = "ReactionFlow test post"):
    video_size = os.path.getsize(video_path)

    # While the app is not audited yet (Sandbox / unaudited Production), TikTok only allows
    # posting with privacy_level = SELF_ONLY (private, visible only to you). You will need to
    # manually change it to public afterwards, or wait until the app passes TikTok's audit.
    init_body = {
        "post_info": {
            "title": title,
            "privacy_level": "SELF_ONLY",
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": video_size,
            "total_chunk_count": 1,
        },
    }

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    print("1) Initializing upload...")
    init_res = requests.post(INIT_URL, json=init_body, headers=headers, timeout=30)
    print("STATUS:", init_res.status_code, "BODY:", init_res.text)
    init_res.raise_for_status()
    init_data = init_res.json()
    print(init_data)

    error = init_data.get("error", {})
    if error.get("code") not in (None, "ok"):
        raise RuntimeError(f"init failed: {init_data}")

    publish_id = init_data["data"]["publish_id"]
    upload_url = init_data["data"]["upload_url"]

    print("2) Uploading video bytes...")
    with open(video_path, "rb") as f:
        video_bytes = f.read()

    upload_headers = {
        "Content-Type": "video/mp4",
        "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
    }
    upload_res = requests.put(upload_url, data=video_bytes, headers=upload_headers, timeout=120)
    print(f"upload status: {upload_res.status_code}")
    upload_res.raise_for_status()

    print(f"3) Done. publish_id = {publish_id}")
    print("   Checking status...")
    status_res = requests.post(
        STATUS_URL, json={"publish_id": publish_id}, headers=headers, timeout=30
    )
    print(status_res.json())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tiktok_post.py <path-to-video.mp4> [title]")
        sys.exit(1)
    video_path = sys.argv[1]
    title = sys.argv[2] if len(sys.argv) > 2 else "ReactionFlow test post"
    post_video(video_path, title)
