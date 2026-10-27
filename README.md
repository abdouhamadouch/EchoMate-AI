# EchoMate AI Server

FastAPI service for media downloads and Telegram group-call playback.

## Endpoints
- `GET /health` — health check
- `POST /download` — download media as MP3 or MP4 and POST the result to `callback_url`
- `POST /call/play` — play an audio/video URL in a Telegram group call
- `POST /call/pause`, `POST /call/resume`, `POST /call/stop`, `POST /call/seek`

All POST endpoints require `Authorization: Bearer <RENDER_SHARED_SECRET>`.

## Render
Create a Docker Web Service from this repository. Set the environment variables in Render's dashboard. Group-call playback requires a dedicated Telegram user session and may need a paid Render instance for reliable continuous streaming. Never commit session strings or API secrets.

Downloads are stored in the service's temporary filesystem; they may disappear after restarts or redeploys. This service does not guarantee that arbitrary social-media URLs can be downloaded: source sites may restrict access, and users must have rights to the media they download.
