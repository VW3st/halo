"""Verify listen_for_wake(interrupt=evt) returns the -1.0 sentinel promptly when
the interrupt Event is set (the dictation-hotkey hand-off). Stubs the audio stack
so no real mic/model is needed."""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m


class _NoStream:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeModel:
    def reset(self):
        pass

    def predict(self, chunk):
        return {}


_stub("sounddevice", InputStream=_NoStream, query_devices=lambda *a, **k: {"name": "fake-mic"})
_stub("openwakeword", utils=types.SimpleNamespace(download_models=lambda *a, **k: None))
_stub("openwakeword.model", Model=_FakeModel)
sys.modules["openwakeword"].model = sys.modules["openwakeword.model"]
_stub("silero_vad", VADIterator=object, load_silero_vad=lambda *a, **k: None)
_stub("torch")

import halo.wake as wake  # noqa: E402

wake._get_model = lambda: _FakeModel()
if not getattr(wake, "_active_wake_key", None):
    wake._active_wake_key = "halo"


def main() -> int:
    f = 0
    evt = threading.Event()
    evt.set()  # pre-set: should bail on the first poll
    t0 = time.time()
    r = wake.listen_for_wake(interrupt=evt)
    dt = time.time() - t0

    ok1 = r == -1.0
    ok2 = dt < 3.0
    f += 0 if ok1 else 1
    f += 0 if ok2 else 1
    print(f"  [{'OK' if ok1 else 'FAIL'}] returns -1.0 sentinel on pre-set interrupt -> {r}")
    print(f"  [{'OK' if ok2 else 'FAIL'}] returns promptly ({dt:.2f}s < 3s)")
    print(f"\n{2 - f}/2 passed")
    return 1 if f else 0


if __name__ == "__main__":
    raise SystemExit(main())
