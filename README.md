# Short Fact Checker

Paste a link to an Instagram Reel, YouTube Short or TikTok and get a fact-check report: each claim in the video with a verdict, a confidence score and sources.

```
python factcheck.py "https://www.instagram.com/reel/XXXXXXXX/"
```

## How it works

1. The video is downloaded with yt-dlp.
2. ffmpeg pulls out the audio and a few frames (to catch on-screen text, charts, captions).
3. The audio is transcribed locally with faster-whisper.
4. Claude reads the transcript, the frames and the caption, and lists the checkable claims.
5. Claude checks each claim with web search and gives a verdict, a confidence score (0-100) and its sources.

The report is printed in the terminal and saved in `reports/<date>/` along with the transcript, the frames and the raw JSON.

## Requirements

- macOS or Linux, Python 3.10+
- ffmpeg
- [Claude Code](https://docs.claude.com/en/docs/claude-code/overview), logged in with a Claude Pro or Max account. No API key needed.

## Setup

```bash
git clone https://github.com/Jolaterreureu/Short_Fact_Checker.git
cd Short_Fact_Checker

brew install ffmpeg            # or: sudo apt install ffmpeg
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Install Claude Code and log in once:

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude        # type /login, pick your Claude account, then /exit
```

Quick check that it works:

```bash
echo "Reply OK" | claude -p
```

The first run also downloads the Whisper model (about 500 MB for `small`).

## Usage

```bash
source .venv/bin/activate

python factcheck.py "https://www.instagram.com/reel/XXXXXXXX/"
python factcheck.py "https://www.youtube.com/shorts/XXXXXXXX"
python factcheck.py ~/Downloads/clip.mp4

# report in another language
python factcheck.py "<url>" --report-lang French
```

If Instagram refuses the download, pass the cookies of a browser where you're logged in:

```bash
python factcheck.py "<url>" --cookies-from-browser chrome
```

| Option | Default | |
|---|---|---|
| `--report-lang` | English | Language of the report |
| `--frames` | 8 | Frames sent to the model |
| `--max-claims` | 8 | Max number of claims checked |
| `--max-searches` | 15 | Max number of web searches |
| `--whisper-model` | small | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `--audio-lang` | auto | Force the spoken language (`en`, `fr`...) |
| `--model` | Claude Code default | e.g. `sonnet`, `opus` |
| `--cookies-from-browser` | | `chrome`, `safari`, `firefox`... |
| `--backend` | claude-code | `claude-code` or `api` |
| `--keep-video` | off | Keep the downloaded video |

Defaults can also be set in a `.env` file, see `.env.example`.

To run it from anywhere, add an alias to `~/.zshrc` (adjust the path):

```bash
alias factcheck="$HOME/path/to/Short_Fact_Checker/.venv/bin/python $HOME/path/to/Short_Fact_Checker/factcheck.py"
```

## Verdicts

| Verdict | Meaning |
|---|---|
| TRUE | Accurate, nothing significant left out |
| MOSTLY TRUE | Minor inaccuracy that doesn't change the meaning |
| MISLEADING | Partly accurate, but framed to suggest something false |
| MOSTLY FALSE | The core of the claim is wrong |
| FALSE | Contradicted by reliable evidence |
| UNVERIFIABLE | Not enough evidence either way |

The confidence score is how likely the verdict itself is to be right. It is capped at 80 unless a source was actually found during the search, and at 60 when the model relies on its own knowledge. The full method is in [`prompts/verify_claims.md`](prompts/verify_claims.md).

## Using the API instead

If you'd rather use API credits than a subscription:

```bash
cp .env.example .env    # set ANTHROPIC_API_KEY and BACKEND=api
```

Web search on the API costs $10 per 1,000 searches on top of tokens.

## Notes

- With a subscription, each run uses part of your usage limits (frames plus around ten searches). Lower `--frames`, `--max-claims` or `--max-searches` if you hit them. `/usage` inside `claude` shows where you are.
- Don't export `ANTHROPIC_API_KEY` in your shell when using the subscription: Claude Code would bill the API instead. The script removes it for that mode anyway.
- Frames are sampled at regular intervals, so text shown very briefly can be missed. Raise `--frames` if needed.
- Whisper can mishear names and numbers. The claim extraction step flags likely transcription errors.
- The model can be wrong. Read the sources before sharing a verdict.
- Downloading Instagram content may go against their terms of use. Keep it for personal use and don't republish the videos.
