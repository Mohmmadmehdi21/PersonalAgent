import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly"
]

def get_service():
    creds = None

    # 1. token.json check
    if os.path.exists("token.json"):
        print("[OAuth] 'token.json' mila, credentials verify kiye ja rahe hain...")
        try:
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        except Exception as e:
            print(f"[OAuth] token.json corrupt ya invalid tha ({e}). Naya login karenge.")
            creds = None

    # 2. Token refresh ya naya login
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("[OAuth] Token expire ho chuka hai, automatic refresh kiya ja raha hai...")
            try:
                creds.refresh(Request())
                print("[OAuth] Token successfully refresh ho gaya!")
            except Exception as e:
                print(f"[OAuth] Token refresh fail hua ({e}). Re-login karenge.")
                creds = None

        if not creds or not creds.valid:
            if not os.path.exists("client_secret.json"):
                raise FileNotFoundError(
                    "[Error] client_secret.json nahi mila! Google Cloud Console se download karke main folder mein rakhein."
                )

            print("[OAuth] Browser open ho raha hai YouTube authentication ke liye...")
            flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)

            try:
                creds = flow.run_local_server(port=0, open_browser=True)
            except Exception as e:
                print(f"[OAuth] Browser auto-open nahi ho paya ({e}).")
                auth_url, _ = flow.authorization_url(prompt="consent")
                print(f"\n[OAuth] Kripya neeche diye gaye link ko browser mein manually kholein aur login karein:\n{auth_url}\n")
                creds = flow.run_local_server(port=0, open_browser=False)

            with open("token.json", "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            print("[OAuth] Authentication successful! 'token.json' file save ho gayi hai.")

    return build("youtube", "v3", credentials=creds)

def upload(yt, video, thumb, title, desc, tags):
    body = {
        "snippet": {
            "title": title[:100],
            "description": desc[:4900],
            "tags": tags[:15],
            "categoryId": "24"
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False
        },
    }
    print(f"[Upload] Video upload ho raha hai: '{title[:50]}...'")
    res = yt.videos().insert(
        part="snippet,status",
        body=body,
        media_body=MediaFileUpload(video, resumable=True)
    ).execute()
    vid = res["id"]
    privacy = res.get("status", {}).get("privacyStatus")
    if privacy == "private":
        print(
            f"[Upload] YouTube ne video {vid} ko private rakha; "
            "private fallback accept karke aage badh rahe hain."
        )
    elif privacy == "public":
        print(f"[Upload] Video public upload ho gaya! Video ID: {vid}")
    else:
        raise RuntimeError(
            f"YouTube ne video {vid} ka privacy status return nahi kiya "
            f"(privacyStatus={privacy!r}); upload status confirm nahi ho saka."
        )

    if os.path.exists(thumb):
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
            print(f"[Upload] Custom thumbnail set ho gaya!")
        except Exception as e:
            print(f"[Upload] Custom thumbnail set nahi hua ({e}). (Channel phone-verified na ho to custom thumbnail allow nahi hota)")

    return vid
