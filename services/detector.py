"""Local Teachable Machine model wrapper using TFLite."""
from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

import cv2  # type: ignore
import numpy as np

try:  # pragma: no cover - runtime import guard
    from tflite_runtime.interpreter import Interpreter  # type: ignore
except Exception:  # pragma: no cover - runtime import guard
    try:
        from tensorflow.lite import Interpreter  # type: ignore
    except Exception:  # pragma: no cover - runtime import guard
        Interpreter = None


log = logging.getLogger(__name__)


class TeachableMachineDetector:
    """Lightweight detector for Teachable Machine image models."""

    def __init__(
        self,
        model_dir: str = "model",
        *,
        model_filename: Optional[str] = None,
        labels_filename: Optional[str] = None,
    ) -> None:
        self.model_dir = model_dir
        self.model_path = self._resolve_model_path(model_filename)
        self.labels_path = self._resolve_labels_path(labels_filename)
        self.labels: List[str] = self._load_labels()
        self.interpreter = self._load_interpreter()
        self.input_details = self.interpreter.get_input_details() if self.interpreter else []
        self.output_details = self.interpreter.get_output_details() if self.interpreter else []
        self.height, self.width = self._infer_input_size()

    def _resolve_model_path(self, preferred: Optional[str]) -> str:
        """Locate a TFLite model inside the model directory."""
        candidates: List[str] = []
        if preferred:
            candidates.append(os.path.join(self.model_dir, preferred))
        candidates.append(os.path.join(self.model_dir, "model.tflite"))

        if os.path.isdir(self.model_dir):
            for name in sorted(os.listdir(self.model_dir)):
                if name.lower().endswith(".tflite"):
                    candidates.append(os.path.join(self.model_dir, name))

        for path in candidates:
            if os.path.exists(path):
                return path

        # Fall back to the first candidate; loading will warn later.
        return candidates[0]

    def _resolve_labels_path(self, preferred: Optional[str]) -> str:
        """Locate a labels file inside the model directory."""
        candidates: List[str] = []
        if preferred:
            candidates.append(os.path.join(self.model_dir, preferred))
        candidates.append(os.path.join(self.model_dir, "labels.txt"))

        if os.path.isdir(self.model_dir):
            for name in sorted(os.listdir(self.model_dir)):
                lower = name.lower()
                if lower.endswith(".txt") and "label" in lower:
                    candidates.append(os.path.join(self.model_dir, name))

        for path in candidates:
            if os.path.exists(path):
                return path

        return candidates[0]

    def _load_labels(self) -> List[str]:
        try:
            with open(self.labels_path, "r", encoding="utf-8") as f:
                labels: List[str] = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # Support formats like "0 class" or raw label text.
                    parts = line.split(maxsplit=1)
                    labels.append(parts[-1])
                return labels
        except FileNotFoundError:
            log.warning("labels.txt not found in %s", os.path.dirname(self.labels_path))
            return []
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Failed to load labels: %s", exc)
            return []

    def _load_interpreter(self):
        if Interpreter is None:
            log.warning("TFLite interpreter unavailable; install tflite-runtime or TensorFlow")
            return None
        if not os.path.exists(self.model_path):
            log.warning("Model file missing: %s", self.model_path)
            return None

        try:
            interpreter = Interpreter(model_path=self.model_path)
            interpreter.allocate_tensors()
            return interpreter
        except Exception as exc:  # pragma: no cover - runtime safety
            log.warning("Failed to initialize TFLite model: %s", exc)
            return None

    def _infer_input_size(self) -> tuple[int, int]:
        if not self.input_details:
            return 224, 224
        shape = self.input_details[0].get("shape")
        if shape is None or len(shape) < 3:
            return 224, 224
        return int(shape[1]), int(shape[2])

    def _prepare_input(self, frame: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        resized = cv2.resize(rgb, (self.width, self.height))
        input_data = np.expand_dims(resized, axis=0)
        target_dtype = self.input_details[0].get("dtype") if self.input_details else np.float32

        if target_dtype == np.float32:
            input_data = input_data.astype(np.float32) / 255.0
        else:
            input_data = input_data.astype(target_dtype)
        return input_data

    def detect(self, jpeg_bytes: bytes) -> Dict:
        """Run inference on JPEG bytes and return sorted predictions."""
        if not self.interpreter or not self.input_details or not self.output_details:
            raise RuntimeError("TFLite model not initialized")

        np_buffer = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        frame = cv2.imdecode(np_buffer, cv2.IMREAD_COLOR)
        if frame is None:
            raise RuntimeError("Failed to decode camera frame")

        input_data = self._prepare_input(frame)
        self.interpreter.set_tensor(self.input_details[0]["index"], input_data)
        self.interpreter.invoke()

        output = self.interpreter.get_tensor(self.output_details[0]["index"])[0]
        if isinstance(output, np.ndarray) and output.dtype != np.float32:
            scale, zero = self.output_details[0].get("quantization", (1.0, 0))
            output = scale * (output.astype(np.float32) - zero)

        scores = output.tolist()
        predictions = []
        for idx, score in enumerate(scores):
            label = self.labels[idx] if idx < len(self.labels) else f"class_{idx}"
            predictions.append({"class": label, "confidence": float(score)})

        predictions.sort(key=lambda x: x["confidence"], reverse=True)
        best = predictions[0] if predictions else {"class": "unknown", "confidence": 0.0}
        return {
            "label": best.get("class"),
            "probability": float(best.get("confidence", 0.0)),
            "predictions": predictions,
        }
