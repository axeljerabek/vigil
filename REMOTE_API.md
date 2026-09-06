# External API (Remote Control)

vaelen can accept video from an external system — a media asset manager, home automation, another app, a script — process it through the same pipeline as a live recording (codec handling, filmstrip generation, AI description, face recognition), and hand the enriched result back, either via webhook callback or by polling a status endpoint.

## Getting an API key

Dashboard → **External API (MAM)** card → give it a label, click Generate Key. **The key is shown exactly once** — copy it immediately, it's stored only as a hash afterward and can't be retrieved again. Revoking a key takes effect immediately.

## Authentication

Every request needs the key in one of these headers:
```
Authorization: Bearer idg_xxxxxxxxxxxx
```
or
```
X-API-Key: idg_xxxxxxxxxxxx
```

## Submitting a job

```
POST /api/v1/jobs
```
Multipart form data:

| Field | Required | Notes |
| :--- | :--- | :--- |
| `file` | Yes | The video file. Same codec handling as everything else in vaelen — HEVC and other browser-incompatible codecs are transcoded automatically. |
| `topics` | No | Comma-separated list, e.g. `break-in, delivery, mail carrier`. Overrides the global topic settings **for this job only**. Omit to use the global settings. |
| `detect_faces` | No | `true` (default) or `false`. |
| `callback_url` | No | POSTed to when the job finishes (see below). Omit if you'd rather poll. |

Responds immediately (before processing finishes):
```json
{"job_id": "a1b2c3...", "status": "queued"}
```
HTTP 202.

**Currently video only.** Audio-only and image-only jobs aren't implemented yet — see the note at the bottom.

## Checking status

```
GET /api/v1/jobs/<job_id>
```
```json
{
  "job_id": "a1b2c3...",
  "status": "processing",
  "media_type": "video",
  "submitted_at": 1735900000.0,
  "started_at": 1735900001.2,
  "finished_at": null,
  "error": null,
  "result": null
}
```
`status` is one of `queued`, `processing`, `done`, `failed`. Poll this as often as you like — there's no rate limit tied to it.

## Getting the result

Once `status` is `"done"`:

```
GET /api/v1/jobs/<job_id>/metadata
```
```json
{
  "job_id": "a1b2c3...",
  "status": "done",
  "description": "A delivery van pulls up and a package is left at the door.",
  "topics": {"delivery": 92},
  "transcript": null,
  "faces": null
}
```

```
GET /api/v1/jobs/<job_id>/video
```
Returns the processed MP4 directly.

**Just a clip instead of the whole thing?**
```
GET /api/v1/jobs/<job_id>/video?start=12&end=18
```
`start`/`end` are seconds from the beginning. Extracted via stream copy where possible — no quality loss, no re-encoding cost. Omit `end` to get from `start` to the end of the video.

## Webhook callback

If you passed `callback_url`, vaelen POSTs this to it once the job finishes (success or failure):
```json
{
  "job_id": "a1b2c3...",
  "status": "done",
  "result": { "description": "...", "topics": {...}, "transcript": null, "faces": null },
  "error": null
}
```
If your endpoint doesn't respond with a 2xx, delivery is retried with exponential backoff (up to 5 attempts, capped at 60s between tries). If all attempts fail, the result is still available via the status/metadata endpoints — polling always works as a fallback even if your webhook receiver was down.

## Example (curl)

```bash
# Submit
curl -X POST https://your-vaelen-host:19473/api/v1/jobs \
  -H "Authorization: Bearer idg_xxxxxxxxxxxx" \
  -F "file=@delivery_clip.mp4" \
  -F "topics=delivery,mail carrier" \
  -F "callback_url=https://your-system.example/vaelen-callback"

# Poll
curl https://your-vaelen-host:19473/api/v1/jobs/<job_id> \
  -H "Authorization: Bearer idg_xxxxxxxxxxxx"

# Get the result
curl https://your-vaelen-host:19473/api/v1/jobs/<job_id>/metadata \
  -H "Authorization: Bearer idg_xxxxxxxxxxxx"
```

## Not yet implemented

* **Audio-only and image-only jobs** — the endpoint currently only accepts video. Submitting an audio or image file returns a clear `400` error rather than silently mishandling it.
* **Agent-driven control** (an AI agent triggering manual recordings/exports through this API) — planned as a later layer on top of this same API, not yet built.
