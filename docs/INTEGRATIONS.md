# Sending transcripts somewhere

Every tool writes the archive first. With a webhook it also POSTs each transcript as JSON, in
order, from a background thread; the archive still has everything if the endpoint is down.

```sh
export OMI_NOTES_WEBHOOK=http://127.0.0.1:8000/transcripts OMI_NOTES_WEBHOOK_TOKEN=s3cret
uv run omi-sync          # every command reads them (also OMI_NOTES_ARCHIVE); schedule.sh bakes them into the LaunchAgent
```

The flags `--webhook URL --webhook-token TOKEN` do the same for one run, but a token on the command line
shows up in the process list for every user on the Mac; the variable does not.

## The request

`POST <URL>` with `Content-Type: application/json`, `Authorization: Bearer <token>` when a token is
set, and `Idempotency-Key: omi-<start_epoch>`. The token travels in the clear over `http://`, so keep such an
endpoint on localhost or use `https://`.

| Field | |
| --- | --- |
| `text` | the transcript; empty ones are never sent |
| `source` | `ring` (downloaded from the pendant), `live` (streamed), `file` (`omi-transcribe`), `double_tap` (`omi-stream`: the last two minutes, sent when the pendant is double-tapped) |
| `start_epoch`, `start_local` | when it was **recorded**, as epoch seconds and ISO time in the Mac's zone; synced audio can be hours old |
| `duration_s`, `wav` | length and the recording's path inside the archive (`recordings/<day>/<file>.wav`), or only the file name for a WAV from elsewhere |
| `segments`, `engine`, `processing_ms`, `session`, `ts` | timed pieces, `whisper-cli` or `whisper-server`, transcription time, live session id, send time |

2xx accepts. 409, 429, 5xx and an unreachable host are retried every 20 s for up to ten minutes,
with later transcripts waiting behind; any other status drops the message. Redirects are not
followed (following one would resend the token to the new address as a GET without the
transcript), so the URL has to be the endpoint's final address.

## Examples

**An agent gateway with bearer-token hooks** (OpenClaw-style `POST /hooks/<name>`) takes the payload
as is. Its prompt should resolve relative dates ("Wednesday") against `start_local`, not the time it runs.

```sh
export OMI_NOTES_WEBHOOK=http://127.0.0.1:18789/hooks/omi
export OMI_NOTES_WEBHOOK_TOKEN=$(jq -r .hooks.token ~/.openclaw/openclaw.json)
```

**Anything else** (Slack, a notes app, a database): a few lines of HTTP server in front of it. This
forwards to a Slack incoming webhook; answer 5xx while the downstream is down and Omi Voice Notes retries.

```python
# forward.py — uv run --with fastapi --with uvicorn uvicorn forward:app --port 8000
import json, os, urllib.request
from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/transcripts")
async def transcript(request: Request):
    t = await request.json()
    text = f"{t['start_local']} · {t['source']} · {t['duration_s']:.0f}s\n{t['text']}"
    urllib.request.urlopen(urllib.request.Request(os.environ["SLACK_WEBHOOK_URL"],
        json.dumps({"text": text}).encode(), {"Content-Type": "application/json"}))
    return {"ok": True}
```
