# xtts-openai-shim

A small FastAPI adapter that translates Open WebUI's OpenAI-compatible
`/v1/audio/speech` TTS requests into the shape your `xtts-api-server`
deployment actually expects.

## Why this exists

Open WebUI's Audio settings only know how to talk to a TTS engine that
speaks the OpenAI API shape:

```
POST /v1/audio/speech
{ "model": "tts-1", "input": "text to speak", "voice": "belldandy" }
```
-> raw audio bytes

`xtts-api-server` speaks its own shape instead:

```
POST /tts_to_audio/
{ "text": "...", "speaker_wav": "Belldandy/reference.wav", "language": "en" }
```
-> raw wav bytes

This shim sits in between, translating one into the other, and keeps a
cache of known "voices" by calling `xtts-api-server`'s own `/speakers`
endpoint -- so a new character shows up automatically once you either
restart the shim, wait for it to hit an unknown-voice request, or hit
`/admin/refresh_speakers` yourself.

## Confirmed against a real xtts-api-server instance

- `GET /speakers` returns entries like:
  ```json
  {"name": "Belldandy", "voice_id": "Belldandy", "preview_url": ".../sample/Belldandy/reference.wav"}
  ```
  which tells us the `speaker_wav` value to send back is `"Belldandy/reference.wav"`.
- `POST /switch_model` takes `{"model_name": "Belldandy"}`.
- `POST /tts_to_audio/` takes `{"text", "speaker_wav", "language"}` and
  returns wav bytes directly.

If your `/speakers` response has a different shape (e.g. a flat filename
with no subfolder, like `"belldandy.wav"`), adjust the `f"{name}/reference.wav"`
line in `refresh_speakers()` in `app.py` to match what your install
actually reports.

## Automatic model switching (one XTTS instance, multiple characters)

`xtts-api-server` only has one fine-tuned model loaded in memory at a
time. Rather than running multiple XTTS containers (one per character,
each eating several GB of VRAM permanently), this shim automatically
calls `/switch_model` on your behalf whenever a request's `voice` needs a
different model than whatever is currently loaded.

- Requesting the **same** character as last time: no switch call at all,
  goes straight to synthesis (fast -- this is what a real conversation
  feels like).
- Requesting a **different** character: the shim switches models first,
  which costs roughly the same load time you saw manually in `/docs`
  (several seconds to ~10-30s depending on model size), then generates.
- The shim tracks what it believes is currently loaded in memory, so a
  restart of the shim (not xtts-api-server) will trigger one extra
  switch call on the very next request even if nothing actually changed,
  just to be sure it's in sync.

By default, a voice name is matched to a model name case-insensitively
(e.g. voice `"belldandy"` -> model `"Belldandy"`, matched against
whatever `/get_models_list` reports). If you ever want a wake-word/voice
name that doesn't match the model's folder name (e.g. calling the model
"doc" in chat but the model folder is actually named "Keiichi"), set:

```
VOICE_MODEL_OVERRIDES={"doc":"Keiichi"}
```

as an environment variable (valid JSON, voice name -> model name).

## Standalone test page

`voice-lab.html` is a simple browser page for testing without touching
`/docs`: pick a character from a dropdown (auto-populated from the
shim's `/healthz`), type a line, click **Go**, hear it play back. It
talks to the exact same `/v1/audio/speech` endpoint Open WebUI uses, so
if a voice works here, it'll work there too.

To use it: open `voice-lab.html` directly in a browser (double-click it,
or host it anywhere -- it doesn't need to be served from the same place
as the shim), type in your shim's address (e.g.
`http://192.168.1.2:8021`), and click the refresh icon next to it to
load the voice list.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `XTTS_BASE_URL` | `http://192.168.1.2:8020` | Base URL of your xtts-api-server |
| `DEFAULT_LANGUAGE` | `en` | Used if a request doesn't specify one |
| `DEFAULT_MODEL` | (unset) | If set, switches to this model on shim startup |
| `VOICE_MODEL_OVERRIDES` | `{}` | JSON map of voice name -> model name, for when they don't match (e.g. a custom wake-word) |

## Deploying

See `truenas-compose.yaml`. Same pattern as `voice-metadata-app`:
push to GitHub, let the Action build the image, install as a TrueNAS
custom app, fill in the real image path.

## Wiring into Open WebUI

In Open WebUI: **Settings -> Audio -> Text-to-Speech Engine**
- Engine: OpenAI
- API Base URL: `http://<ryozanpaku-ip>:8021/v1`
- API Key: anything non-empty (the shim doesn't check it)
- Voice: `belldandy` (lowercase, matches the cached voice_id)

## Testing it directly

```bash
curl -X POST http://<ryozanpaku-ip>:8021/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input": "Hello, this is a test.", "voice": "belldandy"}' \
  --output test.wav
```

```bash
# check what voices the shim currently knows about
curl http://<ryozanpaku-ip>:8021/healthz

# force it to re-pull the speaker list from xtts-api-server
curl -X POST http://<ryozanpaku-ip>:8021/admin/refresh_speakers

# switch which model xtts-api-server has loaded
curl -X POST "http://<ryozanpaku-ip>:8021/admin/switch_model?model_name=Belldandy"
```
