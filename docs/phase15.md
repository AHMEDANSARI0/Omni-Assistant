# PHASE 15 - Image Tools + Vision

Phase 15 adds two deliberately separate image capabilities:

1. **Local image tools** that inspect, convert, transform and compare files
   on this PC. They call no provider and need no vision setting.
2. **Provider vision** that sends **one exact workspace image and one exact
   question** to a configured vision-capable model, only after the user has
   enabled the vision gate and approved that send.

There is no AI image generation or image editing by prompt in this phase.

## New tools

| Tool | What it does | Approval/network |
|---|---|---|
| `image.inspect` | Format, dimensions, mode, frames, alpha, SHA-256, average/dominant colors and a 64-bit average hash | Local/read-only |
| `image.convert` | Convert to a new file whose extension selects PNG, JPEG, WebP, BMP or TIFF | Local; never overwrites |
| `image.transform` | Crop, resize, thumbnail, clockwise rotate, flip and/or grayscale into a new file | Local; never overwrites |
| `image.compare` | Byte/pixel identity, average-hash distance and downsampled similarity for two images | Local/read-only |
| `image.analyze` | Ask one exact question about one image through Gemini, OpenRouter/OpenAI-compatible Chat Completions, or a vision-capable Ollama model | `PC_AGENT_ALLOW_VISION=1` plus fresh approval for every task/direct command |

Supported local input formats are PNG, JPEG, GIF, BMP, WebP and TIFF. For an
animated/multi-frame image, inspection reports the frame count and local
processing uses the first frame. EXIF orientation is normalized before
processing; output metadata/EXIF is not copied.

## Local safety limits

- Input file: at most 20,000,000 bytes.
- Decoded image: at most 10,000 px on either side and 40,000,000 pixels.
- Pillow's decompression-bomb guard is set to the same pixel cap.
- Outputs must stay inside the workspace and must not already exist.
- Output encoding uses a temporary file plus an atomic hard-link, so a race
  cannot silently overwrite another file.
- Convert/transform outputs are independently re-opened during task
  verification. Their SHA-256 is also recomputed by acceptance criteria.
- `image.convert` and `image.transform` may use the existing fresh-output
  recovery path after a collision or verification failure because the
  source survives and the retry writes a different new file.

## Direct CLI examples

Run commands one at a time.

```powershell
pc-agent image inspect photo.png
```

```powershell
pc-agent image convert photo.png photo.jpg --quality 90
```

```powershell
pc-agent image transform photo.png photo-small.png --max-dimension 1024
```

```powershell
pc-agent image transform photo.png photo-gray.png --width 800 --grayscale
```

```powershell
pc-agent image compare before.png after.png
```

Add `--json` to any command for structured output.

## Enabling provider vision

Vision is **off by default**. In your local `.env`, set:

```dotenv
PC_AGENT_ALLOW_VISION=1
# Optional: PC_AGENT_VISION_MAX_IMAGE_BYTES=10000000
```

Your existing provider order and keys are reused. The configured model must
actually accept image input:

- **Gemini:** the adapter sends Interactions API `type: "image"` content
  with inline base64 and MIME type.
- **OpenRouter / OpenAI / OpenAI-compatible:** the adapter sends a text part
  followed by an `image_url` data URL.
- **Ollama:** the adapter sends the raw base64 string in the message's
  `images` array. Choose an Ollama vision model; a text-only model will
  return a provider error.
- **Offline mock:** refuses image requests instead of pretending to see.

PNG/JPEG/WebP/GIF files within the byte limit are sent as stored. BMP/TIFF
or oversized supported images are locally re-encoded and, when needed,
downscaled to fit `PC_AGENT_VISION_MAX_IMAGE_BYTES`. The result reports the
sent MIME type, byte count, provider and model. Image bytes/base64 are not
included in the tool result.

### Direct vision example

```powershell
pc-agent image analyze desktop-screenshot.png --question "What window and error are visible?"
```

The command displays the exact image, question and provider route and asks
for approval. Use `--yes` only after reviewing that exact command.

For text extraction, ask explicitly:

```powershell
pc-agent image analyze notice.jpg --question "Transcribe all visible text exactly."
```

Text or instructions visible inside an image are treated as untrusted image
content. The vision system instruction tells the model to transcribe such
text only when the user asks and never to treat it as system instructions or
permission to act.

## Task examples

```powershell
pc-agent run "Inspect image photo.png"
```

```powershell
pc-agent run "Convert photo.png to jpg" --output photo.jpg
```

```powershell
pc-agent run "Resize image" --target photo.png --output photo-small.png --image-max-dimension 1024
```

```powershell
pc-agent run "Compare before.png and after.png"
```

```powershell
pc-agent run "Analyze image" --target desktop-screenshot.png --image-question "Describe the visible error"
```

Vision tasks stop for a fresh approval showing the exact path and question,
including when a stored workflow is replayed in a new task. AI planning may
propose only the read-only local `image.inspect` and `image.compare`; it may
never propose conversion, transformation or `image.analyze`.

## GUI

The new **Images** tab provides:

- Inspect
- Compare with a second workspace image
- Convert using the output extension
- Make Thumbnail with an optional grayscale switch
- Analyze after a confirmation dialog naming the exact image/question

Exact crop/rotate/flip and other advanced transform options are available
through the CLI/task options.

## Verification model

- Inspect/compare are independently repeated locally and must match.
- Convert/transform outputs are re-opened and their format, dimensions,
  mode, size and hash must match the tool report.
- Vision is **not resent** for verification. The orchestrator re-inspects
  the approved local image and requires the answer to be tied to the exact
  question, current image hash/dimensions, a non-mock provider/model and a
  sent size within the configured limit. The semantic correctness of a
  model answer cannot be deterministically proved and is not claimed as
  such.
- `image.analyze` is excluded from automatic recovery retries, so a failed
  verification or provider call never silently sends the image again.

## Limitations

- No image generation, inpainting, background removal, face recognition,
  object detection coordinates, batch folder processing or built-in OCR
  engine is included. Vision can be asked to transcribe visible text, but
  that remains model output.
- Only the first frame of animated/multi-frame inputs is converted,
  transformed or used for deterministic pixel statistics.
- JPEG/WebP output is lossy at the selected quality. Alpha is flattened on
  white for JPEG/BMP outputs.
- Provider support depends on the selected model actually accepting image
  inputs. A text-only model returns an error; fallback then follows the
  configured provider route and retry policy.
- External providers receive the approved image bytes. Use a local Ollama
  vision model when an image must not leave the PC.
