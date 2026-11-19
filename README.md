# PeakCut

Turn long videos and live streams into captioned vertical clips, ready to post. Free and open source: run it on
your own computer or server, with your own AI key.

Paste a YouTube, Twitch or Kick link (or upload a file) and PeakCut finds the moments worth clipping, cuts them to
9:16, adds word-by-word captions and a hook, and can post them to your connected accounts. It can also record a
creator's live stream and clip the highlights while the stream is still running.

## What it does

- **Clips from any video.** Transcribes the audio, asks an AI which moments would hold a stranger's attention, and
  renders each one with captions in one of three styles.
- **Live clipping.** Records a stream into a rolling buffer and clips as it happens, using loudness, chat activity
  and the transcript.
- **Workspaces per creator.** Channels, clip settings, posting rules and an activity log for each creator you clip.
- **Posting.** Connect YouTube, Instagram and TikTok in one click, then queue or auto-post finished clips, with
  spacing and daily caps (see [DEPLOY.md](DEPLOY.md#connecting-youtube-instagram-and-tiktok) for the one-time setup).
- **No sign-in on your own computer.** It opens straight into the dashboard. On a shared server, switch accounts on
  (`PEAKCUT_ACCOUNTS=1`) for email or Google sign-in and an admin dashboard.
- **Your own AI.** Gemini, OpenAI, Grok, Groq, OpenRouter, Claude, a local model through Ollama, or anything that
  speaks the OpenAI chat format. No key at all also works: clips are then picked by loudness.

## PeakCut Pro Features (Hinglish Gaming Shorts Pipeline)

PeakCut Pro extends PeakCut into an end-to-end production studio specifically built for Hinglish gaming YouTube Shorts (GTA 6 / Vice City, high-energy streams and gameplay). It turns raw gameplay moments into original, monetisation-safe vertical Shorts through a 5-phase studio workflow:

### 1. Source & Permission Manager
- **Rights & Credit Tracking**: Store source video URL, creator name, custom credit text, and permission status per video.
- **Proof Storage**: Upload screenshot proof or notes documenting permission directly into the job record (`/api/jobs/{jid}/permission/proof`).
- **Automated Credits**: Automatically creates a formatted credit line for descriptions and an on-screen credit subtitle displayed during the last 2 seconds of the Short.
- **Hard Permission Gating**: Posting to YouTube, Instagram, or TikTok is completely blocked while *"Permission confirmed"* is unchecked.

### 2. Frame-Aware Commentary Writer
- **Multimodal AI Analysis**: Downscales and extracts 1 frame every 2 seconds alongside the transcript, sending both to vision-capable models (with automatic transcript fallback if vision is unavailable).
- **Strict Structured Scripts**: Outputs hook, timed commentary lines `[{start, end, text}]`, outro, catchy title, description, and hashtags.
- **Tone & Persona Controls**: Select tones (*Hype*, *Funny*, *Roast*, *Informative*, *Mystery*) and paste a 3-line channel voice sample to match your persona.
- **Hinglish Style Toggle**: Choose between default Devanagari Hindi + Latin English (e.g., *"Bhai log ye drift check karo!"*) or full Roman Hinglish.
- **Pacing & Auto-Retry**: Automatically calculates speaking pace (default 2.3 words/sec) to fit the exact clip duration, auto-retrying if generated text is too long.
- **3 Variants & Live Editing**: Generates 3 unique commentary variants per clip with line-by-line script editing in the dashboard.
- **Fact Integrity**: Restricts AI to what is visible or heard on screen; flags uncertain details with `[CHECK]`.
- **Safe-Start Violence Detector**: Scans the first 7 seconds and cover frame for graphic violence, warning you and suggesting an alternate start point.

### 3. Voiceover Studio (My Voice by Default)
- **Interactive Teleprompter**: Synchronized video playback with volume control and live script line highlighting in time with timestamps.
- **In-Browser Microphone Recording**: Record full takes or punch in line-by-line using browser `MediaRecorder` with a 3-2-1 countdown.
- **FFmpeg Audio Mastering**: Server-side audio processing pipeline applying band-pass filtering (80Hz–8kHz), noise reduction (`afftdn`), and EBU R128 loudness normalisation (`loudnorm` -14 LUFS, -1.5 dB True Peak) into a clean 44.1kHz mono WAV master.
- **Audio File Import**: Upload external microphone recordings or edited voice tracks (WAV, MP3, M4A, AAC, FLAC).
- **Optional AI TTS Generator**: Off by default (labelled *"AI voice: higher monetisation risk"*); supports free Google Voice and OpenAI `tts-1` (`alloy`, `echo`, `nova`, etc.).

### 4. Mixer & Renderer
- **Sidechain Audio Ducking**: Dynamically compresses and ducks the game audio under your voiceover track (`sidechaincompress` + `amix`), keeping game ambience and SFX clearly audible.
- **Hook Title Duration**: Hook text is strictly displayed for the first 2.0 seconds with smooth fade effects, keeping viewers hooked without cluttering the screen.
- **Vertical 9:16 Standard**: Renders 1080x1920 (9:16), H.264 + AAC, strictly capped at 60 seconds max.
- **Word-by-Word Voiceover Captions**: Uses local word-level timestamps with automatic rotating caption styles (`bounce` -> `hype` -> `clean`).
- **Originality Checklist**: Evaluates each clip across 3 criteria:
  1. Voiceover track present.
  2. Voiceover covers at least 40% of duration (`PEAKCUT_MIN_VOICEOVER_COVERAGE`).
  3. Non-identical edit layout.
  Pass/fail badge displayed on thumbnail cards and clip modal.

### 5. Review Queue & Metadata
- **Review Before Posting**: Default is review-first (`autopost: false`). Auto-posting triggers only when source permission is confirmed, originality passes, and no duplicate is detected.
- **Duplicate-Moment Detection**: Tracks source identity and time ranges across clips to prevent posting the same gameplay moment twice (flags overlap >= 50% or >= 10s).
- **Editable Metadata**: Edit Title, Description (with credit line and source URL), and Hashtags (#tag) in the clip editor without re-rendering the video (`PUT /api/jobs/{jid}/clips/{cid}/metadata`).
- **Daily Caps & Post Spacing**: Enforces minimum gap between posts and daily upload caps per account.

---

## 10-Step Quick Start for Windows

Follow these 10 steps to run PeakCut Pro on Windows:

1. **Install Python 3.10+**  
   Download from [python.org](https://www.python.org/downloads/windows/). During installation, **check "Add Python to PATH"**.

2. **Install FFmpeg**  
   Open PowerShell or Command Prompt as Administrator and run:
   ```powershell
   winget install Gyan.FFmpeg
   ```
   *(Or download from [gyan.dev/ffmpeg/builds](https://www.gyan.dev/ffmpeg/builds/) and add its `bin` folder to your system PATH).*

3. **Clone PeakCut Pro to a short path**  
   Use a short folder path like `C:\peakcut` to avoid Windows 260-character path limits:
   ```cmd
   git clone https://github.com/haziqahmedqasmi/PeakCut.git C:\peakcut
   ```

4. **Navigate to the directory**  
   ```cmd
   cd C:\peakcut
   ```

5. **Create and activate a virtual environment**  
   ```cmd
   python -m venv .venv
   .venv\Scripts\activate
   ```

6. **Install dependencies**  
   ```cmd
   pip install -r requirements.txt
   ```

7. **Configure environment settings**  
   Copy the example environment file:
   ```cmd
   copy .env.example .env
   ```
   Open `.env` in Notepad and configure your AI key (e.g., `PEAKCUT_AI_PROVIDER=gemini` and `PEAKCUT_AI_KEY=your_key`), or leave it blank to configure directly in the web UI.

8. **Start PeakCut Pro**  
   ```cmd
   python -m studio.server
   ```
   *(The first run downloads the local speech model ~480 MB automatically).*

9. **Open the web studio**  
   Open your browser to:
   ```
   http://127.0.0.1:8765
   ```

10. **Clip, Write, Record & Post**  
    - Paste a GTA 6 or gaming stream link (or upload gameplay video).
    - Under **Permissions**, enter the creator's name and confirm permission.
    - Click **Generate Commentary** in the clip editor to create 3 Hinglish script variants.
    - Tap **Teleprompter & Record** to record your voiceover with your microphone.
    - Check the **Originality Badge** (Pass), verify title/hashtags, and post or queue your Short!

---

## Quick start (macOS & Linux)

You need Python 3.10 or newer and `ffmpeg`. On a Mac: `brew install ffmpeg`. On Linux: `sudo apt install ffmpeg`. Put PeakCut in a short folder: `~/peakcut`.

```bash
git clone https://github.com/haziqahmedqasmi/PeakCut.git ~/peakcut
cd ~/peakcut
pip install -r requirements.txt
python -m studio.server
```

Open http://127.0.0.1:8765. There is no sign-in: it opens straight into the dashboard. Go to **Settings, AI**, choose your AI service, paste your key and press **Save and test**.

## Choose your AI

PeakCut needs a chat model that can return JSON. The easy way is in the dashboard: **Settings, AI**. Pick the
service, paste your key, press **Find models** to choose from the service's own list, then **Save and test**. The
key is kept in the data folder on your machine and is never shown again.

If you prefer a file (or run a server), the same three things can go in `.env`. What is saved in the dashboard wins:

```ini
PEAKCUT_AI_PROVIDER=gemini
PEAKCUT_AI_MODELS=gemini-2.5-flash
PEAKCUT_AI_KEY=your-key-here
```

| `PEAKCUT_AI_PROVIDER` | Service | Where to get a key |
|---|---|---|
| `gemini` | Google Gemini | https://aistudio.google.com/apikey |
| `openai` | OpenAI | https://platform.openai.com/api-keys |
| `xai` | Grok (xAI) | https://console.x.ai |
| `groq` | Groq | https://console.groq.com/keys |
| `openrouter` | OpenRouter (many models, one key) | https://openrouter.ai/keys |
| `anthropic` | Claude | https://console.anthropic.com |
| `ollama` | A model on your own machine, no key | https://ollama.com |
| `custom` | Any OpenAI-compatible address: also set `PEAKCUT_AI_BASE_URL` | |

- `PEAKCUT_AI_MODELS` is the model id exactly as the service names it. Model names change often, so check the
  service's current list. Several ids separated by commas are tried in order, which gives you a fallback.
- A fast, inexpensive model is the right choice. A one-hour video is read in about four requests.
- You pay the AI service directly for what you use. The **AI usage** page in the app shows how many requests and
  tokens each video took.
- Speech to text does not use the AI service. It runs on your own machine, so audio never leaves it.

## Settings

Everything is set through environment variables, normally in a `.env` file next to this README.
[`.env.example`](.env.example) lists every setting with a short explanation. The ones that matter first:

| Setting | What it is for |
|---|---|
| `PEAKCUT_AI_PROVIDER`, `PEAKCUT_AI_MODELS`, `PEAKCUT_AI_KEY` | The AI that picks clips and writes titles (or set it in the dashboard) |
| `PEAKCUT_ACCOUNTS` | `1` makes everyone sign in. Use it on any server other people can reach |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Optional: Sign in with Google, and connecting YouTube |
| `PEAKCUT_SMTP_*`, `PEAKCUT_MAIL_FROM` | Optional: confirmation and password emails |
| `PEAKCUT_DATA` | Where videos, clips and the database are kept (default `./data`) |

`.env`, the `secrets/` folder and all user data are ignored by git. Keep keys there, never in the code.

## Admins

This only matters with accounts switched on (`PEAKCUT_ACCOUNTS=1`); without them you are the owner and can do
everything. An account is an admin only when the database says so. No email address is special in the code and no web request
can change a role. Do it on the machine that runs PeakCut:

```bash
python -m studio.admin grant you@example.com
```

`revoke` and `list` work the same way. The admin dashboard is at `/admin` and answers "not found" to everyone else.

## How it is built

| Part | What it is |
|---|---|
| `studio/` | The server: FastAPI, the clip pipeline, live recording, accounts |
| `studio/ai.py` | The only file that talks to an AI service. Add a provider here |
| `web/` | The pages: landing, sign-in, the app, the admin dashboard (plain HTML, CSS and JavaScript, no build step) |
| `tests/` | Tests that run against a throwaway copy of the app |
| `deploy/`, `Dockerfile`, `docker-compose.yml` | One-server deployment with PostgreSQL and automatic HTTPS |

Video work is done with `ffmpeg` and `yt-dlp`. Speech is transcribed with `sherpa-onnx` and the Parakeet model.
Data is kept per user: accounts in the database (SQLite on your own computer, PostgreSQL on a server), videos and
clips in each user's own folder. [AGENTS.md](AGENTS.md) is a short map of the code for contributors and AI coding
assistants.

## Where your videos and clips are kept

Everything PeakCut keeps is in one folder: videos, clips, recordings, creators, settings and the speech model. By
default it is the `data` folder inside PeakCut. **Settings, Storage** shows the folder and how much room its drive
has, opens it in one click, and lets you move it to another drive: choose the new folder, and PeakCut moves
everything there the next time it starts. If the drive is nearly full, PeakCut says so and stops taking new videos
until there is room. (`PEAKCUT_DATA` in `.env` still works and takes priority.)

## Updating

PeakCut checks GitHub once a minute while it runs. When a newer version is out, the dashboard shows a notice
within a minute or two (no restart needed), with what changed
and how to update: ask your AI coding assistant to do it (the notice has a sentence to paste), or run
`git pull && pip install -r requirements.txt` in the PeakCut folder and restart it (with Docker:
`git pull && docker compose up -d --build`). Your `.env`, keys and clips are not touched. Only the two commit ids
are sent to GitHub; set `PEAKCUT_UPDATE_CHECK=0` to turn the check off.

## Deploy

[DEPLOY.md](DEPLOY.md) walks through a single server with Docker: the app, PostgreSQL, and Caddy for HTTPS.

```bash
docker compose up -d --build
```

## How it behaves under load

- The page asks the server one small question every few seconds. When nothing has changed, the answer comes
  straight from memory.
- Each person's videos wait in their own line and the lines take turns, so one person cannot hold everyone up.
- The server measures its own processor, memory and disk every second. Extra videos and recordings start only
  while there is room, wait briefly when it is nearly full, and continue the moment it frees up.
- There are no limits on upload size, video length, recordings, creators or storage unless you set them in
  `.env`. Five videos can be sent at once. Request sizes are always capped, so junk can't fill the memory.

## Tests

```bash
python tests/run.py
```

This starts a copy of the app on a spare port with an empty temporary data folder, runs the permission, abuse and
queue tests against it, then removes it. It needs no keys and never touches real data.

## Security

- With sign-in off, PeakCut listens on this computer only (`127.0.0.1`), refuses changes sent by other websites,
  and only answers to its own address. Do not expose it to the internet that way: turn accounts on.
- Passwords are stored only as salted scrypt hashes. Sessions are random tokens stored hashed; cookies are HTTP-only.
- Every route works out who is asking from the session, never from the request, so users only reach their own data.
- Only known video sites are fetched, and text from videos is treated as data when it is shown to the AI.
- Found a problem? Please report it privately through the repository's security advisories instead of opening a
  public issue.

## Using it responsibly

Clip videos you made, or have permission to use. You are responsible for the rights to what you download, clip and
post, and for following the terms of the sites you fetch from and post to.

## Licence

[MIT](LICENSE). Caption fonts in `studio/fonts/` (Anton, Archivo Black, Poppins) are under the SIL Open Font
License; the licence texts are in the same folder.
