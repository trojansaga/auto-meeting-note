# Continuous Screen Recording Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace screen recording with a persistent ScreenCaptureKit stream so pause/resume keeps the stream alive and MP4 compression happens during capture.

**Architecture:** Add a ScreenCaptureKit-backed screen recording module that owns a long-lived `SCStream` and rotates `SCRecordingOutput` segments on pause/resume. Update `Recorder` to use it for screen mode while preserving the existing audio-only path and mic merge behavior.

**Tech Stack:** Python 3.11, PyObjC ScreenCaptureKit, PyObjC Quartz, ffmpeg, unittest

---

### Task 1: Add failing controller tests

**Files:**
- Create: `tests/test_continuous_screen_recorder.py`
- Test: `tests/test_continuous_screen_recorder.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement the minimal controller and helpers**
- [ ] **Step 4: Run test to verify it passes**

### Task 2: Implement ScreenCaptureKit-backed recorder

**Files:**
- Create: `continuous_screen_recorder.py`
- Modify: `recorder.py`

- [ ] **Step 1: Wire a persistent stream and recording-output rotation**
- [ ] **Step 2: Handle pause/resume/stop and segment finalization**
- [ ] **Step 3: Preserve mic capture and final mix behavior**
- [ ] **Step 4: Run targeted tests**

### Task 3: Integrate app flow

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Detect already-compressed MP4 output from the new recorder**
- [ ] **Step 2: Skip legacy post-compression when not needed**
- [ ] **Step 3: Run app-adjacent regression tests**

### Task 4: Verify end-to-end behavior

**Files:**
- Test: `tests/test_continuous_screen_recorder.py`
- Test: `tests/test_transcriber.py`

- [ ] **Step 1: Run targeted unit tests**
- [ ] **Step 2: Run the full Python test suite**
- [ ] **Step 3: Review resulting diff for unintended regressions**
