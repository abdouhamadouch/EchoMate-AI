import asyncio
import os
import secrets
import uuid
from pathlib import Path
from typing import Literal, Optional

import httpx
import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, HttpUrl

APP_NAME = "EchoMate AI Media Server"
SECRET = os.getenv("RENDER_SHARED_SECRET", "").strip()
PUBLIC_URL = os.getenv("RENDER_SERVER_URL", "").rstrip("/")
CALLBACK_BASE = os.getenv("BOT_CALLBACK_BASE_URL", "").rstrip("/")
DOWNLOADS = Path(os.getenv("DOWNLOADS_DIR", "/tmp/echomate-downloads"))
DOWNLOADS.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME, version="1.0.0")
app.mount("/files", StaticFiles(directory=str(DOWNLOADS)), name="files")

# Optional Telegram group-call client. The HTTP/download service can start without it.
pyro_client = None
call_client = None
call_start_error = None
call_handler_error = None
active_sessions: dict[int, dict] = {}

try:
    api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    session_string = os.getenv("TELEGRAM_SESSION_STRING", "").strip()
    if api_id_raw and api_hash and session_string:
        from pyrogram import Client
        from pytgcalls import PyTgCalls
        pyro_client = Client(
            "echomate_assistant",
            api_id=int(api_id_raw),
            api_hash=api_hash,
            session_string=session_string,
            in_memory=True,
        )
        call_client = PyTgCalls(pyro_client)
except Exception as exc:
    call_start_error = str(exc)[:300]


def require_auth(authorization: Optional[str]) -> None:
    if not SECRET:
        raise HTTPException(status_code=503, detail="Server secret is not configured")
    supplied = (authorization or "")
    if supplied.lower().startswith("bearer "):
        supplied = supplied[7:].strip()
    if not secrets.compare_digest(supplied, SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.on_event("startup")
async def startup() -> None:
    if call_client:
        try:
            await call_client.start()
        except Exception as exc:
            global call_start_error
            call_start_error = str(exc)[:300]
            print(f"Telegram call client failed to start: {call_start_error}")


@app.on_event("shutdown")
async def shutdown() -> None:
    if call_client:
        try:
            await call_client.stop()
        except Exception:
            pass
    if pyro_client:
        try:
            await pyro_client.stop()
        except Exception:
            pass


class DownloadRequest(BaseModel):
    download_id: str = Field(min_length=1, max_length=120)
    url: HttpUrl
    format: Literal["mp3", "mp4"]
    chat_id: int
    callback_url: HttpUrl


class CallPlayRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=120)
    chat_id: int
    url: HttpUrl
    mode: Literal["audio", "video"] = "audio"


class CallControlRequest(BaseModel):
    session_id: Optional[str] = None
    chat_id: int
    seconds: Optional[int] = None


async def post_callback(url: str, payload: dict) -> bool:
    headers = {"Authorization": f"Bearer {SECRET}"} if SECRET else {}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=25) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
        return True
    except Exception as exc:
        print(f"Callback failed: {type(exc).__name__}: {str(exc)[:250]}")
        return False


async def auto_delete_file(file_path: str, delay_seconds: int = 180) -> None:
    """Delete served downloads after the bot has had time to fetch them."""
    await asyncio.sleep(max(60, delay_seconds))
    try:
        path = Path(file_path)
        if path.exists() and path.is_file():
            path.unlink()
            print(f"Cleaned up download: {path.name}")
    except Exception as exc:
        print(f"Could not clean download {file_path}: {type(exc).__name__}: {str(exc)[:200]}")


def cleanup_job_files(job_id: str) -> None:
    for path in DOWNLOADS.glob(f"{job_id}.*"):
        try:
            if path.is_file():
                path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Could not remove partial download {path.name}: {exc}")


