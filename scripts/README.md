# Hardware self-tests

Run these scripts directly on the Raspberry Pi after wiring the corresponding
hardware:

```bash
python scripts/relay_selftest.py
python scripts/temperature_selftest.py
python scripts/oled_selftest.py
```

The relay test changes GPIO output states. Disconnect loads that could be
damaged by unexpected switching before running it.
