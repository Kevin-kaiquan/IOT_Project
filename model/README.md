# Local vision model

Export an image model from Teachable Machine in TensorFlow Lite format and
place the runtime files here:

```text
model/
├── model.tflite
└── labels.txt
```

The loader also accepts another `.tflite` filename and another text filename
containing `label`, but the names above are recommended.

Labels may be written as plain text or with a numeric prefix:

```text
0 base
1 shiitake
2 mold
3 fly agaric
```

Model binaries and local labels are ignored by Git because trained models may
be large or project-specific. This directory is for the image-classification
model only; the enclosure's 3D model is not included. Contact
[Kevin-kaiquan](https://github.com/Kevin-kaiquan) if you need the corresponding
3D files.

