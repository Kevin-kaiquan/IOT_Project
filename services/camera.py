"""Lightweight multi-camera helper built on OpenCV.

This module keeps a tiny cache of ``cv2.VideoCapture`` objects so we don't
re-open the device on every HTTP request. It exposes a simple ``get_frame``
method that returns a JPEG-encoded snapshot for the requested camera index.

The helper is intentionally defensive:
- invalid camera IDs raise ``KeyError``
- failed reads trigger a single re-open attempt
- ``cleanup()`` releases all cached captures so Flask shutdowns don't leave
  devices locked
"""
from __future__ import annotations

import threading
from typing import Dict, Iterable, List

import cv2  # type: ignore


class CameraManager:
    def __init__(
        self,
        device_indices: Iterable[int],
        *,
        width: int = 640,
        height: int = 480,
    ) -> None:
        self.device_indices: List[int] = sorted(set(int(i) for i in device_indices))
        self.width = width
        self.height = height
        self._captures: Dict[int, cv2.VideoCapture] = {}
        self._lock = threading.Lock()

    # ---- lifecycle helpers ----
    def _open_capture(self, cam_id: int) -> cv2.VideoCapture | None:
        cap = cv2.VideoCapture(cam_id)
        if not cap.isOpened():
            cap.release()
            return None

        if self.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
        if self.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
        return cap

    def _get_capture(self, cam_id: int) -> cv2.VideoCapture | None:
        cap = self._captures.get(cam_id)
        if cap is None or not cap.isOpened():
            cap = self._open_capture(cam_id)
            if cap is not None:
                self._captures[cam_id] = cap
        return cap

    # ---- public API ----
    def get_frame(self, cam_id: int) -> bytes:
        if cam_id not in self.device_indices:
            raise KeyError(f"Camera {cam_id} not allowed")

        with self._lock:
            cap = self._get_capture(cam_id)
            if cap is None:
                raise RuntimeError(f"Camera {cam_id} not available")

            ok, frame = cap.read()
            if not ok or frame is None:
                # try once more after reopening
                cap.release()
                self._captures.pop(cam_id, None)
                cap = self._get_capture(cam_id)
                if cap is None:
                    raise RuntimeError(f"Camera {cam_id} could not be reopened")
                ok, frame = cap.read()
                if not ok or frame is None:
                    raise RuntimeError(f"Camera {cam_id} read failed")

        success, buf = cv2.imencode(".jpg", frame)
        if not success:
            raise RuntimeError("Failed to encode frame")
        return buf.tobytes()

    def status(self) -> List[dict]:
        # check availability without locking the devices for long
        result: List[dict] = []
        with self._lock:
            for cam_id in self.device_indices:
                cap = self._get_capture(cam_id)
                ready = bool(cap and cap.isOpened())
                result.append({"id": cam_id, "ready": ready})
        return result

    def cleanup(self) -> None:
        with self._lock:
            for cap in self._captures.values():
                try:
                    cap.release()
                except Exception:
                    pass
            self._captures.clear()
