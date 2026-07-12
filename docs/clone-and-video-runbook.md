# Clone and video runbook (free path)

How to clone your voice and face and produce finished video at zero marginal
cost. Engines run on your Windows PC's GPU; the orchestrator calls them over
Tailscale. No subscriptions, no API keys, no per-minute charges. The paid
ElevenLabs/HeyGen path from the earlier build stays in the code as an
optional fallback but nothing requires it.

Every clone output still stops at the `clone_output` governance gate before
it moves past render. That rule is engine-independent.

## The free stack

| Capability | Engine | Licence | VRAM | Where |
|---|---|---|---|---|
| Voice clone | F5-TTS via `gpu_workers/voice_worker.py` (port 8020) | MIT | ~3 GB | GPU PC |
| Avatar | SadTalker via `gpu_workers/avatar_worker.py` (port 7861) | Apache-2.0 code | ~6 GB | GPU PC |
| Assembly | ffmpeg + Remotion (already installed) | free | none | orchestrator |

F5-TTS clones from a single 5 to 15 second reference clip and is the leading
open-weight zero-shot clone; quality with a clean reference sits close to
the paid services for narration. SadTalker animates one portrait photo into
a talking head with natural motion. MuseTalk (lip-sync onto real video of
you, closer to the HeyGen look) can slot behind the same worker contract
later; the orchestrator does not care which engine answers.

Licence note: F5-TTS code and SadTalker code are commercial-friendly; XTTS-v2
was avoided because its CPML licence restricts commercial use, which matters
for WijerCo output.

## Step 1: set up the GPU PC (~45 minutes, mostly downloads)

On the Windows PC, from the repo folder:

```
powershell -ExecutionPolicy Bypass -File deploy\gpu-worker-setup.ps1
```

It checks the GPU, creates a worker venv, installs F5-TTS (CUDA torch) and
SadTalker with checkpoints (~15 GB disk total), and prints the start
commands. No accounts anywhere.

## Step 2: your reference material (~15 minutes)

Voice: record one clean clip, 5 to 15 seconds, quiet room, natural pace,
saved as wav. Write down the exact transcript. Set on the PC:

```
VOICE_REF_AUDIO=C:\clone\aaron-ref.wav
VOICE_REF_TEXT=<exact transcript>
```

Face: one good portrait, front-on, even light, neutral background, jpg. Set:

```
AVATAR_PORTRAIT=C:\clone\aaron-portrait.jpg
SADTALKER_DIR=C:\Users\ajwij\SadTalker
```

That is the whole clone procedure. No upload, nothing leaves your machines,
and better reference material can replace these files any time.

## Step 3: start the workers

Two terminals on the PC (or register both with NSSM so they start on boot):

```
gpu_workers\.venv\Scripts\python -m gpu_workers.voice_worker
gpu_workers\.venv\Scripts\python -m gpu_workers.avatar_worker
```

Set `VOICE_OUT_DIR` and `AVATAR_OUT_DIR` to a folder the orchestrator
machine can also read (the OneDrive repo folder works, or a shared folder
over Tailscale) so returned file paths resolve on both sides.

## Step 4: point the orchestrator at them

In the orchestrator's `.env` (PC for now, mini PC after migration):

```
F5_TTS_URL=http://<gpu-pc-tailscale-ip>:8020
MEDIA_TOOL_SADTALKER_ENDPOINT=http://<gpu-pc-tailscale-ip>:7861
MEDIA_TOOL_DEFAULT_VOICE=f5-tts
MEDIA_TOOL_DEFAULT_AVATAR=sadtalker
```

On the same machine use `http://localhost:...`. Restart the orchestrator,
then:

```
python -m scripts.verify_media_providers
```

READY means voice, avatar, and assembly each have a working path.

## Step 5: first end-to-end video

By Telegram: "plan: 60-second talking head on the TEQSA teaching
qualification requirement". Or directly:

```
curl -X POST localhost:8000/production -H 'Content-Type: application/json' \
  -d '{"title":"TEQSA 60s talking head","format":"talking_head_clip","project":"thought-leadership"}'
```

The Content Studio agents write brief, research, script, storyboard. At
render, the plan calls f5-tts (script in your voice) then sadtalker (your
portrait speaking it); ffmpeg/Remotion assemble. The production stops at the
`clone_output` gate and you approve from your phone. Expect the first
SadTalker render to be slow (model warm-up); a 60-second clip is minutes,
not seconds, on a mid-range GPU.

## Quality expectations, stated plainly

Voice: with a good reference clip, F5-TTS narration is hard to tell from
paid cloning on spoken-word content. Avatar: SadTalker from one photo reads
as animated-photo, a step below HeyGen's video clone. If the avatar quality
bothers you after the first renders, the upgrade path is MuseTalk with two
minutes of real footage of you talking, behind the same worker contract,
still free. Ask for it and it is one session's work.

## If a render fails

Check the worker's terminal output first; both return the real error in the
HTTP response. `python -m scripts.verify_media_providers` from the
orchestrator machine isolates which side is broken. The two workers have
/health endpoints you can open in a browser.

## Consent and data

Your voice and face never leave your machines. The reference clip and
portrait are files on your PC; delete them and the clone capability is gone.
Do not clone anyone else's voice or face without written consent; the
`--i-consent` discipline from the cloud path applies to inputs here just the
same, it is simply not enforced by an upload step.
