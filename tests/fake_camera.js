/* A fake webcam for the browser tests.
 *
 * This container's Chromium exposes no capture devices at all, and --use-fake-device
 * does not work here, so getUserMedia is replaced with a canvas captureStream. That is
 * better than a fake device file anyway: the test controls the pixels exactly, so the
 * presence rules can be driven deterministically instead of hoping a synthetic pattern
 * happens to trip them.
 *
 * window.__fakeCam.setScene(name) switches what the camera "sees":
 *   person  - high luminance variance, which the presence check reads as someone there
 *   empty   - a flat wall, read as nobody in front of the screen
 *   phone   - a person plus a bright device-shaped rectangle, which wakes the prefilter
 */
(() => {
  const WIDTH = 320;
  const HEIGHT = 240;
  const canvas = document.createElement("canvas");
  canvas.width = WIDTH;
  canvas.height = HEIGHT;
  const context = canvas.getContext("2d");

  let scene = "person";
  let frame = 0;

  function drawPerson() {
    // Coarse high-contrast structure: plenty of luminance variance, the way a lit face
    // and shoulders against a background look to a variance test.
    for (let y = 0; y < HEIGHT; y += 40) {
      for (let x = 0; x < WIDTH; x += 40) {
        const dark = ((x / 40) + (y / 40)) % 2 === 0;
        context.fillStyle = dark ? "#1e1e1e" : "#d8d8d8";
        context.fillRect(x, y, 40, 40);
      }
    }
  }

  function drawEmpty() {
    context.fillStyle = "#4a4a4a";
    context.fillRect(0, 0, WIDTH, HEIGHT);
    // A trace of sensor noise, so it is a plausible frame rather than a synthetic flat.
    context.fillStyle = "rgba(255,255,255,0.02)";
    context.fillRect(0, (frame * 3) % HEIGHT, WIDTH, 2);
  }

  function drawPhone() {
    drawPerson();
    context.fillStyle = "#fbfbfb";
    const x = 120 + ((frame * 2) % 40);
    context.fillRect(x, 70, 56, 104);   // roughly 1:2, a phone held towards the camera
  }

  function draw() {
    frame += 1;
    if (scene === "empty") drawEmpty();
    else if (scene === "phone") drawPhone();
    else drawPerson();
    requestAnimationFrame(draw);
  }
  draw();

  const stream = canvas.captureStream(15);

  const fake = {
    getUserMedia: async (constraints) => {
      if (!constraints || !constraints.video) throw new DOMException("no video", "NotFoundError");
      if (window.__fakeCam.failNext) {
        window.__fakeCam.failNext = false;
        throw new DOMException("Requested device not found", "NotFoundError");
      }
      return stream;
    },
    enumerateDevices: async () => [
      { kind: "videoinput", deviceId: "fake", label: "Fake camera", groupId: "fake" }
    ],
    addEventListener: () => {},
    removeEventListener: () => {}
  };

  if (!navigator.mediaDevices) {
    Object.defineProperty(navigator, "mediaDevices", { value: fake, configurable: true });
  } else {
    Object.defineProperty(navigator.mediaDevices, "getUserMedia", {
      value: fake.getUserMedia, configurable: true, writable: true
    });
    Object.defineProperty(navigator.mediaDevices, "enumerateDevices", {
      value: fake.enumerateDevices, configurable: true, writable: true
    });
  }

  window.__fakeCam = {
    canvas,
    stream,
    failNext: false,
    setScene: (name) => { scene = name; },
    getScene: () => scene,
    stopTracks: () => stream.getTracks().forEach((track) => track.stop())
  };
})();
