# YouTube Course Sorter

Paste a YouTube channel link and get every video:

- **summarized** Blinkist-style (a short summary plus key takeaways)
- broken into a **timestamped table of contents** with the main points of each section (click a timestamp to jump to that moment on YouTube)
- **grouped by topic** into ordered course modules

You can search everything, and download the result as a Markdown course outline or as JSON.

## Running it

You need Python 3.10+ and an Anthropic API key (https://console.anthropic.com/).

**macOS / Linux:** run `./start.sh`
**Windows:** double-click `start.bat`

The first run installs dependencies and asks for your API key (saved to `.env`). Then open
http://127.0.0.1:5000, paste a channel (`https://www.youtube.com/@name`, `@name`, or a playlist link) and click
**Organize channel**.

Manual setup instead:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then paste your key into .env
python app.py
```

## How it works

1. `yt-dlp` lists the channel's uploads (Videos tab, newest first). No YouTube API key needed.
2. `youtube-transcript-api` pulls each video's captions (English preferred, otherwise any language). If a video
   has no captions, the notes come from its description and chapters instead, and the page says so.
3. Claude Haiku writes the summary, takeaways, and timestamped chapters for each video (4 videos at a time).
4. Claude Sonnet groups all the videos into course modules, ordered foundational → advanced.

Results are saved in `data/`. Each video's notes are cached, so **Re-run** on a channel only pays for new videos
(and re-does the topic grouping).

## Cost and time

Built to be cheap:

- Each video's notes are written by `claude-haiku-5-5`, Claude's low-cost model. A typical 15-20 minute video
  costs well under a cent.
- The one call that groups the whole channel into modules uses `claude-sonnet-5-5`, which is better at that
  judgment call. It runs once per channel and usually costs a few cents.
- Notes are cached, so re-running a channel only pays for new videos.

Expect a 100-video channel to cost well under a dollar. The channel page shows the actual Claude spend as it runs.
Set **Max videos** to around 10 for a first test.

To change models, set `NOTES_MODEL` or `OUTLINE_MODEL` in `.env` (for example `NOTES_MODEL=claude-sonnet-5-5` for
richer notes at roughly 20x the cost). If Haiku declines to process a video, it is retried on the outline model,
which uses Claude's server-side refusal fallback.

## Troubleshooting

- **Many videos say "No transcript or description available".** YouTube sometimes blocks caption requests from
  cloud servers or VPNs. Run the app on a normal home or office connection.
- **"No videos found".** Check the link opens the channel in a browser. For a channel's Shorts or Live tab, paste
  that tab's URL directly (e.g. `https://www.youtube.com/@name/shorts`).
- If you stop the server mid-run, open the channel and click **Re-run**. Finished videos are kept.
