# YouTube Clip Agent

Source video or channel -> popular source video -> Whisper transcript -> Gemini (free) viral moments -> ffmpeg clips + captions
-> thumbnail -> YouTube par 4 clips/day upload.

## Setup (local)
1. `pip install -r requirements.txt` aur ffmpeg install karo
2. `.env.example` ko `.env` banao, GEMINI_API_KEY daalo (free: aistudio.google.com)
3. Google Cloud: YouTube Data API v3 enable, OAuth Client ID (Desktop) -> `client_secret.json` root mein rakho
4. `sources.txt` mein reusable video URL ya channel URL daalo (CC-BY ya permission wala)
5. `python main.py` (pehli baar browser login hoga, `token.json` ban jayega)

Channel URL dene par agent YouTube ke view-count order mein results dekhta hai,
pehle se process hue videos chhodkar sabse zyada viewed video chunta hai (ek run mein
adhiktam 500 results tak). Har run mein ek source video se `CLIPS_PER_DAY` clips bante hain.
“Viral” yahan total views ke hisaab se hai, views ki recent growth ke hisaab se nahi.

`DRY_RUN=false` hone par clips seedha YouTube par public upload hote hain; scheduling nahi hoti.
YouTube agar API project ya channel restrictions ki wajah se video ko private rakhe, app private
status ko accept karke aage badhega.

## Daily automation (GitHub Actions)
Repo Settings -> Secrets: `GEMINI_API_KEY`, `CLIENT_SECRET_JSON` (file ka content),
`TOKEN_JSON` (local `token.json` ka content). Workflow roz 00:00 IST chalta hai.
Chahe to manually "Run workflow" bhi kar sakte ho.

## Dhyan do
- Naya unverified API project uploads ko private lock kar sakta hai -> Google ka API compliance audit form bharo.
- Quota: ek upload ~1600 units, daily limit 10,000 => 4/day safe.
- Shorts (vertical) ka custom thumbnail Shorts feed mein nahi dikhta. Thumbnail chahiye to `ORIENTATION=horizontal`.
- Description mein source credit auto-add hota hai. Permission ka proof save rakho.
