"""
xtts-openai-shim

Small FastAPI adapter that sits between Open WebUI (which expects an
OpenAI-compatible /v1/audio/speech TTS endpoint) and xtts-api-server
(which speaks its own /tts_to_audio/ shape).

Confirmed against a real xtts-api-server instance:

    GET  /get_models_list   -> ["v2.0.2", "Belldandy", ...]
    GET  /speakers          -> [{"name": "...", "voice_id": "...",
                                  "preview_url": ".../sample/<name>/reference.wav"}]
    POST /switch_model      -> {"model_name": "<name>"}
    POST /tts_to_audio/     -> {"text": "...", "speaker_wav": "<name>/reference.wav",
                                 "language": "en"}
                               returns raw wav bytes

Open WebUI (Settings -> Audio -> TTS Engine -> OpenAI-compatible) calls:

    POST /v1/audio/speech   -> {"model": "tts-1", "input": "...", "voice": "belldandy"}
                               expects raw audio bytes back

This shim:
  - Refreshes the list of available speakers from xtts-api-server on
    startup and periodically, mapping a lowercased "voice" name to the
    exact speaker_wav path xtts-api-server itself reports.
  - Optionally switches the active XTTS model to match the requested
    voice, if VOICE_MODEL_MAP says a given voice needs a specific model
    loaded first (most setups only have one fine-tuned model loaded at a
    time, so this is usually a no-op after the first call).
  - Forwards the synthesis request and streams the wav bytes back
    unchanged, since Open WebUI is happy with wav (response_format is
    accepted but currently ignored -- see NOTE below if you need mp3).
"""

import asyncio
import os
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("xtts-shim")

# Base URL of your existing xtts-api-server container.
# Set via env var so this works whether it's reached by container name
# (same docker network) or by host IP.
XTTS_BASE_URL = os.environ.get("XTTS_BASE_URL", "http://192.168.1.2:8020").rstrip("/")

# Default language used if a request doesn't specify one.
DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "en")

# If set, the shim will call /switch_model to this model name on startup
# and whenever a request's voice isn't found in the current speaker list,
# in case it just needs a refresh. Leave blank to never auto-switch.
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "")

# Optional explicit "voice name" -> "xtts model name" overrides, as
# JSON, e.g. VOICE_MODEL_OVERRIDES='{"doc":"Keiichi"}' if a wake-word
# ("hey doc") doesn't match the model's actual folder name ("Keiichi").
# Anything not listed here falls back to matching /get_models_list by
# case-insensitive name, which covers the common case where the voice
# name and model name are the same (e.g. "belldandy" -> "Belldandy").
import json as _json
try:
    VOICE_MODEL_OVERRIDES: dict[str, str] = _json.loads(os.environ.get("VOICE_MODEL_OVERRIDES", "{}"))
except _json.JSONDecodeError:
    log.warning("VOICE_MODEL_OVERRIDES is not valid JSON, ignoring")
    VOICE_MODEL_OVERRIDES = {}

# name (lowercased) -> real casing, as reported by /get_models_list
_model_cache: dict[str, str] = {}

app = FastAPI(title="xtts-openai-shim")

# Open CORS so a standalone test page (voice-lab.html) or any other
# browser-based client on your network can call this directly, since it
# won't share an origin with the shim itself. This is a personal homelab
# tool sitting behind your own network, not a public-facing API, so
# allowing any origin is a reasonable tradeoff for convenience here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# voice name (lowercased) -> speaker_wav path string exactly as
# xtts-api-server expects it back in /tts_to_audio/
_speaker_cache: dict[str, str] = {}

# Which model xtts-api-server currently has loaded, as best the shim
# knows -- used to skip redundant /switch_model calls when the same
# character is used for consecutive messages in a conversation, so only
# an actual character *change* pays the ~10s reload cost.
_current_model: Optional[str] = None

