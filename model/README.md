# Model directory

Place the Teachable Machine exports for both cameras here. The defaults are:

- `shiitake_detector.tflite` with label file `shiitake_labels.txt` for the香菇识别镜头。
- `contaminant_detector.tflite` with label file `contaminant_labels.txt` for污染/杂物镜头。

If you name the files differently, update `config.py` (or environment variables) so the Flask app picks them up.
