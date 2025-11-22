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
        width: int = 960,
        height: int = 720,
        probe_limit: int = 6,
    ) -> None:
        """Manage a small set of USB cameras.

        ``device_indices`` are *logical* camera IDs exposed to the API. The manager
        will try to bind each logical ID to the same physical index first, then
        fall back to any available device up to ``probe_limit``.
        """

        self.device_indices: List[int] = sorted(set(int(i) for i in device_indices))
        self.width = width
        self.height = height
        self.probe_limit = max(1, int(probe_limit))
        self._captures: Dict[int, cv2.VideoCapture] = {}
        self._logical_map: Dict[int, int] = {}
        self._errors: Dict[int, str] = {}
        self._lock = threading.Lock()

    # ---- lifecycle helpers ----
    def _open_capture(self, physical_id: int) -> cv2.VideoCapture | None:
        cap = cv2.VideoCapture(physical_id, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(physical_id)

        if not cap.isOpened():
            cap.release()
            return None

        if self.width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, float(self.width))
        if self.height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(self.height))
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            # Some platforms do not support these properties; keep going.
            pass
        return cap

    def _attach_capture(self, cam_id: int) -> cv2.VideoCapture | None:
        """Open a capture for ``cam_id`` with fallbacks.

        We first try to open the same-numbered physical device. If that fails, we
        probe other indices up to ``probe_limit`` and bind the first available to
        this logical ``cam_id``.
        """

        used_physical = set(self._logical_map.values())
        start = self._logical_map.get(cam_id, cam_id)
        candidates: List[int] = [start]
        candidates.extend(i for i in range(self.probe_limit) if i not in candidates)

        for physical_id in candidates:
            if physical_id in used_physical and self._logical_map.get(cam_id) != physical_id:
                continue
            cap = self._open_capture(physical_id)
            if cap is not None:
                self._logical_map[cam_id] = physical_id
                self._captures[cam_id] = cap
                self._errors.pop(cam_id, None)
                return cap

        self._errors[cam_id] = f"Camera {cam_id} could not be opened"
        return None

    def _get_capture(self, cam_id: int) -> cv2.VideoCapture | None:
        cap = self._captures.get(cam_id)
        if cap is None or not cap.isOpened():
            cap = self._attach_capture(cam_id)
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
                cap = self._attach_capture(cam_id)
                if cap is None:
                    raise RuntimeError(f"Camera {cam_id} could not be reopened")
                ok, frame = cap.read()
                if not ok or frame is None:
                    self._errors[cam_id] = "Frame read failed"
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
                result.append(
                    {
                        "id": cam_id,
                        "ready": ready,
                        "error": None if ready else self._errors.get(cam_id),
                        "mapped_to": self._logical_map.get(cam_id),
                    }
                )
        return result

    def cleanup(self) -> None:
        with self._lock:
            for cap in self._captures.values():
                try:
                    cap.release()
                except Exception:
                    pass
            self._captures.clear()