# PyTgCalls 2.2.x emits separate audio/video end updates.
if call_client:
    try:
        from pytgcalls.types import Update
        from pytgcalls.types.stream import StreamAudioEnded, StreamVideoEnded

        @call_client.on_update()
        async def on_stream_end(_, update: Update):
            if not isinstance(update, (StreamAudioEnded, StreamVideoEnded)):
                return

            chat_id = update.chat_id
            session = active_sessions.get(chat_id)
            if not session or session.get("ending"):
                return

            # Prevent duplicate end updates from advancing the queue twice.
            session["ending"] = True
            session_id = session.get("session_id")
            if CALLBACK_BASE and session_id:
                callback_url = f"{CALLBACK_BASE}/api/public/render/callback"
                await post_callback(
                    callback_url,
                    {"type": "track_ended", "session_id": session_id},
                )
            else:
                print("track_ended not sent: BOT_CALLBACK_BASE_URL or session_id is missing")

            # Do not remove a newer track started while the callback was running.
            if active_sessions.get(chat_id) is session:
                active_sessions.pop(chat_id, None)

    except Exception as exc:
        call_handler_error = f"{type(exc).__name__}: {str(exc)[:220]}"
        print(f"Stream-end handler unavailable: {call_handler_error}")


def _download(url: str, fmt: str, job_id: str) -> tuple[str, str]:
    # Keep filenames independent of untrusted titles.
    template = str(DOWNLOADS / f"{job_id}.%(ext)s")
    options = {
        "outtmpl": template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 25,
        "retries": 2,
        "max_filesize": 48 * 1024 * 1024,
    }
    if fmt == "mp3":
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        options.update({
            "format": "best[ext=mp4][height<=720]/best[height<=720]/best",
            "merge_output_format": "mp4",
        })
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))
        if fmt == "mp3":
            candidate = prepared.with_suffix(".mp3")
            if not candidate.exists():
                matches = list(DOWNLOADS.glob(f"{job_id}*.mp3"))
                if matches:
                    candidate = matches[0]
            path = candidate
        else:
            candidate = prepared.with_suffix(".mp4")
            path = candidate if candidate.exists() else prepared
        if not path.exists():
            matches = list(DOWNLOADS.glob(f"{job_id}.*"))
            if not matches:
                raise RuntimeError("Download finished but output file was not found")
            path = matches[0]
        return str(path), str(info.get("title") or "Downloaded media")


async def process_download(req: DownloadRequest) -> None:
    job_id = uuid.uuid4().hex[:16]
    status, title, file_url, error = "failed", "Media", None, None
    file_path = None
    try:
        file_path, title = await asyncio.to_thread(
            _download, str(req.url), req.format, job_id
        )
        size = Path(file_path).stat().st_size
        if size > 50 * 1024 * 1024:
            raise RuntimeError("حجم الملف يتجاوز الحد الأقصى المستهدف للبوت (50 ميجابايت)")
        if not PUBLIC_URL:
            raise RuntimeError("RENDER_SERVER_URL غير مضبوط في متغيرات البيئة")
        status = "done"
        file_url = f"{PUBLIC_URL}/files/{Path(file_path).name}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:350]}"
        cleanup_job_files(job_id)
        file_path = None

    await post_callback(str(req.callback_url), {
        "type": "download",
        "download_id": req.download_id,
        "chat_id": req.chat_id,
        "status": status,
        "title": title,
        "file_url": file_url,
        "error": error,
    })

    # Begin the TTL after callback delivery so the bot has time to fetch the file.
    if status == "done" and file_path:
        asyncio.create_task(auto_delete_file(file_path, 180))


@app.get("/")
async def root():
    return {"service": APP_NAME, "status": "running", "health": "/health"}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "downloads_dir_writable": os.access(DOWNLOADS, os.W_OK),
        "call_client_configured": call_client is not None,
        "call_client_started": call_client is not None and call_start_error is None,
        "stream_end_handler_configured": call_client is not None and call_handler_error is None,
        "call_client_error": call_start_error or call_handler_error,
    }


@app.post("/download", status_code=202)
async def download_endpoint(
    req: DownloadRequest,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None),
):
    require_auth(authorization)
    background_tasks.add_task(process_download, req)
    return {"ok": True, "download_id": req.download_id, "status": "accepted"}


def require_calls() -> None:
    if not call_client or call_start_error or call_handler_error:
        detail = call_start_error or call_handler_error or (
            "خدمة المكالمات غير مهيأة. تأكد من TELEGRAM_API_ID و TELEGRAM_API_HASH و TELEGRAM_SESSION_STRING."
        )
        raise HTTPException(status_code=503, detail=detail)


