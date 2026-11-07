# Omi Voice Notes

[![CI](https://github.com/kevinmkarol/omi-voice-notes/actions/workflows/ci.yml/badge.svg)](https://github.com/kevinmkarol/omi-voice-notes/actions/workflows/ci.yml)

Turns the [Omi](https://omi.me) pendant into a voice-notes button that syncs to your Mac. Tap,
talk, tap: the pendant stores the note; next time it's near the Mac, `omi-sync` pulls it over
Bluetooth, transcribes it locally with whisper.cpp and files it as audio plus text. No phone, no
Omi app, no cloud. Leave it unmuted and it records everything instead, if that's what you want.

```
tap ● talk ● tap ──▶ stored on the pendant ──BLE──▶ omi-sync ──▶ ~/Omi/recordings + transcripts (+ webhook)
                                                    omi-stream   live transcription while connected
```

## Setup

Needs macOS, Homebrew and a pendant on firmware 3.0.20 or newer (update it in the Omi app first).

```sh
cd mac
./setup.sh          # brew install whisper-cpp opus uv, download the models (1.5 GB), seed the vocabulary
uv run omi-scan     # pendant on, Omi phone app closed; macOS asks for Bluetooth the first time
```

The tap-to-mute button needs the custom firmware in [`firmware/`](firmware/README.md): one patch
on the stock 3.0.21, built in Docker, flashed over Bluetooth, still updatable from the Omi app.
Stock firmware works with everything else here but records continuously from power-on.

## Commands

```sh
uv run omi-sync                   # pull every stored note, transcribe, free the pendant's storage
./schedule.sh 10m                 # do that every 10 minutes whenever the pendant is in range (./schedule.sh remove)
uv run omi-scan --identify        # firmware, battery, clock, how much audio is waiting
uv run omi-stream                 # live transcription while connected; a double tap sends the last two minutes to the webhook
uv run omi-transcribe clip.wav    # any WAV through the same pipeline
```

Every command takes `--archive DIR` (default `~/Omi`, or `$OMI_NOTES_ARCHIVE`) and `--webhook URL` to
hand each note to something else (see [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)). `--help` lists the rest.

## Where things go

| Path | What |
| --- | --- |
| `~/Omi/recordings/<day>/*.wav` + `.json` | each note's audio and metadata (start, seconds, clock source) |
| `~/Omi/transcripts/<day>.jsonl` and `.md` | one entry per note; the Markdown is the same, readable |
| `~/Omi/events/<day>.jsonl` | taps, battery, connects, storage info |
| `~/.config/omi-voice-notes/vocabulary.txt` | names whisper should spell right, one per line |
| `~/Library/Application Support/omi-voice-notes/` | the models (`OMI_NOTES_MODELS` moves them) |

Notes are split wherever the pendant's timestamps jump by more than `--gap` (90 s), so each
tap-talk-tap becomes its own recording and transcript.

## Good to know

- The pendant takes **one** Bluetooth connection. Close the Omi app, or nothing here can connect.
- It stores audio **only while nothing is connected**, so a sync is a short gap in recording:
  about 8 s when nothing is new, plus ~7 s per minute of audio.
- Stored audio is freed on the pendant only after it is on disk. An interrupted sync resumes.
- With the custom firmware: it powers on muted (purple LED). Tap to start a note (red, two
  buzzes, then one buzz every 30 s while recording), tap to end it (purple, one buzz). Hold 1.5 s
  to power off.
- Under 3.5 V the pendant powers itself off a few seconds after boot, even on the dock, with no LED.
- `docs/PROTOCOL.md` documents the pendant's Bluetooth protocol, storage included.

## Security

- The pendant pairs with nothing: whoever is in Bluetooth range while nothing else is connected
  can read the notes stored on it, clear them, or set its clock. Sync often and keep it muted
  when that matters.
- Its firmware is signed with the development key Based Hardware publishes with the source, so
  anyone can flash it over Bluetooth. That is what lets `firmware/flash.sh` work; it is upstream's
  choice and nothing here changes it.
- Recordings and transcripts are plain files under `~/Omi`. The webhook token, once scheduled,
  sits in `~/Library/LaunchAgents/omi-voice-notes.sync.plist` (mode 600). Pass it as
  `OMI_NOTES_WEBHOOK_TOKEN` rather than `--webhook-token`, which the process list shows.

## Layout

```
mac/omi_voice_notes/ble/        BLE protocol + bleak glue (uuids, ring storage, audio framing, client)
mac/omi_voice_notes/pipeline/   recordings, speech segmentation, transcription, sinks (archive, webhook)
mac/omi_voice_notes/whisper.py  whisper-cli runner, model and vocabulary paths
mac/omi_voice_notes/cli/        omi-scan, omi-sync, omi-stream, omi-transcribe
mac/tests/                      uv run pytest
firmware/                       tap-to-mute firmware: patch, Docker build, checks, Bluetooth flash
```

## Contributing

A personal project, built against one pendant on firmware 3.0.21, with no issue tracker. Pull
requests that fix a bug or correct the protocol notes are welcome, with a test where one fits
(`cd mac && uv run pytest`). For anything larger, a small pull request that explains the need is
the way to ask. Security problems go through the [security policy](SECURITY.md).

## License

MIT (see [LICENSE](LICENSE)). The firmware patch modifies Based Hardware's
[Omi firmware](https://github.com/BasedHardware/omi), also MIT.
