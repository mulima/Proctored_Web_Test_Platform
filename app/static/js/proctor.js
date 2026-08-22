/* Client-side proctoring for the web test platform.
 *
 * Why this runs here and not on the server: continuous detection means several frames a
 * second per candidate. Thirty candidates is thirty video streams, which no small
 * container can keep up with, and it would put continuous webcam video on the wire. Doing
 * it in the browser means the work scales with the number of laptops in the room and the
 * video never leaves the machine. Only events - and one still image per flag - go up.
 *
 * The two-stage cascade from the desktop build is preserved:
 *
 *   stage 1  a 160x120 grayscale frame, motion plus a lit-rectangle test, well under a
 *            millisecond, deciding whether stage 2 is worth running at all;
 *   stage 2  an ONNX phone detector via onnxruntime-web, only on frames stage 1 wakes.
 *
 * A skipped frame is NOT a negative reading. Treating it as one would keep resetting the
 * confirmation window, and a phone held still would never be confirmed - the same bug
 * that had to be fixed in the desktop version.
 */
(function (global) {
  "use strict";

  const CAPTURE_WIDTH = 320;
  const CAPTURE_HEIGHT = 240;
  const SMALL_WIDTH = 80;
  const SMALL_HEIGHT = 60;

  /* `??` rather than `||` throughout: a threshold deliberately set to 0 means "never
   * skip", and `||` would silently swallow it and restore the default. */
  function pick(value, fallback) {
    return value === undefined || value === null ? fallback : value;
  }

  function Prefilter(options) {
    options = options || {};
    this.motionThreshold = pick(options.motionThreshold, 6.0);
    this.brightAreaRatio = pick(options.brightAreaRatio, 0.004);
    this.maxSkipSeconds = pick(options.maxSkipSeconds, 2.0);
    this.previous = null;
    this.lastFullRunAt = 0;
    this.checked = 0;
    this.skipped = 0;
  }

  Prefilter.prototype.shouldRunModel = function (gray) {
    const now = performance.now() / 1000;
    this.checked += 1;

    if (now - this.lastFullRunAt >= this.maxSkipSeconds) {
      this.lastFullRunAt = now;
      this.previous = gray;
      return { run: true, reason: "watchdog" };
    }
    const previous = this.previous;
    this.previous = gray;
    if (!previous) {
      this.lastFullRunAt = now;
      return { run: true, reason: "first_frame" };
    }

    let diff = 0;
    for (let i = 0; i < gray.length; i++) diff += Math.abs(gray[i] - previous[i]);
    const motion = diff / gray.length;
    if (motion >= this.motionThreshold) {
      this.lastFullRunAt = now;
      return { run: true, reason: "motion:" + motion.toFixed(1) };
    }
    if (this.hasDeviceLikeBlob(gray)) {
      this.lastFullRunAt = now;
      return { run: true, reason: "bright_rectangle" };
    }
    this.skipped += 1;
    return { run: false, reason: "quiet" };
  };

  /* A lit phone screen is a compact bright patch that fills most of its bounding box.
   * A highlight on a forehead is diffuse and does not. */
  Prefilter.prototype.hasDeviceLikeBlob = function (gray) {
    const sorted = Float32Array.from(gray).sort();
    const cutoff = sorted[Math.floor(sorted.length * 0.98)];
    if (cutoff < 90) return false;

    const seen = new Uint8Array(gray.length);
    const frameArea = SMALL_WIDTH * SMALL_HEIGHT;
    const minArea = this.brightAreaRatio * frameArea;
    const maxArea = 0.25 * frameArea;
    const stack = [];

    for (let start = 0; start < gray.length; start++) {
      if (seen[start] || gray[start] <= cutoff) continue;
      let area = 0;
      let minX = SMALL_WIDTH, maxX = 0, minY = SMALL_HEIGHT, maxY = 0;
      stack.length = 0;
      stack.push(start);
      seen[start] = 1;

      while (stack.length) {
        const index = stack.pop();
        const x = index % SMALL_WIDTH;
        const y = (index / SMALL_WIDTH) | 0;
        area += 1;
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;

        if (x > 0) pushIf(index - 1);
        if (x < SMALL_WIDTH - 1) pushIf(index + 1);
        if (y > 0) pushIf(index - SMALL_WIDTH);
        if (y < SMALL_HEIGHT - 1) pushIf(index + SMALL_WIDTH);
      }
      if (area < minArea || area > maxArea) continue;
      const width = maxX - minX + 1;
      const height = maxY - minY + 1;
      if (width < 4 || height < 4) continue;
      const aspect = width / height;
      if (aspect < 0.3 || aspect > 3.2) continue;
      if (area / (width * height) < 0.55) continue;
      return true;

      function pushIf(next) {
        if (!seen[next] && gray[next] > cutoff) {
          seen[next] = 1;
          stack.push(next);
        }
      }
    }
    return false;
  };

  Prefilter.prototype.stats = function () {
    if (!this.checked) return "no frames checked";
    return this.skipped + " of " + this.checked + " frames skipped inference (" +
      Math.round((100 * this.skipped) / this.checked) + "%)";
  };

  /* ------------------------------------------------------------------ camera monitor */

  function Monitor(options) {
    this.settings = options.settings;
    this.onIncident = options.onIncident;      // (category, detail, snapshotDataUrl)
    this.onState = options.onState || function () {};
    this.video = options.video;
    this.canvas = options.canvas;
    this.context = this.canvas.getContext("2d", { willReadFrequently: true });
    this.small = document.createElement("canvas");
    this.small.width = SMALL_WIDTH;
    this.small.height = SMALL_HEIGHT;
    this.smallContext = this.small.getContext("2d", { willReadFrequently: true });

    this.prefilter = new Prefilter({});
    this.session = null;
    this.detector = null;
    this.running = false;
    this.stream = null;

    this.noFaceSince = null;
    this.absenceWarned = false;
    this.absenceFlagged = false;
    this.multipleSince = null;
    this.multipleFlagged = false;
    this.phoneSince = null;
    this.phoneFlagged = false;
    this.lastPhoneAt = 0;
    this.status = { camera: "starting", detector: "loading" };
  }

  Monitor.prototype.start = async function () {
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: { width: CAPTURE_WIDTH, height: CAPTURE_HEIGHT, facingMode: "user" },
        audio: false
      });
    } catch (error) {
      this.status.camera = "denied";
      this.onState(this.status);
      this.onIncident("CAMERA_UNAVAILABLE",
        "The camera could not be started: " + (error && error.name ? error.name : "unknown") + ".");
      return false;
    }

    this.video.srcObject = this.stream;
    await this.video.play().catch(function () {});
    this.canvas.width = CAPTURE_WIDTH;
    this.canvas.height = CAPTURE_HEIGHT;
    this.status.camera = "active";
    this.onState(this.status);

    // A candidate who stops the camera mid-test should not simply go quiet.
    const self = this;
    this.stream.getVideoTracks().forEach(function (track) {
      track.addEventListener("ended", function () {
        if (self.running) {
          self.status.camera = "stopped";
          self.onState(self.status);
          self.onIncident("CAMERA_STOPPED", "The camera was stopped during the test.");
        }
      });
    });

    await this.loadDetector();
    this.running = true;
    this.loop();
    return true;
  };

  Monitor.prototype.loadDetector = async function () {
    try {
      await loadScript(global.MBS_DATA.ortUrl);
      if (!global.ort) throw new Error("onnxruntime did not load");
      global.ort.env.wasm.numThreads = 1;
      this.session = await global.ort.InferenceSession.create(global.MBS_DATA.modelUrl, {
        executionProviders: ["wasm"]
      });
      this.inputName = this.session.inputNames[0];
      this.status.detector = "ready";
    } catch (error) {
      // No model, no WASM, or a slow link. Presence checks still work; phone detection
      // simply is not available, and the candidate is told so rather than being misled.
      this.session = null;
      this.status.detector = "unavailable";
    }
    this.onState(this.status);
  };

  Monitor.prototype.stop = function () {
    this.running = false;
    if (this.stream) this.stream.getTracks().forEach(function (t) { t.stop(); });
  };

  Monitor.prototype.loop = function () {
    const self = this;
    if (!this.running) return;
    this.tick()
      .catch(function () {})
      .then(function () {
        setTimeout(function () { self.loop(); }, 250);
      });
  };

  Monitor.prototype.tick = async function () {
    if (!this.running || this.video.readyState < 2) return;
    this.context.drawImage(this.video, 0, 0, CAPTURE_WIDTH, CAPTURE_HEIGHT);
    this.smallContext.drawImage(this.video, 0, 0, SMALL_WIDTH, SMALL_HEIGHT);
    const smallData = this.smallContext.getImageData(0, 0, SMALL_WIDTH, SMALL_HEIGHT).data;

    const gray = new Float32Array(SMALL_WIDTH * SMALL_HEIGHT);
    for (let i = 0, p = 0; i < smallData.length; i += 4, p++) {
      gray[p] = 0.299 * smallData[i] + 0.587 * smallData[i + 1] + 0.114 * smallData[i + 2];
    }

    await this.checkPresence();

    const decision = this.prefilter.shouldRunModel(gray);
    if (decision.run && this.session) {
      const seen = await this.detectPhone();
      // Only a real reading updates the phone state. A skipped frame says nothing.
      this.updatePhone(seen);
    }
  };

  /* Presence uses the platform FaceDetector where it exists, and falls back to a crude
   * but honest luminance-variance test: an empty chair in front of a static background
   * has far less structure than a person does. The fallback is deliberately conservative,
   * because a false "you have left" is worse than a missed one. */
  Monitor.prototype.checkPresence = async function () {
    let faces = null;
    if (global.FaceDetector) {
      try {
        if (!this.detector) this.detector = new global.FaceDetector({ fastMode: true });
        const found = await this.detector.detect(this.canvas);
        faces = found.length;
      } catch (error) {
        faces = null;
      }
    }
    if (faces === null) {
      const data = this.context.getImageData(0, 0, CAPTURE_WIDTH, CAPTURE_HEIGHT).data;
      let sum = 0, sumSquares = 0, count = 0;
      for (let i = 0; i < data.length; i += 16) {
        const value = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
        sum += value;
        sumSquares += value * value;
        count += 1;
      }
      const mean = sum / count;
      const variance = sumSquares / count - mean * mean;
      faces = variance > 260 ? 1 : 0;
    }
    this.updatePresence(faces);
  };

  Monitor.prototype.updatePresence = function (faceCount) {
    const now = Date.now() / 1000;
    const settings = this.settings;

    if (faceCount <= 0) {
      this.multipleSince = null;
      this.multipleFlagged = false;
      if (this.noFaceSince === null) this.noFaceSince = now;
      const away = now - this.noFaceSince;
      if (!this.absenceWarned && away >= settings.absence_warn_seconds) {
        this.absenceWarned = true;
        this.onState({ absent: true, absentSeconds: Math.round(away) });
      }
      if (!this.absenceFlagged && away >= settings.absence_flag_seconds) {
        this.absenceFlagged = true;
        this.onIncident("ABSENCE_FLAGGED",
          "No candidate was visible to the camera for " + Math.round(away) + " seconds.",
          this.snapshot());
      }
      return;
    }

    if (this.noFaceSince !== null) this.onState({ absent: false, absentSeconds: 0 });
    this.noFaceSince = null;
    this.absenceWarned = false;
    this.absenceFlagged = false;

    if (faceCount > 1) {
      if (this.multipleSince === null) this.multipleSince = now;
      if (!this.multipleFlagged && now - this.multipleSince >= settings.multiple_faces_seconds) {
        this.multipleFlagged = true;
        this.onIncident("MULTIPLE_FACES",
          faceCount + " people were visible to the camera.", this.snapshot());
      }
    } else {
      this.multipleSince = null;
      this.multipleFlagged = false;
    }
  };

  Monitor.prototype.detectPhone = async function () {
    try {
      const size = 640;
      const work = document.createElement("canvas");
      work.width = size;
      work.height = size;
      work.getContext("2d").drawImage(this.video, 0, 0, size, size);
      const pixels = work.getContext("2d").getImageData(0, 0, size, size).data;

      const input = new Float32Array(3 * size * size);
      const plane = size * size;
      for (let i = 0, p = 0; i < pixels.length; i += 4, p++) {
        input[p] = pixels[i] / 255;
        input[plane + p] = pixels[i + 1] / 255;
        input[2 * plane + p] = pixels[i + 2] / 255;
      }
      const tensor = new global.ort.Tensor("float32", input, [1, 3, size, size]);
      const feeds = {};
      feeds[this.inputName] = tensor;
      const output = await this.session.run(feeds);
      return readPhoneScore(output) >= 0.35;
    } catch (error) {
      return false;
    }
  };

  Monitor.prototype.updatePhone = function (seen) {
    const now = Date.now() / 1000;
    if (seen) {
      if (this.phoneSince === null) this.phoneSince = now;
      if (!this.phoneFlagged && now - this.phoneSince >= this.settings.phone_confirm_seconds) {
        this.phoneFlagged = true;
        this.lastPhoneAt = now;
        this.onIncident("PHONE_DETECTED",
          "A phone-like device was visible to the camera.", this.snapshot());
      }
    } else if (this.phoneSince !== null && now - this.phoneSince > 2) {
      this.phoneSince = null;
      this.phoneFlagged = false;
    }
  };

  Monitor.prototype.snapshot = function () {
    if (!this.settings.snapshots_enabled) return null;
    try {
      const width = Math.min(this.settings.snapshot_max_width || 480, CAPTURE_WIDTH);
      const scale = width / CAPTURE_WIDTH;
      const out = document.createElement("canvas");
      out.width = width;
      out.height = Math.round(CAPTURE_HEIGHT * scale);
      out.getContext("2d").drawImage(this.canvas, 0, 0, out.width, out.height);
      return out.toDataURL("image/jpeg", 0.7);
    } catch (error) {
      return null;
    }
  };

  function readPhoneScore(output) {
    const key = Object.keys(output)[0];
    const tensor = output[key];
    const dims = tensor.dims;
    const data = tensor.data;
    if (!dims || dims.length < 3) return 0;

    // Ultralytics exports come out as [1, 4+classes, boxes]; the phone class is 339 for
    // the Open Images model and 67 for COCO. Which one is in play depends on the file,
    // so read whichever index exists.
    const attributes = dims[1];
    const boxes = dims[2];
    const classCount = attributes - 4;
    const wanted = classCount > 400 ? [339] : [67];
    let best = 0;
    for (const classId of wanted) {
      if (classId >= classCount) continue;
      const rowStart = (4 + classId) * boxes;
      for (let i = 0; i < boxes; i++) {
        const score = data[rowStart + i];
        if (score > best) best = score;
      }
    }
    return best;
  }

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      if (document.querySelector('script[data-src="' + url + '"]')) return resolve();
      const element = document.createElement("script");
      element.src = url;
      element.dataset.src = url;
      element.onload = resolve;
      element.onerror = function () { reject(new Error("Could not load " + url)); };
      document.head.appendChild(element);
    });
  }

  global.MBSProctor = { Monitor: Monitor, Prefilter: Prefilter, readPhoneScore: readPhoneScore };
})(window);