# xtts-api-server appears to corrupt its own HTTP response stream when two
# /tts_to_audio/ (or /switch_model) requests overlap -- observed as
# "Too much/little data for declared Content-Length" errors in its logs,
# which surface here as a 502. Open WebUI splits long responses into
# per-sentence TTS calls and can fire them in close succession, so we
# serialize all calls to XTTS through this lock rather than trusting it
# to handle concurrency safely on its own.
_xtts_lock = asyncio.Lock()


async def refresh_speakers(client: httpx.AsyncClient) -> None:
    """Pull the current speaker list from xtts-api-server and rebuild the
    voice -> speaker_wav map. Uses the *voice_id* field's relative wav path
    convention confirmed via /speakers: "<name>/reference.wav"."""
    resp = await client.get(f"{XTTS_BASE_URL}/speakers")
    resp.raise_for_status()
    speakers = resp.json()

    _speaker_cache.clear()
    for sp in speakers:
        name = sp.get("voice_id") or sp.get("name")
        if not name:
            continue
        # xtts-api-server's own /speakers listing implies the path it wants
        # back is "<name>/reference.wav" (confirmed from preview_url shape:
        # http://host:port/sample/<name>/reference.wav). If a given install
        # uses flat filenames instead (e.g. "belldandy.wav" with no
        # subfolder), that will also show up correctly here as long as
        # voice_id/name matches -- adjust the f-string below if your
        # /speakers response shows a different convention.
        _speaker_cache[name.lower()] = f"{name}/reference.wav"

    log.info("Refreshed speakers: %s", _speaker_cache)


async def refresh_models(client: httpx.AsyncClient) -> None:
    """Pull the current model list from xtts-api-server (/get_models_list)
    so voice->model resolution can match by name case-insensitively."""
    resp = await client.get(f"{XTTS_BASE_URL}/get_models_list")
    resp.raise_for_status()
    models = resp.json()

    _model_cache.clear()
    for name in models:
        _model_cache[name.lower()] = name

    log.info("Refreshed models: %s", _model_cache)


def resolve_model_for_voice(voice: str) -> Optional[str]:
    """Figure out which xtts-api-server model name a given voice needs.
    Checks explicit overrides first, then falls back to matching the
    voice name itself against known models."""
    voice_key = voice.lower()
    if voice_key in VOICE_MODEL_OVERRIDES:
        override = VOICE_MODEL_OVERRIDES[voice_key]
        return _model_cache.get(override.lower(), override)
    return _model_cache.get(voice_key)


@app.on_event("startup")
async def on_startup():
    global _current_model

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            await refresh_speakers(client)
        except Exception as e:
            log.warning("Could not refresh speakers on startup: %s", e)

        try:
            await refresh_models(client)
        except Exception as e:
            log.warning("Could not refresh models on startup: %s", e)

        if DEFAULT_MODEL:
            try:
                r = await client.post(
                    f"{XTTS_BASE_URL}/switch_model",
                    json={"model_name": DEFAULT_MODEL},
                )
                log.info("switch_model(%s) -> %s", DEFAULT_MODEL, r.status_code)
                if r.status_code == 200:
                    _current_model = DEFAULT_MODEL
            except Exception as e:
                log.warning("Could not switch to default model %s: %s", DEFAULT_MODEL, e)


class SpeechRequest(BaseModel):
    model: str = "tts-1"          # ignored -- XTTS model selection is separate, see /admin/switch_model
    input: str
    voice: str = "belldandy"
    response_format: Optional[str] = "wav"   # NOTE: only wav is actually returned, see below
    speed: Optional[float] = None            # accepted, currently ignored
    language: Optional[str] = None           # non-standard extra field some clients may send


