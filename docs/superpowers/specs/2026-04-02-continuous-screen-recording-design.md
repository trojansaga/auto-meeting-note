# Continuous Screen Recording Design

**Goal:** Keep the ScreenCaptureKit screen stream alive across pause/resume while producing compressed MP4 output during recording.

## Current Constraints

- `recorder.py` uses `screencapture` for video and `SystemAudioCapture` for system audio.
- Pause/resume stops and restarts the underlying capture processes, which breaks continuity.
- `compress_and_merge()` runs only after recording stops, so compression is not concurrent with capture.

## Chosen Architecture

- Replace screen-mode video capture with a persistent `SCStream`.
- Attach `SCRecordingOutput` to the stream so MP4 compression happens while recording.
- Model pause/resume as recording-output rotation, not stream restart.
- Keep audio-only recording on the existing path.
- Preserve optional mic capture through ffmpeg, segmented on pause/resume and merged after stop.

## Data Flow

1. Start screen recording:
   - Create one `SCStream` configured for the main display plus system audio.
   - Start stream capture once.
   - Add an `SCRecordingOutput` that writes the active MP4 segment.
   - Optionally start mic capture to a WAV sidecar.
2. Pause:
   - Remove the current `SCRecordingOutput` and wait for that segment to finalize.
   - Stop the current mic segment.
   - Keep `SCStream` running.
3. Resume:
   - Add a new `SCRecordingOutput` segment to the already running stream.
   - Start a new mic segment.
4. Stop:
   - Finalize the current recording output.
   - Stop the stream.
   - Concatenate MP4 segments if needed.
   - Concatenate mic segments if needed, then mix mic into the final MP4 when enabled.

## Pause/Resume Semantics

- Screen capture stream continuity is preserved across pause/resume.
- Paused time is excluded from the final output by closing the active segment and dropping stream samples until resume.
- Each resumed span becomes a new MP4 segment.

## Error Handling

- Surface stream start errors immediately.
- Track segment start/finalize failures through delegate callbacks and fail the operation on pause/resume/stop if a segment errors.
- Keep partially finalized segment files for debugging when concat or mix fails.
- Fall back to the existing post-processing path only if the new screen recorder is unavailable before start.

## Testing Strategy

- Add unit tests for a pure controller that manages:
  - stream starts only once
  - pause/resume rotates segments without stopping the stream
  - stop concatenates multiple segments in order
  - segment failures propagate
- Keep existing transcriber tests unchanged.
- Run targeted unit tests and then the full Python test suite before completion.