async def ensure_assistant_joined(chat_id: int) -> None:
    if not pyro_client:
        raise HTTPException(status_code=503, detail="جلسة الحساب المساعد غير مهيأة على السيرفر.")
    try:
        await pyro_client.join_chat(chat_id)
    except Exception as exc:
        error_name = type(exc).__name__.upper()
        error_text = str(exc).upper()
        combined = f"{error_name} {error_text}"

        if "USER_ALREADY_PARTICIPANT" in combined or "USERALREADYPARTICIPANT" in combined:
            return
        if "USER_BANNED_IN_CHANNEL" in combined or "USERBANNEDINCHANNEL" in combined:
            raise HTTPException(
                status_code=403,
                detail="الحساب المساعد محظور في هذه المجموعة/القناة. أزل الحظر عنه أولاً.",
            ) from exc
        if "CHAT_ADMIN_REQUIRED" in combined or "CHATADMINREQUIRED" in combined:
            raise HTTPException(
                status_code=403,
                detail="تعذّر انضمام الحساب المساعد. تحقق من صلاحيات المجموعة وأضف الحساب إليها.",
            ) from exc
        if "CHANNEL_PRIVATE" in combined or "CHANNELPRIVATE" in combined:
            raise HTTPException(
                status_code=403,
                detail="المجموعة/القناة خاصة ولا يستطيع الحساب المساعد الوصول إليها. أضفه يدوياً أو زوّد البوت برابط دعوة صالح.",
            ) from exc
        raise HTTPException(
            status_code=403,
            detail=f"تعذّر إدخال الحساب المساعد إلى المجموعة: {type(exc).__name__}: {str(exc)[:180]}",
        ) from exc


@app.post("/call/play")
async def call_play(req: CallPlayRequest, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    require_calls()
    await ensure_assistant_joined(req.chat_id)

    try:
        def resolve_stream():
            opts = {
                "format": "bestaudio/best" if req.mode == "audio" else "best[height<=720]/best",
                "quiet": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(str(req.url), download=False)
                return info.get("url") or str(req.url)

        stream_url = await asyncio.to_thread(resolve_stream)
        from pytgcalls.types import MediaStream, AudioQuality
        kwargs = {"media_path": stream_url, "audio_parameters": AudioQuality.HIGH}
        if req.mode == "audio":
            kwargs["video_flags"] = MediaStream.Flags.IGNORE

        await call_client.play(req.chat_id, MediaStream(**kwargs))
        active_sessions[req.chat_id] = {
            "session_id": req.session_id,
            "mode": req.mode,
            "started": True,
            "ending": False,
        }
        return {"ok": True, "chat_id": req.chat_id, "session_id": req.session_id}
    except HTTPException:
        raise
    except Exception as exc:
        err_text = f"{type(exc).__name__}: {str(exc)}"
        if "NoActiveGroupCall" in err_text or "GROUPCALL_INVALID" in err_text:
            raise HTTPException(
                status_code=400,
                detail="المكالمة الصوتية غير مفتوحة في المجموعة، يرجى فتح المكالمة أولاً.",
            ) from exc
        raise HTTPException(status_code=502, detail=f"فشل تشغيل البث: {err_text[:300]}") from exc


@app.post("/call/pause")
async def call_pause(req: CallControlRequest, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    require_calls()
    try:
        await call_client.pause_stream(req.chat_id)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300])


@app.post("/call/resume")
async def call_resume(req: CallControlRequest, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    require_calls()
    try:
        await call_client.resume_stream(req.chat_id)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300])


@app.post("/call/stop")
async def call_stop(req: CallControlRequest, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    require_calls()
    try:
        await call_client.leave_call(req.chat_id)
    except Exception as exc:
        # Don't conceal a real failure, but clear local tracking.
        active_sessions.pop(req.chat_id, None)
        raise HTTPException(status_code=502, detail=str(exc)[:300])
    active_sessions.pop(req.chat_id, None)
    return {"ok": True}


@app.post("/call/seek")
async def call_seek(req: CallControlRequest, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    require_calls()
    if req.seconds is None or req.seconds == 0:
        raise HTTPException(status_code=422, detail="seconds must be non-zero; send +10 or -10")
    # PyTgCalls seek support varies by stream type/version; do not report success if unavailable.
    seek = getattr(call_client, "seek_stream", None)
    if seek is None:
        raise HTTPException(status_code=501, detail="Seek is not supported by the installed PyTgCalls API for this stream")
    try:
        await seek(req.chat_id, req.seconds)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:300])
