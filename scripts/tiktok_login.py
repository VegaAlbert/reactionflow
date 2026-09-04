"""
Step 1 of testing the TikTok integration: log in as your TikTok account (e.g. @vegzz)
and get an access token.

What this does:
  1. Builds the TikTok OAuth authorization URL for your app (ReactionFlow).
  2. Opens it in your browser.
  3. You log in on TikTok and approve the requested permissions.
  4. TikTok redirects your browser to the Netlify callback page, which exchanges the
     code for tokens and shows them on screen.
  5. You copy those tokens into scripts/.env (see .env.example) before running tiktok_post.py.

Requirements: pip install -r requirements.txt
Fill in scripts/.env first (copy .env.example -> .env and fill TIKTOK_CLIENT_KEY / TIKTOK_REDIRECT_URI).
"""
import os
import secrets
import urllib.parse
import webbrowser

from dotenv import load_dotenv

load_dotenv()

CLIENT_KEY = os.environ["TIKTOK_CLIENT_KEY"]
REDIRECT_URI = os.environ["TIKTOK_REDIRECT_URI"]

# Scopes needed for this test: read basic profile info + upload/publish a video directly.
SCOPES = "user.info.basic,video.publish,video.upload"

state = secrets.token_urlsafe(16)

params = {
    "client_key": CLIENT_KEY,
    "response_type": "code",
    "scope": SCOPES,
    "redirect_uri": REDIRECT_URI,
    "state": state,
}

auth_url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode(params)

print("Opening this URL in your browser (log in with the TikTok account you added as a")
print("Sandbox target user, e.g. @vegzz):\n")
print(auth_url)
print()
print(f"(state used for this request, just for your reference: {state})")

webbrowser.open(auth_url)
