"""Writes decoded ATVV PCM to the one user-selected Windows output endpoint.

Windows-only (``sounddevice``/PortAudio). Never touches the system default
device: it always opens the specific endpoint the user picked by name, and
raises immediately if that endpoint can't be opened - callers must treat
that as "voice fails closed, buttons keep working" (see audio_output.py).
"""

from __future__ import annotations

from typing import List

from . import audio_output

SOURCE_SAMPLE_RATE_HZ = 16000
DEFAULT_CHANNELS = 1


class PlaybackUnavailableError(Exception):
    pass


class EndpointPlaybackSink:
    """Opens one output stream bound to a specific, already-resolved endpoint
    and accepts decoded int16 PCM sample batches to play.

    Endpoint identity is (name, host_api) - matching audio_output.py's
    disambiguation contract - since a bare display name is not always unique
    across PortAudio host APIs (e.g. the same physical device can appear
    once under WASAPI and once under MME).
    """

    def __init__(self, endpoint_name: str, host_api: str = "") -> None:
        self._endpoint_name = endpoint_name
        self._host_api = host_api
        self._stream = None
        self._output_sample_rate_hz = SOURCE_SAMPLE_RATE_HZ
        self._output_channels = DEFAULT_CHANNELS
        self._previous_source_sample = 0
        self._last_output_sample = 0
        self._have_previous_sample = False
        self._previous_source_rate_hz = SOURCE_SAMPLE_RATE_HZ
        self._source_frames_consumed = 0
        self._next_output_source_index = 0.0

    def open(self) -> None:
        try:
            import sounddevice as sd  # type: ignore
        except ImportError as exc:  # pragma: no cover - exercised only on Windows
            raise PlaybackUnavailableError(
                "the 'sounddevice' package is not installed"
            ) from exc

        device_index = self._resolve_device_index(sd)
        self._output_channels = self._select_output_channels(sd, device_index)
        self._output_sample_rate_hz = self._select_output_sample_rate(sd, device_index)

        self._stream = sd.OutputStream(
            device=device_index,
            channels=self._output_channels,
            dtype="int16",
            samplerate=self._output_sample_rate_hz,
            latency="low",
        )
        self._stream.start()
        self._previous_source_sample = 0
        self._last_output_sample = 0
        self._have_previous_sample = False
        self._previous_source_rate_hz = SOURCE_SAMPLE_RATE_HZ
        self._source_frames_consumed = 0
        self._next_output_source_index = 0.0

    @property
    def output_sample_rate_hz(self) -> int:
        return self._output_sample_rate_hz

    @property
    def output_channels(self) -> int:
        return self._output_channels

    def _select_output_channels(self, sd, device_index: int) -> int:
        """Use stereo when the endpoint supports it so virtual cables receive both channels."""
        device = sd.query_devices()[device_index]
        return 2 if int(device.get("max_output_channels") or 0) >= 2 else DEFAULT_CHANNELS

    def _select_output_sample_rate(self, sd, device_index: int) -> int:
        device = sd.query_devices()[device_index]
        preferred = int(device.get("default_samplerate") or 0)
        candidates = []
        if preferred > 0:
            candidates.append(preferred)
        candidates.extend([SOURCE_SAMPLE_RATE_HZ, 48000, 44100])

        seen = set()
        errors = []
        for sample_rate in candidates:
            if sample_rate in seen:
                continue
            seen.add(sample_rate)
            try:
                sd.check_output_settings(
                    device=device_index,
                    channels=self._output_channels,
                    dtype="int16",
                    samplerate=sample_rate,
                )
                return sample_rate
            except Exception as exc:  # pragma: no cover - exercised only on Windows
                errors.append(f"{sample_rate} Hz: {exc}")

        detail = "; ".join(errors) if errors else "no candidate sample rates available"
        raise audio_output.AudioOutputUnavailableError(
            "selected output endpoint cannot play mono int16 PCM at any supported "
            f"sample rate ({detail})"
        )

    def _resolve_device_index(self, sd) -> int:
        host_apis = sd.query_hostapis()
        candidates = []
        for index, device in enumerate(sd.query_devices()):
            if device.get("max_output_channels", 0) <= 0:
                continue
            if device["name"] != self._endpoint_name:
                continue
            host_api_name = host_apis[device["hostapi"]]["name"] if host_apis else ""
            candidates.append((index, host_api_name))

        if not candidates:
            raise audio_output.AudioOutputUnavailableError(
                f"selected output endpoint is not currently present: {self._endpoint_name!r}"
            )

        if self._host_api:
            for index, host_api_name in candidates:
                if host_api_name == self._host_api:
                    return index
            raise audio_output.AudioOutputUnavailableError(
                f"selected output endpoint {self._endpoint_name!r} is no longer present "
                f"under host API {self._host_api!r}"
            )

        if len(candidates) > 1:
            raise audio_output.AudioOutputUnavailableError(
                f"{len(candidates)} output endpoints are named {self._endpoint_name!r} "
                "across different host APIs; open settings and re-select one to disambiguate"
            )

        return candidates[0][0]

    def write(self, samples: List[int]) -> None:
        self.write_pcm(samples, SOURCE_SAMPLE_RATE_HZ, DEFAULT_CHANNELS)

    def reset_conversion(self) -> None:
        """Reset interpolation history when the routed microphone changes."""

        self._previous_source_sample = 0
        self._last_output_sample = 0
        self._have_previous_sample = False
        self._source_frames_consumed = 0
        self._next_output_source_index = 0.0

    def write_pcm(
        self,
        samples,
        source_sample_rate_hz: int,
        source_channels: int = DEFAULT_CHANNELS,
    ) -> None:
        """Write int16 PCM from an arbitrary input rate/channel count.

        RC003 remains 16 kHz mono.  Unified-input mode also calls this with
        the selected Windows microphone's native rate (normally 44.1/48 kHz)
        and one or two channels.  Conversion happens exactly once, directly
        into the already-open virtual-cable output format.
        """

        if self._stream is None:
            raise PlaybackUnavailableError("open() must be called before write()")
        if source_sample_rate_hz <= 0:
            raise ValueError("source_sample_rate_hz must be positive")
        if source_channels <= 0:
            raise ValueError("source_channels must be positive")
        import numpy as np  # type: ignore

        array = np.asarray(samples, dtype="int16")
        if array.ndim == 1:
            if source_channels > 1:
                array = array.reshape(-1, source_channels)
            else:
                array = array.reshape(-1, 1)
        elif array.ndim != 2:
            array = array.reshape(-1, source_channels)
        if array.shape[1] > 1:
            # A virtual microphone is mono. Averaging, rather than silently
            # taking channel 0, preserves a stereo microphone transparently.
            mono = np.rint(array.astype("int32").mean(axis=1)).clip(-32768, 32767)
            array = mono.astype("int16").reshape(-1, 1)
        elif array.shape[1] != 1:
            array = array.reshape(-1, 1)

        if self._previous_source_rate_hz != source_sample_rate_hz:
            self.reset_conversion()
        self._previous_source_rate_hz = source_sample_rate_hz

        if (
            source_sample_rate_hz == SOURCE_SAMPLE_RATE_HZ
            and self._output_sample_rate_hz == 48000
            and len(array) > 0
        ):
            # Match the upstream RC003 path: continuous 16 kHz -> 48 kHz
            # interpolation keeps the boundary between BLE notifications smooth.
            values = array[:, 0].astype("int32").tolist()
            previous = (
                self._previous_source_sample
                if self._have_previous_sample
                else values[0]
            )
            output = []
            for current in values:
                delta = current - previous
                output.extend(
                    (
                        previous + round(delta / 3.0),
                        previous + round(delta * (2.0 / 3.0)),
                        current,
                    )
                )
                previous = current
            self._previous_source_sample = values[-1]
            self._have_previous_sample = True
            array = np.asarray(output, dtype="int16").reshape(-1, 1)
        elif self._output_sample_rate_hz != source_sample_rate_hz and len(array) > 0:
            # Streaming linear interpolation with a persistent fractional
            # phase. Per-block round(len * ratio) slowly drifts for 44.1 ->
            # 48 kHz and creates a small discontinuity at every callback.
            # Global source positions plus the prior source sample preserve
            # both the exact long-term rate and the boundary interpolation.
            values = array[:, 0].astype("float64")
            chunk_start = self._source_frames_consumed
            if self._have_previous_sample:
                source_positions = np.arange(
                    chunk_start - 1,
                    chunk_start + len(values),
                    dtype=np.float64,
                )
                source_values = np.concatenate(
                    ([float(self._previous_source_sample)], values)
                )
            else:
                source_positions = np.arange(
                    chunk_start,
                    chunk_start + len(values),
                    dtype=np.float64,
                )
                source_values = values
                self._next_output_source_index = float(chunk_start)
            chunk_end = float(chunk_start + len(values) - 1)
            step = source_sample_rate_hz / self._output_sample_rate_hz
            if self._next_output_source_index <= chunk_end:
                output_length = int(
                    (chunk_end - self._next_output_source_index) / step
                ) + 1
                target_positions = self._next_output_source_index + step * np.arange(
                    output_length, dtype=np.float64
                )
                resampled = np.interp(
                    target_positions, source_positions, source_values
                )
                self._next_output_source_index += step * output_length
            else:
                resampled = np.empty(0, dtype=np.float64)
            self._source_frames_consumed += len(values)
            self._previous_source_sample = int(values[-1])
            self._have_previous_sample = True
            array = np.rint(resampled).clip(-32768, 32767).astype("int16").reshape(-1, 1)
        if len(array) > 0:
            self._last_output_sample = int(array[-1, 0])
            if self._output_sample_rate_hz == source_sample_rate_hz:
                self._previous_source_sample = int(array[-1, 0])
                self._have_previous_sample = True
                self._source_frames_consumed += len(array)
        if self._output_channels > 1:
            array = np.repeat(array, self._output_channels, axis=1)
        self._stream.write(array)

    def write_fade_to_silence(self, duration_ms: int = 8) -> None:
        """Write a very short ramp before a source switch to avoid a click."""

        if self._stream is None or duration_ms <= 0:
            return
        import numpy as np  # type: ignore

        frame_count = max(1, int(self._output_sample_rate_hz * duration_ms / 1000))
        start = self._last_output_sample if self._have_previous_sample else 0
        array = np.linspace(start, 0, frame_count, endpoint=True)
        array = np.rint(array).clip(-32768, 32767).astype("int16").reshape(-1, 1)
        if self._output_channels > 1:
            array = np.repeat(array, self._output_channels, axis=1)
        self._stream.write(array)
        self.reset_conversion()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
