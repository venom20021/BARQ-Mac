"""
Audio device resolution for BARQ voice system.
Handles auto-detection of the best physical microphone and speaker,
and lets users override via AUDIO_INPUT_DEVICE / AUDIO_OUTPUT_DEVICE config.
"""

from typing import Optional

# Cache the resolved device indices so we only query once
_resolved_input: Optional[int] = None
_resolved_output: Optional[int] = None


def resolve_input_device(config_device: str) -> Optional[int]:
    """Resolve the input audio device index from config.

    Args:
        config_device: The AUDIO_INPUT_DEVICE config value.
            - "" or None → None (use PortAudio default)
            - "auto" → auto-detect best physical mic
            - integer string → use that device index directly
            - name substring → find first device whose name contains the string

    Returns:
        Device index to pass to sounddevice, or None for system default.
    """
    global _resolved_input
    if _resolved_input is not None:
        return _resolved_input

    if not config_device or config_device.strip() == "":
        _resolved_input = None
        return None

    config_device = config_device.strip()

    # Direct integer index
    if config_device.isdigit():
        idx = int(config_device)
        _resolved_input = idx
        return idx

    # Auto-detect: find the best physical microphone
    if config_device.lower() == "auto":
        auto_idx = _auto_detect_input()
        _resolved_input = auto_idx
        return auto_idx

    # Try to match by name substring
    try:
        import sounddevice as sd
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] > 0 and config_device.lower() in dev["name"].lower():
                _resolved_input = i
                return i
    except Exception:
        pass

    # Fallback to default
    _resolved_input = None
    return None


def resolve_output_device(config_device: str) -> Optional[int]:
    """Resolve the output audio device index from config.

    Args:
        config_device: The AUDIO_OUTPUT_DEVICE config value.

    Returns:
        Device index or None for system default.
    """
    global _resolved_output
    if _resolved_output is not None:
        return _resolved_output

    if not config_device or config_device.strip() == "":
        _resolved_output = None
        return None

    config_device = config_device.strip()

    if config_device.isdigit():
        idx = int(config_device)
        _resolved_output = idx
        return idx

    if config_device.lower() == "auto":
        auto_idx = _auto_detect_output()
        _resolved_output = auto_idx
        return auto_idx

    try:
        import sounddevice as sd
        for i, dev in enumerate(sd.query_devices()):
            if dev["max_output_channels"] > 0 and config_device.lower() in dev["name"].lower():
                _resolved_output = i
                return i
    except Exception:
        pass

    _resolved_output = None
    return None


def get_device_list():
    """Return a list of available audio devices with metadata."""
    try:
        import sounddevice as sd
        devices = []

        # Get default device indices using kind= query (returns single Device or None)
        try:
            default_in_dev = sd.query_devices(kind="input")
            default_out_dev = sd.query_devices(kind="output")
            default_in_idx = int(default_in_dev["index"]) if default_in_dev is not None else -1
            default_out_idx = int(default_out_dev["index"]) if default_out_dev is not None else -1
        except Exception:
            default_in_idx = -1
            default_out_idx = -1

        # Cache host APIs for resolving hostapi indices to names
        hostapis = sd.query_hostapis()

        for i, dev in enumerate(sd.query_devices()):
            # sounddevice returns dicts on this platform
            name = str(dev.get("name", "Unknown")).strip()
            ch_in = int(dev.get("max_input_channels", 0))
            ch_out = int(dev.get("max_output_channels", 0))
            samplerate = float(dev.get("default_samplerate", 44100))
            host_api_idx = int(dev.get("hostapi", 0))
            hostapi_name = str(hostapis[host_api_idx].get("name", "Unknown")) if host_api_idx < len(hostapis) else "Unknown"

            devices.append({
                "index": i,
                "name": name,
                "channels_in": ch_in,
                "channels_out": ch_out,
                "default_samplerate": samplerate,
                "host_api": hostapi_name,
                "is_default_input": i == default_in_idx,
                "is_default_output": i == default_out_idx,
            })
        return devices
    except Exception as e:
        return {"error": str(e)}


def _auto_detect_input() -> Optional[int]:
    """Auto-detect the best physical microphone.

    Priority:
    1. WDM-KS Realtek Microphone Array (direct KS access — lowest latency)
    2. MME Microphone Array (most compatible)
    3. Any device called "Microphone" or "Mic"
    4. Fallback to system default
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()

        # Score each input device and pick the best
        scored: list[tuple[int, int]] = []  # (index, score)
        for i, dev in enumerate(devices):
            if dev["max_input_channels"] == 0:
                continue
            name = dev["name"].lower()
            score = 0

            # Prefer physical mics over virtual/streaming devices
            if "streaming" in name or "steam" in name:
                score -= 50
            if "virtual" in name or "vac" in name or "line" in name:
                score -= 40

            # Prefer Realtek (physical onboard audio)
            if "realtek" in name:
                score += 30
            if "microphone array" in name:
                score += 20

            # Prefer WDM-KS (lowest latency) over MME
            hostapi = sd.query_hostapis()[dev["host_api"]]
            if "ks" in hostapi["name"].lower() or "wdm" in hostapi["name"].lower():
                score += 15
            elif "wasapi" in hostapi["name"].lower():
                score += 10
            elif "mme" in hostapi["name"].lower():
                score += 5

            # Prefer devices with "microphone" or "mic" in name
            if "microphone" in name or "mic" in name:
                score += 10

            scored.append((i, score))

        if not scored:
            return None

        # Pick the highest-scored device
        scored.sort(key=lambda x: x[1], reverse=True)
        best_idx, best_score = scored[0]

        if best_score > 0:
            print(f"[AudioDevice] Auto-selected input device [{best_idx}]: {devices[best_idx]['name'].strip()} (score={best_score})")
            return best_idx

        return None
    except Exception:
        return None


def _auto_detect_output() -> Optional[int]:
    """Auto-detect the best speaker output.

    Priority:
    1. Realtek Speakers
    2. Any speaker/exclude virtual/streaming
    3. Fallback to default
    """
    try:
        import sounddevice as sd
        devices = sd.query_devices()

        scored: list[tuple[int, int]] = []
        for i, dev in enumerate(devices):
            if dev["max_output_channels"] == 0:
                continue
            name = dev["name"].lower()
            score = 0

            if "streaming" in name or "steam" in name:
                score -= 50
            if "virtual" in name or "vac" in name or "line" in name:
                score -= 30

            if "realtek" in name:
                score += 30
            if "speaker" in name:
                score += 20
            if "headphone" in name:
                score += 15

            hostapi = sd.query_hostapis()[dev["host_api"]]
            if "ks" in hostapi["name"].lower() or "wdm" in hostapi["name"].lower():
                score += 10
            elif "wasapi" in hostapi["name"].lower():
                score += 5

            scored.append((i, score))

        if not scored:
            return None

        scored.sort(key=lambda x: x[1], reverse=True)
        best_idx, best_score = scored[0]

        if best_score > 0:
            print(f"[AudioDevice] Auto-selected output device [{best_idx}]: {devices[best_idx]['name'].strip()} (score={best_score})")
            return best_idx

        return None
    except Exception:
        return None


def reset_cache():
    """Reset resolved device cache (useful when audio hardware changes)."""
    global _resolved_input, _resolved_output
    _resolved_input = None
    _resolved_output = None
