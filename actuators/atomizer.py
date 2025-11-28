import importlib.util

if importlib.util.find_spec("RPi.GPIO"):
    import RPi.GPIO as GPIO  # type: ignore
else:
    class _MockGPIO:
        BCM=BOARD=OUT=IN=LOW=HIGH=0
        def setwarnings(self,*a,**k): pass
        def setmode(self,*a,**k): pass
        def setup(self,*a,**k): pass
        def output(self,*a,**k): pass
        def cleanup(self,*a,**k): pass
        def input(self,*a,**k): return 0
    GPIO = _MockGPIO()

class Atomizer:
    def __init__(self, pin: int, active_high: bool = True, initial: bool = False):
        self.pin = pin
        self.active_high = active_high
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        off_level = GPIO.LOW if active_high else GPIO.HIGH
        GPIO.setup(self.pin, GPIO.OUT, initial=off_level)
        self._state = False
        if initial: self.on()

    def _drive(self, on: bool):
        level = GPIO.HIGH if (on == self.active_high) else GPIO.LOW
        GPIO.output(self.pin, level)

    def on(self):
        self._state = True
        self._drive(True)

    def off(self):
        self._state = False
        self._drive(False)

    def set(self, on: bool):
        self.on() if on else self.off()

    @property
    def state(self) -> str:
        return "on" if self._state else "off"

    def cleanup(self):
        try:
            self.off()
            GPIO.cleanup()
        except Exception:
            pass
