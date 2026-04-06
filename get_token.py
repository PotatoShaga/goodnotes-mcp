"""
One-time helper: exchange a Dropbox authorization code for a refresh token.
Run this on your local machine (Python 3.6+, no pip installs needed).

Usage:
    python get_token.py
"""

import urllib.request
import urllib.parse
import json
import sys

print("=" * 60)
print("  Dropbox Refresh Token Helper")
print("=" * 60)
print()

app_key = input("1. Enter your Dropbox App Key: ").strip()
app_secret = input("2. Enter your Dropbox App Secret: ").strip()

print()
print("3. Open this URL in your browser, then click 'Allow':")
print()
print(f"   https://www.dropbox.com/oauth2/authorize?client_id={app_key}&response_type=code&token_access_type=offline")
print()

auth_code = input("4. Paste the authorization code you received: ").strip()

print()
print("Exchanging code for refresh token...")

data = urllib.parse.urlencode({
    "code": auth_code,
    "grant_type": "authorization_code",
    "client_id": app_key,
    "client_secret": app_secret,
}).encode("utf-8")

req = urllib.request.Request("https://api.dropboxapi.com/oauth2/token", data=data, method="POST")

try:
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    body = e.read().decode("utf-8")
    print(f"\nError {e.code}: {body}")
    print("Common causes:")
    print("  - Auth code was already used (each code works once)")
    print("  - App key/secret are wrong")
    print("  - Permissions weren't saved before generating the code")
    sys.exit(1)

print()
print("=" * 60)
print("  SUCCESS! Save these values for Render deployment:")
print("=" * 60)
print()
print(f"  DROPBOX_APP_KEY      = {app_key}")
print(f"  DROPBOX_APP_SECRET   = {app_secret}")
print(f"  DROPBOX_REFRESH_TOKEN = {result['refresh_token']}")
print()
print("These go into Render.com as environment variables.")
print("The refresh token does NOT expire — keep it safe.")
