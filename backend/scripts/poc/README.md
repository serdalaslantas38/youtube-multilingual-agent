# Caption-translation proof of concept (Stage 3)

Isolated scripts that answer one question: **can the official YouTube Data API
reproduce the Studio "Auto-translate" subtitle workflow?** See
[`docs/youtube-caption-api-investigation.md`](../../../docs/youtube-caption-api-investigation.md).

## Running the diagnostic command

### Stage 3a — Mocked mode (default, zero network, zero credentials)
```bash
PYTHONPATH=backend .venv/bin/python -m scripts.poc.caption_diagnostics --video-id dev_tr_video_01
```
Reports only sanitized metadata (`selected_language`, `selected_track_kind`,
`selected_status`, `source_download_succeeded`, per-language `download_succeeded`,
`validation_passed`, `cue_count`, and `insert_request_valid`) without printing
credentials or raw subtitle contents.

### Stage 3b — Read-only live mode (next step, requires owner authorization)
`captions.list` + `captions.download` (with and without `tlang`) on ONE explicitly
authorized development video. No writes (`captions.insert` is never executed).
```bash
PYTHONPATH=backend .venv/bin/python -m scripts.poc.caption_diagnostics \
    --mode live-readonly \
    --video-id <YOUR_DEV_VIDEO_ID> \
    --i-own-this-dev-video <YOUR_DEV_VIDEO_ID>
```

### Stage 3c — Write live mode (only if needed after 3b)
`captions.insert` with `isDraft=true` on the same unlisted development video,
only after written owner approval. Never on public videos.

