Drop a phone-detection model here to switch on detection.

  phone_detector.onnx    what the browser fetches (see app/templates/sit.html)

The server also looks for, in order:
  phone_detector_oiv7.onnx      Open Images V7, class 339 "Mobile phone"  (preferred)
  phone_detector_yolo11n.onnx   COCO, class 67 "cell phone"
  phone_detector.onnx           COCO fallback

The same files the desktop app used will do. Without a model the platform runs normally:
presence and full-screen rules work, snapshots are stored with a verdict of
"not_checked", and both the admin panel and the candidate's status chip say so rather
than implying detection is running.

Models are gitignored because of their size - see docs/DEPLOYMENT.md.
