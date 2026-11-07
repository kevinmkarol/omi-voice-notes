# ABOUTME: The flags every command shares take their defaults from the environment, so the scheduled LaunchAgent
# ABOUTME: (which can only carry variables) can set the archive and webhook without editing anything committed.
import argparse

from omi_voice_notes.cli import common


def _parse(monkeypatch, env: dict, argv: list[str] | None = None) -> argparse.Namespace:
    for key in ("OMI_NOTES_ARCHIVE", "OMI_NOTES_WEBHOOK", "OMI_NOTES_WEBHOOK_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    p = argparse.ArgumentParser()
    common.add_sink_args(p)
    return p.parse_args(argv or [])


def test_sink_defaults_come_from_the_environment(monkeypatch):
    args = _parse(
        monkeypatch,
        {
            "OMI_NOTES_ARCHIVE": "/srv/omi",
            "OMI_NOTES_WEBHOOK": "http://x/n",
            "OMI_NOTES_WEBHOOK_TOKEN": "t",
        },
    )
    assert (args.archive, args.webhook, args.webhook_token) == ("/srv/omi", "http://x/n", "t")


def test_flags_override_the_environment_and_defaults_apply_without_it(monkeypatch):
    args = _parse(monkeypatch, {"OMI_NOTES_ARCHIVE": "/srv/omi"}, ["--archive", "/tmp/a"])
    assert args.archive == "/tmp/a"
    args = _parse(monkeypatch, {})
    assert args.archive.endswith("/Omi") and args.webhook is None and args.webhook_token is None
