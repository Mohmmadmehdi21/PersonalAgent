import os, sys
from upload import get_service

def test_auth():
    print("=" * 60)
    print("[Test Auth] YouTube OAuth Authentication Test")
    print("=" * 60)

    # 1. client_secret.json check
    if not os.path.exists("client_secret.json"):
        print("\n[Error] 'client_secret.json' nahi mila!")
        print("Google Cloud Console se OAuth Client ID (Desktop app) download karke")
        print("project root ('K:\\PersonalAgent\\client_secret.json') mein rakhein.\n")
        sys.exit(1)

    print("[Test Auth] 'client_secret.json' maujood hai.")

    # 2. Authenticate
    try:
        yt = get_service()
        print("\n[Test Auth] YouTube service successfully initialized!")

        # 3. Channel Info check (agar scope allow kare)
        try:
            res = yt.channels().list(part="snippet", mine=True).execute()
            items = res.get("items", [])
            if items:
                channel_title = items[0]["snippet"]["title"]
                print(f"[Test Auth] Connected Channel: '{channel_title}'")
            else:
                print("[Test Auth] Channel list response received (No public channel snippet found).")
        except Exception as e:
            print(f"[Test Auth] Channel info note: {e}")

        # 4. Verify token.json
        if os.path.exists("token.json"):
            print("\n" + "=" * 60)
            print("[SUCCESS] 'token.json' successfully generate/verify ho chuka hai!")
            print("Ab aap bina browser khule direct videos upload aur schedule kar sakte hain.")
            print("=" * 60)
        else:
            print("\n[Warning] Authentication complete hui par 'token.json' file nahi mili.")

    except Exception as e:
        print(f"\n[Error] Authentication fail ho gayi: {e}")
        sys.exit(1)

if __name__ == "__main__":
    test_auth()