@app.post("/v1/audio/speech")
async def speech(req: SpeechRequest):
    global _current_model
    voice_key = req.voice.lower()

    # Serialize the whole switch+generate sequence -- see _xtts_lock's
    # docstring-comment above for why. If several sentences from the same
    # response arrive close together, they'll simply queue up here and
    # run one at a time instead of tripping over each other in XTTS.
    async with _xtts_lock:
        async with httpx.AsyncClient(timeout=120) as client:
            # Auto-switch models on demand: only pay the reload cost when
            # the requested voice actually needs a different model than
            # whatever is currently loaded. Consecutive messages using the
            # same character are effectively free after the first switch.
            wanted_model = resolve_model_for_voice(req.voice)

            if wanted_model is None:
                # Might just be stale -- refresh once before giving up.
                try:
                    await refresh_models(client)
                except Exception as e:
                    log.warning("Could not refresh models: %s", e)
                wanted_model = resolve_model_for_voice(req.voice)

            if wanted_model is not None and wanted_model != _current_model:
                log.info("Switching model: %s -> %s", _current_model, wanted_model)
                try:
                    r = await client.post(
                        f"{XTTS_BASE_URL}/switch_model",
                        json={"model_name": wanted_model},
                        timeout=60,  # model loads can take a while, don't time out early
                    )
                except httpx.RequestError as e:
                    raise HTTPException(status_code=502, detail=f"Could not reach xtts-api-server to switch model: {e}")

                if r.status_code != 200:
                    raise HTTPException(
                        status_code=r.status_code,
                        detail=f"Failed to switch to model '{wanted_model}': {r.text}",
                    )
                _current_model = wanted_model

            speaker_wav = _speaker_cache.get(voice_key)

            if speaker_wav is None:
                # Voice not known yet -- try one refresh in case a new
                # character finished training since the shim last started,
                # before giving up.
                try:
                    await refresh_speakers(client)
                except Exception as e:
                    raise HTTPException(
                        status_code=502,
                        detail=f"Could not reach xtts-api-server to refresh speakers: {e}",
                    )
                speaker_wav = _speaker_cache.get(voice_key)

            if speaker_wav is None:
                known = ", ".join(sorted(_speaker_cache)) or "(none found)"
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown voice '{req.voice}'. Known voices: {known}",
                )

            payload = {
                "text": req.input,
                "speaker_wav": speaker_wav,
                "language": req.language or DEFAULT_LANGUAGE,
            }

            try:
                r = await client.post(f"{XTTS_BASE_URL}/tts_to_audio/", json=payload)
            except httpx.RequestError as e:
                raise HTTPException(status_code=502, detail=f"Could not reach xtts-api-server: {e}")

            if r.status_code != 200:
                # Surface XTTS's own error message rather than a generic
                # 500, since it's almost always something actionable (bad
                # path, model not loaded, etc).
                raise HTTPException(status_code=r.status_code, detail=r.text)

    return Response(content=r.content, media_type="audio/wav")


@app.get("/v1/audio/voices")
async def list_voices():
    """Not part of the official OpenAI API, but some clients probe this or
    similar endpoints to populate a voice picker. Harmless to expose."""
    return {"voices": [{"id": name, "name": name} for name in sorted(_speaker_cache)]}


@app.get("/healthz")
async def healthz():
    return {
        "ok": True,
        "xtts_base_url": XTTS_BASE_URL,
        "known_voices": sorted(_speaker_cache),
        "known_models": sorted(_model_cache.values()),
        "current_model": _current_model,
        "voice_model_overrides": VOICE_MODEL_OVERRIDES,
    }


@app.post("/admin/refresh_speakers")
async def admin_refresh_speakers():
    """Manually trigger a speaker + model list refresh, e.g. right after
    finishing a new training run, instead of waiting for the next
    unknown-voice request or a container restart."""
    async with httpx.AsyncClient(timeout=30) as client:
        await refresh_speakers(client)
        await refresh_models(client)
    return {"known_voices": sorted(_speaker_cache), "known_models": sorted(_model_cache.values())}


@app.post("/admin/switch_model")
async def admin_switch_model(model_name: str):
    """Manually switch which fine-tuned model xtts-api-server has loaded,
    since xtts-api-server only serves one model at a time."""
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(f"{XTTS_BASE_URL}/switch_model", json={"model_name": model_name})
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()
