/* The sitting page: question rendering, autosave, full-screen enforcement, submission.
 *
 * The server owns the clock. This page counts down locally for a smooth display but takes
 * the server's figure as a ceiling on every poll, so a device clock cannot buy time.
 */
(function (global) {
  "use strict";

  const data = global.MBS_DATA;
  const settings = data.settings;
  const questions = data.questions;
  const required = data.sectionCRequired;

  let current = Math.min(Math.max(0, data.currentQuestion), Math.max(0, questions.length - 1));
  let remaining = data.remaining;
  let submitted = false;
  let started = false;
  let dirty = false;
  let saving = false;
  let monitor = null;
  let lastTickAt = performance.now();

  const el = (id) => document.getElementById(id);
  const timer = el("timer");
  const alerts = el("alerts");

  /* ------------------------------------------------------------------- incidents */

  const reported = {};
  async function reportIncident(category, detail, snapshot) {
    if (submitted) return;
    try {
      const response = await fetch(data.coursePrefix + "/api/incident", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ category, detail: detail || "", snapshot: snapshot || null })
      });
      if (response.ok) applyStatus(await response.json());
    } catch (error) {
      /* offline: the incident is lost, but the page must keep working */
    }
  }

  function showAlert(key, message, kind, milliseconds) {
    let node = document.querySelector('[data-alert="' + key + '"]');
    if (!node) {
      node = document.createElement("div");
      node.className = "alert " + (kind || "");
      node.dataset.alert = key;
      alerts.appendChild(node);
    }
    node.className = "alert " + (kind || "");
    node.textContent = message;
    clearTimeout(node.dataset.timer);
    if (milliseconds) {
      node.dataset.timer = setTimeout(() => node.remove(), milliseconds);
    }
  }
  function clearAlert(key) {
    const node = document.querySelector('[data-alert="' + key + '"]');
    if (node) node.remove();
  }

  /* -------------------------------------------------------- full-screen enforcement */

  let blackoutReason = "";
  let blackoutSince = null;
  let fullscreenReported = false;
  let hiddenReported = false;

  function isEffectivelyFullscreen() {
    if (document.fullscreenElement) return true;
    return Math.abs(window.innerHeight - screen.height) <= 4 &&
           Math.abs(window.innerWidth - screen.width) <= 4;
  }

  function enterFullscreen() {
    const target = document.documentElement;
    if (target.requestFullscreen) target.requestFullscreen().catch(() => {});
  }

  function showBlackout(title, message, reason) {
    if (submitted || !started) return;
    el("blackout").style.display = "flex";
    document.body.classList.add("blanked");
    el("blackoutTitle").textContent = title;
    el("blackoutMessage").textContent = message;
    if (blackoutReason !== reason) {
      blackoutReason = reason;
      blackoutSince = Date.now();
    }
  }

  function hideBlackout() {
    el("blackout").style.display = "none";
    document.body.classList.remove("blanked");
    el("blackoutCount").textContent = "";
    blackoutReason = "";
    blackoutSince = null;
    fullscreenReported = false;
  }

  function evaluateScreen() {
    if (submitted || !started) { hideBlackout(); return; }

    if (document.hidden) {
      showBlackout("Test window is not in view",
        "Return to the test window now. Your paper is hidden and this interruption is being timed.",
        "hidden");
      const away = (Date.now() - blackoutSince) / 1000;
      if (!hiddenReported && away >= settings.hidden_warn_seconds) {
        hiddenReported = true;
        reportIncident("WINDOW_HIDDEN",
          "Test window minimised or switched away from for " + Math.round(away) + " seconds.");
      }
      el("blackoutCount").textContent = formatDuration(away) + " away";
      return;
    }

    if (!isEffectivelyFullscreen()) {
      showBlackout("Return to full-screen test mode",
        "Your paper is hidden until this window is back in full-screen mode. This interruption has been recorded.",
        "fullscreen");
      const away = (Date.now() - blackoutSince) / 1000;
      if (!fullscreenReported && away >= settings.fullscreen_grace_seconds) {
        fullscreenReported = true;
        reportIncident("FULLSCREEN_EXIT",
          "Left full-screen test mode for more than " + settings.fullscreen_grace_seconds + " seconds.");
      }
      el("blackoutCount").textContent = formatDuration(away);
      return;
    }

    hiddenReported = false;
    hideBlackout();
  }

  document.addEventListener("fullscreenchange", evaluateScreen);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) hiddenReported = false;
    evaluateScreen();
  });
  window.addEventListener("resize", evaluateScreen);
  window.addEventListener("blur", () => setTimeout(evaluateScreen, 250));
  window.addEventListener("focus", evaluateScreen);
  setInterval(evaluateScreen, 500);

  // requestFullscreen needs a gesture, so any click or key doubles as consent to return.
  ["click", "keydown"].forEach((name) => {
    document.addEventListener(name, () => {
      if (started && !submitted && !document.hidden && !isEffectivelyFullscreen()) enterFullscreen();
    }, true);
  });

  /* --------------------------------------------------------------- key and copy rules */

  const BLOCKED = [
    { key: "p", ctrl: true, name: "Print" },
    { key: "s", ctrl: true, name: "Save page" },
    { key: "u", ctrl: true, name: "View source" },
    { key: "w", ctrl: true, name: "Close tab" },
    { key: "n", ctrl: true, name: "New window" },
    { key: "t", ctrl: true, name: "New tab" },
    { key: "r", ctrl: true, name: "Reload" },
    { key: "i", ctrl: true, shift: true, name: "Developer tools" },
    { key: "j", ctrl: true, shift: true, name: "Developer console" },
    { key: "c", ctrl: true, shift: true, name: "Element inspector" },
    { key: "s", ctrl: true, shift: true, name: "Windows screen clip" }
  ];

  document.addEventListener("keydown", (event) => {
    if (!started || submitted) return;
    const key = (event.key || "").toLowerCase();
    if (key === "f12" || key === "f5") {
      event.preventDefault();
      reportIncident("SHORTCUT_BLOCKED", "Blocked " + event.key + ".");
      return;
    }
    if (settings.block_shortcut_keys) {
      for (const combo of BLOCKED) {
        if (key !== combo.key || event.ctrlKey !== !!combo.ctrl) continue;
        if (combo.shift && !event.shiftKey) continue;
        if (!combo.shift && event.shiftKey) continue;
        event.preventDefault();
        reportIncident("SHORTCUT_BLOCKED", "Blocked shortcut: " + combo.name + ".");
        return;
      }
    }
    if (key === "printscreen") handleCaptureKey(event);
  }, true);

  document.addEventListener("keyup", (event) => {
    if ((event.key || "").toLowerCase() === "printscreen") handleCaptureKey(event);
  }, true);

  function handleCaptureKey(event) {
    if (!started || submitted) return;
    if (event && event.preventDefault) event.preventDefault();
    try { navigator.clipboard.writeText(""); } catch (error) { /* permission denied is fine */ }
    reportIncident("SCREENSHOT_KEY", "A screen-capture key was pressed during the test.",
      monitor ? monitor.snapshot() : null);
    showAlert("capture", "Screen capture is not permitted during this test. The attempt has been recorded.", "warn", 12000);
  }

  if (settings.block_context_menu) {
    document.addEventListener("contextmenu", (event) => {
      if (!started) return;
      event.preventDefault();
      reportIncident("CONTEXT_MENU_BLOCKED", "Right-click menu blocked.");
    });
  }
  if (settings.block_copy_paste) {
    ["copy", "cut"].forEach((name) => document.addEventListener(name, (event) => {
      if (!started) return;
      event.preventDefault();
      reportIncident("COPY_BLOCKED", "Copy or cut blocked.");
    }));
    document.addEventListener("paste", (event) => {
      if (!started) return;
      event.preventDefault();
      reportIncident("PASTE_BLOCKED", "Paste into an answer box blocked.");
      showAlert("paste", "Pasting into an answer is not permitted. Type your answer.", "warn", 8000);
    });
    document.addEventListener("dragstart", (event) => { if (started) event.preventDefault(); });
  }

  /* ---------------------------------------------------------------------- rendering */

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function sectionName(section) {
    return section === "A" ? "Section A" : section === "B" ? "Section B" : "Section C";
  }

  function selectedCount() {
    return questions.filter((q) => q.section === "C" && q.selected).length;
  }

  function sectionCSummary() {
    const badges = questions.filter((q) => q.section === "C").map((q, index) => {
      const classes = ["badge"];
      if (q.selected) classes.push("selected");
      if ((q.value || "").trim()) classes.push("written");
      const state = q.selected ? "SELECTED"
        : (q.value || "").trim() ? "Written, not selected" : "Not selected";
      return '<span class="' + classes.join(" ") + '">C' + (index + 1) + ": " + state + "</span>";
    }).join("");
    return '<div class="sectionc"><strong>Section C: ' + selectedCount() + " of " +
      required + " selected</strong>" + badges + "</div>";
  }

  function render() {
    const question = questions[current];
    if (!question) return;
    let body = '<div class="progress"><span>Question ' + (current + 1) + " of " +
      questions.length + "</span><span>" + sectionName(question.section) +
      (question.marks ? " &middot; " + question.marks + " marks" : "") + "</span></div>";

    if (question.section === "A") {
      body += "<h2>" + escapeHtml(question.prompt) + "</h2>";
      body += question.options.map((option, index) => {
        const letter = String.fromCharCode(65 + index);
        const checked = question.value === letter ? " checked" : "";
        return '<label class="option"><input type="radio" name="mc" value="' + letter + '"' +
          checked + "> " + letter + ". " + escapeHtml(option) + "</label>";
      }).join("");
    } else if (question.section === "B") {
      body += "<h2>" + escapeHtml(question.prompt) + "</h2>" +
        '<textarea id="answerBox">' + escapeHtml(question.value || "") + "</textarea>";
    } else {
      body += sectionCSummary();
      body += "<h2>" + escapeHtml(question.title || "Section C") + "</h2>";
      body += '<label class="selectbox' + (question.selected ? " selected" : "") + '">' +
        '<input type="checkbox" id="selectC"' + (question.selected ? " checked" : "") + ">" +
        "<span>Select this question for marking<br><small>" +
        (question.selected ? "This question WILL be submitted."
                           : "This question is NOT selected.") + "</small></span></label>";
      body += '<div class="prompt">' + escapeHtml(question.prompt) + "</div>" +
        '<textarea id="answerBox">' + escapeHtml(question.value || "") + "</textarea>" +
        "<p class='muted'>Choose exactly " + required + " Section C question(s) before submitting.</p>";
    }

    el("questionPage").innerHTML = body;
    el("prevBtn").disabled = !canGoPrevious();
    el("prevBtn").style.visibility = question.section === "A" ? "hidden" : "visible";
    el("nextBtn").disabled = current >= questions.length - 1;
    el("submitBtn").style.display = current >= questions.length - 1 ? "" : "none";
    wireInputs();
  }

  function wireInputs() {
    const box = el("answerBox");
    if (box) box.addEventListener("input", () => { capture(); dirty = true; });
    const select = el("selectC");
    if (select) {
      select.addEventListener("change", () => {
        if (select.checked && selectedCount() >= required && !questions[current].selected) {
          select.checked = false;
          showAlert("sectionc", "Section C allows exactly " + required +
            " selected question(s). Deselect another first.", "warn", 6000);
          return;
        }
        capture();
        dirty = true;
        render();
      });
    }
    document.querySelectorAll('input[name="mc"]').forEach((input) => {
      input.addEventListener("change", () => { capture(); dirty = true; });
    });
  }

  function capture() {
    const question = questions[current];
    if (!question) return;
    if (question.section === "A") {
      const checked = document.querySelector('input[name="mc"]:checked');
      question.value = checked ? checked.value : "";
    } else {
      const box = el("answerBox");
      if (box) question.value = box.value;
      const select = el("selectC");
      if (select) question.selected = select.checked;
    }
  }

  // Section A is forward-only: once past a multiple-choice question it stays answered.
  function canGoPrevious() {
    if (current <= 0) return false;
    return questions[current].section !== "A" && questions[current - 1].section !== "A";
  }

  function next() {
    capture();
    if (current < questions.length - 1) current += 1;
    render();
    save();
  }
  function previous() {
    if (!canGoPrevious()) return;
    capture();
    current -= 1;
    render();
    save();
  }

  /* ------------------------------------------------------------------------ saving */

  async function save() {
    if (submitted || saving) return;
    capture();
    saving = true;
    el("saveChip").textContent = "Saving";
    el("saveChip").className = "chip";
    try {
      const response = await fetch(data.coursePrefix + "/api/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_question: current,
          answers: questions.map((q) => ({
            question_id: q.id, value: q.value || "", selected: !!q.selected
          }))
        })
      });
      if (response.ok) {
        applyStatus(await response.json());
        dirty = false;
        el("saveChip").textContent = "Saved";
        el("saveChip").className = "chip ok";
        clearAlert("offline");
      } else {
        el("saveChip").textContent = "Not saved";
        el("saveChip").className = "chip bad";
      }
    } catch (error) {
      el("saveChip").textContent = "Offline";
      el("saveChip").className = "chip bad";
      showAlert("offline",
        "Connection lost. Your answers are held in this page and will be saved when the connection returns. Do not close this window.",
        "warn");
    } finally {
      saving = false;
    }
  }

  setInterval(() => { if (dirty) save(); }, 5000);

  /* ------------------------------------------------------------------------ status */

  function applyStatus(status) {
    if (!status) return;
    if (typeof status.remaining_seconds === "number") {
      remaining = Math.min(remaining, status.remaining_seconds);
    }
    if (typeof status.strike_count === "number") {
      const chip = el("strikeChip");
      chip.textContent = "Incidents recorded: " + status.strike_count +
        (status.flag_after ? " of " + status.flag_after + " before review" : "");
      chip.className = "chip " + (status.flagged ? "bad" : status.strike_count ? "" : "ok");
    }
    if (status.locked && !submitted) finishUp(data.coursePrefix + "/submitted");
  }

  async function pollStatus() {
    if (submitted) return;
    try {
      const response = await fetch(data.coursePrefix + "/api/status");
      if (response.ok) applyStatus(await response.json());
    } catch (error) { /* handled by the save path */ }
  }
  setInterval(pollStatus, 5000);

  function formatDuration(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
    return minutes + ":" + String(seconds % 60).padStart(2, "0");
  }

  function tick() {
    const now = performance.now();
    if (!submitted && started) {
      remaining = Math.max(0, remaining - (now - lastTickAt) / 1000);
    }
    lastTickAt = now;
    const whole = Math.floor(remaining);
    timer.textContent = String(Math.floor(whole / 3600)).padStart(2, "0") + ":" +
      String(Math.floor((whole % 3600) / 60)).padStart(2, "0") + ":" +
      String(whole % 60).padStart(2, "0");
    timer.className = whole <= 900 ? "timer low" : "timer";
    if (started && !submitted && whole <= 0) {
      doSubmit(true, "Time expired.");
      return;
    }
    setTimeout(tick, 1000);
  }

  /* -------------------------------------------------------------------- submission */

  async function manualSubmit() {
    capture();
    if (selectedCount() !== required) {
      showAlert("sectionc", "Select exactly " + required +
        " Section C question(s) before submitting. You have " + selectedCount() + ".", "warn", 10000);
      return;
    }
    if (!confirm("Submit your final paper now? This cannot be undone and you cannot sit again.")) return;
    await doSubmit(false, "");
  }

  async function review() {
    capture();
    await save();
    if (remaining <= 0) {
      await doSubmit(true, "Time expired.");
      return;
    }
    try {
      const response = await fetch(data.coursePrefix + "/api/review");
      if (response.status === 409) {
        await doSubmit(true, "Time expired.");
        return;
      }
      if (!response.ok) throw new Error("Review failed");
      const html = await response.text();
      const parsed = new DOMParser().parseFromString(html, "text/html");
      el("reviewContent").innerHTML = parsed.body.innerHTML;
      el("reviewOverlay").style.display = "flex";
      el("reviewOverlay").setAttribute("aria-hidden", "false");
    } catch (error) {
      showAlert("review", "Could not load the final review. Your answers remain saved.", "warn", 10000);
    }
  }

  function closeReview() {
    el("reviewOverlay").style.display = "none";
    el("reviewOverlay").setAttribute("aria-hidden", "true");
  }

  async function submitReview() {
    closeReview();
    await doSubmit(false, "");
  }

  async function doSubmit(auto, reason) {
    if (submitted) return;
    capture();
    await save();
    try {
      const response = await fetch(data.coursePrefix + "/api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ auto: !!auto, reason: reason || "" })
      });
      const payload = await response.json();
      if (!response.ok) {
        showAlert("submit", payload.error || "Submission failed.", "warn", 12000);
        return;
      }
      finishUp(payload.redirect || data.coursePrefix + "/submitted");
    } catch (error) {
      showAlert("submit",
        "Could not reach the server to submit. Check your connection - your answers are saved.",
        "warn");
    }
  }

  function finishUp(url) {
    submitted = true;
    if (monitor) monitor.stop();
    hideBlackout();
    if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(() => {});
    global.location.href = url;
  }

  /* ----------------------------------------------------------------------- preflight */

  function onMonitorState(state) {
    if (state.camera) {
      const chip = el("cameraChip");
      const text = { active: "Camera: active", denied: "Camera: not allowed",
                     stopped: "Camera: stopped", starting: "Camera: starting" }[state.camera];
      chip.textContent = text || "Camera: unknown";
      chip.className = "chip " + (state.camera === "active" ? "ok" : "bad");
    }
    if (state.detector) {
      const chip = el("detectorChip");
      chip.textContent = state.detector === "ready" ? "Detector: on" : "Detector: unavailable";
      chip.className = "chip " + (state.detector === "ready" ? "ok" : "");
    }
    if (state.absent === true) {
      showAlert("absent", "You are not visible to the camera. Return to your seat now.", "warn");
    } else if (state.absent === false) {
      clearAlert("absent");
    }
  }

  function onIncident(category, detail, snapshot) {
    reportIncident(category, detail, snapshot);
    if (category === "PHONE_DETECTED") {
      showAlert("phone",
        "A phone or similar device was detected in the camera view. Put it away now - this has been recorded.",
        "warn", settings.phone_warning_display_seconds * 1000);
    }
    if (category === "MULTIPLE_FACES") {
      showAlert("multi", "More than one person is visible to the camera. This has been recorded.",
        "warn", settings.phone_warning_display_seconds * 1000);
    }
  }

  async function beginTest() {
    started = true;
    el("preflight").style.display = "none";
    enterFullscreen();
    render();
    save();
    evaluateScreen();
    if (monitor) await monitor.start();
  }

  async function boot() {
    render();
    tick();
    monitor = new global.MBSProctor.Monitor({
      settings,
      video: el("proctorVideo"),
      canvas: el("proctorCanvas"),
      onIncident,
      onState: onMonitorState
    });
    el("preflightStatus").textContent =
      "Ready. Click Begin - the paper opens in full screen and the camera starts.";
    el("beginButton").disabled = false;
    el("beginButton").addEventListener("click", beginTest);
  }

  window.addEventListener("beforeunload", (event) => {
    if (!submitted && started && dirty) {
      capture();
      navigator.sendBeacon(data.coursePrefix + "/api/save", new Blob([JSON.stringify({
        current_question: current,
        answers: questions.map((q) => ({
          question_id: q.id, value: q.value || "", selected: !!q.selected
        }))
      })], { type: "application/json" }));
      event.preventDefault();
      event.returnValue = "";
    }
  });

  global.MBS = { next, previous, manualSubmit, review, closeReview, submitReview, enterFullscreen };
  boot();
})(window);
