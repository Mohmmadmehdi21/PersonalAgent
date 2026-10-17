import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.http import MediaFileUpload
from retry import (retry_upload, QuotaExceededError, AuthError,
                   AccountUploadLimitError, NonRetryableAPIError,
                   ExternalWriteOutcomeUnknownError, UploadCommittedInterruptedError,
                   build_google_service, call_google_write)
from logger import log, log_warn, log_error, mask_secrets

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
            print(f"[OAuth] token.json corrupt ya invalid tha ({mask_secrets(e)}). Naya login karenge.")
            creds = None

    # 2. Token refresh ya naya login
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("[OAuth] Token expire ho chuka hai, automatic refresh kiya ja raha hai...")
            try:
                creds.refresh(Request())
                print("[OAuth] Token successfully refresh ho gaya!")
            except Exception as e:
                print(f"[OAuth] Token refresh fail hua ({mask_secrets(e)}). Re-login karenge.")
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
                print(f"[OAuth] Browser auto-open nahi ho paya ({mask_secrets(e)}).")
                auth_url, _ = flow.authorization_url(prompt="consent")
                print(f"\n[OAuth] Kripya neeche diye gaye link ko browser mein manually kholein aur login karein:\n{auth_url}\n")
                creds = flow.run_local_server(port=0, open_browser=False)

            with open("token.json", "w", encoding="utf-8") as f:
                f.write(creds.to_json())
            print("[OAuth] Authentication successful! 'token.json' file save ho gayi hai.")

    return build_google_service("youtube", "v3", creds)

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
    def _execute_insert():
        return yt.videos().insert(
            part="snippet,status",
            body=body,
            media_body=MediaFileUpload(video, resumable=True)
        ).execute()

    try:
        res = retry_upload(_execute_insert)
    except (AccountUploadLimitError, NonRetryableAPIError) as e:
        msg = "[Quota Limit] Aaj ki upload limit poori ho chuki hai. Clips local 'work/' folder mein saved hain."
        print(f"\n{msg}\n")
        log_warn(msg)
        raise
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
            call_google_write(
                lambda: yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute(),
                task=f"Thumbnail set {vid}",
            )
            print(f"[Upload] Custom thumbnail set ho gaya!")
        except KeyboardInterrupt as e:
            raise UploadCommittedInterruptedError(vid) from e
        except ExternalWriteOutcomeUnknownError as e:
            if "interrupted" in str(e).lower():
                raise UploadCommittedInterruptedError(vid) from e
            print(f"[Upload] Thumbnail result unknown for confirmed video {vid}: {mask_secrets(e)}")
        except Exception as e:
            print(f"[Upload] Custom thumbnail set nahi hua ({mask_secrets(e)}). (Channel phone-verified na ho to custom thumbnail allow nahi hota)")

    return vid
