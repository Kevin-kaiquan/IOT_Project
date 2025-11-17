import base64
import importlib
import importlib.util
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

_cv2_spec = importlib.util.find_spec("cv2")
cv2 = importlib.import_module("cv2") if _cv2_spec else None

_np_spec = importlib.util.find_spec("numpy")
np = importlib.import_module("numpy") if _np_spec else None


def _resolve_interpreter():
    for module_name in (
        "tflite_runtime.interpreter",
        "tensorflow.lite.python.interpreter",
        "tensorflow.lite",
    ):
        spec = importlib.util.find_spec(module_name)
        if not spec:
            continue
        module = importlib.import_module(module_name)
        interpreter = getattr(module, "Interpreter", None)
        if interpreter:
            return interpreter
    return None


InterpreterClass = _resolve_interpreter()

log = logging.getLogger("vision")


@dataclass
class DetectionResult:
    ok: bool
    label: str = "--"
    confidence: float = 0.0
    updated_at: float = 0.0
    message: str = ""
    frame_b64: Optional[str] = None
    source: str = ""
    model: str = ""


@dataclass
class DetectorConfig:
    name: str
    camera_index: int
    model_path: str
    labels_path: str
    interval_sec: float = 2.0
    frame_size: Tuple[int, int] = (640, 480)


class CameraDetector:
    """
    Camera + TFLite inference loop.
    Designed for Teachable Machine exported models placed under the webapp's model/ directory.
    """

    def __init__(self, cfg: DetectorConfig):
        self.cfg = cfg
        self._labels: List[str] = []
        self._interpreter = None
        self._input_index: Optional[int] = None
        self._output_index: Optional[int] = None
        self._input_size: Optional[Tuple[int, int]] = None
        self._dtype = None
        self._cap = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._result: DetectionResult = DetectionResult(
            ok=False, message="waiting for runtime", source=cfg.name, model=os.path.basename(cfg.model_path)
        )

    def start(self) -> None:
        if self._cap or self._stop.is_set():
            return
        th = threading.Thread(target=self._loop, name=f"vision-{self.cfg.name}", daemon=True)
        th.start()

    def stop(self) -> None:
        self._stop.set()
        if self._cap:
            try:
                self._cap.release()
            except Exception:
                pass

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return self._result.__dict__.copy()

    # ---------------- internal helpers ----------------
    def _prepare_runtime(self) -> bool:
        if cv2 is None or np is None:
            self._update_result(False, "OpenCV / numpy not installed")
            return False
        if InterpreterClass is None:
            self._update_result(False, "TFLite runtime not available")
            return False
        if not os.path.exists(self.cfg.model_path):
            self._update_result(False, f"model not found: {self.cfg.model_path}")
            return False
        if not self._labels:
            self._labels = self._load_labels(self.cfg.labels_path)
        if self._interpreter is None:
            interpreter = InterpreterClass(model_path=self.cfg.model_path)
            interpreter.allocate_tensors()
            input_details = interpreter.get_input_details()[0]
            output_details = interpreter.get_output_details()[0]
            shape = input_details.get("shape")
            if not shape or len(shape) < 3:
                self._update_result(False, "unexpected model input shape")
                return False
            height, width = int(shape[1]), int(shape[2])
            self._input_size = (width, height)
            self._input_index = input_details.get("index")
            self._output_index = output_details.get("index")
            self._dtype = input_details.get("dtype")
            self._interpreter = interpreter

        if self._cap is None:
            self._cap = cv2.VideoCapture(self.cfg.camera_index)
            if self.cfg.frame_size:
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.frame_size[0])
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.frame_size[1])
        if not self._cap.isOpened():
            self._update_result(False, f"camera {self.cfg.camera_index} not available")
            return False
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self._prepare_runtime():
                time.sleep(self.cfg.interval_sec)
                continue

            ok, frame = self._cap.read()
            if not ok or frame is None:
                self._update_result(False, "frame grab failed")
                time.sleep(self.cfg.interval_sec)
                continue

            label, conf = self._infer(frame)
            frame_b64 = self._encode_frame(frame)
            self._update_result(True, "", label=label, confidence=conf, frame_b64=frame_b64)
            time.sleep(self.cfg.interval_sec)

    def _infer(self, frame) -> Tuple[str, float]:
        if self._interpreter is None or self._input_index is None or self._output_index is None:
            return "--", 0.0
        resized = cv2.resize(frame, self._input_size)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        input_data = np.asarray(rgb)
        input_data = np.expand_dims(input_data, 0)
        if self._dtype and getattr(np, "float32", None) and self._dtype == np.float32:
            input_data = input_data.astype(np.float32) / 255.0
        self._interpreter.set_tensor(self._input_index, input_data)
        self._interpreter.invoke()
        output = self._interpreter.get_tensor(self._output_index)
        scores = output.flatten().tolist()
        if not scores:
            return "--", 0.0
        idx = scores.index(max(scores))
        label = self._labels[idx] if idx < len(self._labels) else f"class {idx}"
        return label, float(scores[idx])

    def _encode_frame(self, frame) -> Optional[str]:
        if cv2 is None:
            return None
        success, buf = cv2.imencode(".jpg", frame)
        if not success:
            return None
        return base64.b64encode(buf).decode("ascii")

    def _load_labels(self, path: str) -> List[str]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
        return []

    def _update_result(
        self,
        ok: bool,
        message: str,
        *,
        label: Optional[str] = None,
        confidence: Optional[float] = None,
        frame_b64: Optional[str] = None,
    ) -> None:
        with self._lock:
            if label is not None:
                self._result.label = label
            if confidence is not None:
                self._result.confidence = confidence
            if frame_b64 is not None:
                self._result.frame_b64 = frame_b64
            self._result.ok = ok
            self._result.message = message
            self._result.updated_at = time.time()
            self._result.source = self.cfg.name
            self._result.model = os.path.basename(self.cfg.model_path)


class DualCameraService:
    def __init__(
        self,
        *,
        model_dir: str,
        target_camera_index: int,
        contamination_camera_index: int,
        target_model_name: str,
        target_label_file: str,
        contamination_model_name: str,
        contamination_label_file: str,
        interval_sec: float = 2.0,
        frame_size: Tuple[int, int] = (640, 480),
    ):
        self.model_dir = model_dir
        self.target = CameraDetector(
            DetectorConfig(
                name="shiitake",
                camera_index=target_camera_index,
                model_path=os.path.join(model_dir, target_model_name),
                labels_path=os.path.join(model_dir, target_label_file),
                interval_sec=interval_sec,
                frame_size=frame_size,
            )
        )
        self.contaminant = CameraDetector(
            DetectorConfig(
                name="contaminant",
                camera_index=contamination_camera_index,
                model_path=os.path.join(model_dir, contamination_model_name),
                labels_path=os.path.join(model_dir, contamination_label_file),
                interval_sec=interval_sec,
                frame_size=frame_size,
            )
        )

    def start(self) -> None:
        self.target.start()
        self.contaminant.start()

    def stop(self) -> None:
        self.target.stop()
        self.contaminant.stop()

    def snapshot(self) -> Dict[str, object]:
        return {
            "shiitake": self.target.snapshot(),
            "contaminant": self.contaminant.snapshot(),
            "model_dir": self.model_dir,
        }
