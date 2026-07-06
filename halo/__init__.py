"""Halo — voice frontend for agentic coding tools."""

__version__ = "1.7.0"


def _guard_against_wedged_wmi() -> None:
    """Stop a wedged Windows WMI service from hanging Halo at startup.

    Several libraries (torch, kokoro_onnx, ...) call platform.machine() /
    platform.platform() / platform.processor() at IMPORT time. On Windows
    (Python 3.12+) those resolve through ``platform._wmi_query`` — a WMI call. If
    the Winmgmt service is wedged (it can hang while still reporting "Running"),
    the query never returns, the import blocks forever, and Halo freezes right
    after the config banner — un-interruptible, because it's stuck in a native
    call.

    We neutralize ``platform._wmi_query`` so every WMI lookup raises immediately
    and falls back to fast, env-var-based values (PROCESSOR_ARCHITECTURE,
    sys.getwindowsversion(), PROCESSOR_IDENTIFIER) — the same path Python takes
    when WMI is simply unavailable, and verified to return correct values. Runs
    once on ``import halo``, before any submodule pulls in torch/kokoro.
    Windows-only, best-effort, never raises.
    """
    import sys

    if sys.platform != "win32":
        return
    try:
        import platform

        if hasattr(platform, "_wmi_query"):
            def _no_wmi(*args, **kwargs):
                raise OSError(
                    "WMI disabled by Halo (avoids hang on a wedged Winmgmt service)"
                )

            platform._wmi_query = _no_wmi
    except Exception:
        pass


_guard_against_wedged_wmi()
