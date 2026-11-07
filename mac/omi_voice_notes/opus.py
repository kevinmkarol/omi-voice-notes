# ABOUTME: libopus through ctypes (Homebrew's /opt/homebrew/lib/libopus.dylib), decoder for the pendant's frames and an encoder for tests.
# ABOUTME: No Python Opus package: the pendant sends bare Opus packets, not Ogg, and ffmpeg can't take those from a pipe.
from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import os
from pathlib import Path

OPUS_OK = 0
OPUS_APPLICATION_VOIP = 2048
OPUS_APPLICATION_RESTRICTED_LOWDELAY = 2051
OPUS_SET_BITRATE_REQUEST = 4002
OPUS_SET_VBR_REQUEST = 4006
OPUS_SET_COMPLEXITY_REQUEST = 4010
MAX_FRAME_SAMPLES = 5760  # 120 ms at 48 kHz, libopus's documented ceiling

_CANDIDATES = (
    os.environ.get("OMI_NOTES_LIBOPUS"),
    "/opt/homebrew/lib/libopus.dylib",
    "/usr/local/lib/libopus.dylib",
    ctypes.util.find_library("opus"),
)


class OpusError(RuntimeError):
    pass


def _load() -> ctypes.CDLL:
    for candidate in _CANDIDATES:
        if candidate and Path(candidate).exists():
            lib = ctypes.CDLL(candidate)
            break
    else:
        raise OpusError("libopus not found (brew install opus, or set OMI_NOTES_LIBOPUS)")
    lib.opus_decoder_create.restype = ctypes.c_void_p
    lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_decode.restype = ctypes.c_int
    lib.opus_decode.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int,
    ]  # fmt: skip
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encoder_create.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    lib.opus_encode.restype = ctypes.c_int32
    lib.opus_encode.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
        ctypes.c_char_p, ctypes.c_int32,
    ]  # fmt: skip
    # Variadic: declare only the fixed parameters so ctypes passes the value argument the arm64 way
    # (ffi_prep_cif_var); without this the call segfaults on Apple Silicon.
    lib.opus_encoder_ctl.restype = ctypes.c_int
    lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.opus_strerror.restype = ctypes.c_char_p
    lib.opus_strerror.argtypes = [ctypes.c_int]
    lib.opus_get_version_string.restype = ctypes.c_char_p
    return lib


_lib: ctypes.CDLL | None = None


def lib() -> ctypes.CDLL:
    global _lib
    if _lib is None:
        _lib = _load()
    return _lib


def version() -> str:
    return lib().opus_get_version_string().decode()


class Decoder:
    """One Opus packet in, 16-bit PCM out. `decode(None)` asks libopus to conceal one lost frame."""

    def __init__(self, sample_rate: int = 16000, channels: int = 1) -> None:
        self._lib = lib()
        err = ctypes.c_int()
        self._state = self._lib.opus_decoder_create(sample_rate, channels, ctypes.byref(err))
        if err.value != OPUS_OK or not self._state:
            raise OpusError(self._lib.opus_strerror(err.value).decode())
        self.sample_rate, self.channels = sample_rate, channels
        self._pcm = (ctypes.c_int16 * (MAX_FRAME_SAMPLES * channels))()

    def decode(self, packet: bytes | None, frame_samples: int = 320) -> bytes:
        if packet is None:
            n = self._lib.opus_decode(self._state, None, 0, self._pcm, frame_samples, 0)
        else:
            n = self._lib.opus_decode(
                self._state, packet, len(packet), self._pcm, MAX_FRAME_SAMPLES, 0
            )
        if n < 0:
            raise OpusError(f"opus_decode: {self._lib.opus_strerror(n).decode()}")
        return ctypes.string_at(self._pcm, n * self.channels * 2)

    def close(self) -> None:
        if self._state:
            self._lib.opus_decoder_destroy(self._state)
            self._state = None

    def __del__(self) -> None:  # pragma: no cover
        with contextlib.suppress(Exception):
            self.close()


class Encoder:
    """Only for tests and the fake sender: produces frames shaped like the pendant's (16 kHz mono, 20 ms, 32 kbps VBR)."""

    def __init__(self, sample_rate: int = 16000, channels: int = 1, bitrate: int = 32000) -> None:
        self._lib = lib()
        err = ctypes.c_int()
        self._state = self._lib.opus_encoder_create(
            sample_rate, channels, OPUS_APPLICATION_RESTRICTED_LOWDELAY, ctypes.byref(err)
        )
        if err.value != OPUS_OK or not self._state:
            raise OpusError(self._lib.opus_strerror(err.value).decode())
        self._lib.opus_encoder_ctl(self._state, OPUS_SET_BITRATE_REQUEST, ctypes.c_int32(bitrate))
        self._lib.opus_encoder_ctl(self._state, OPUS_SET_VBR_REQUEST, ctypes.c_int32(1))
        self._lib.opus_encoder_ctl(self._state, OPUS_SET_COMPLEXITY_REQUEST, ctypes.c_int32(3))
        self.channels = channels
        self._out = ctypes.create_string_buffer(4000)

    def encode(self, pcm: bytes, frame_samples: int = 320) -> bytes:
        needed = frame_samples * self.channels * 2
        if len(pcm) != needed:
            raise OpusError(f"encode wants {needed} bytes, got {len(pcm)}")
        buf = (ctypes.c_int16 * (frame_samples * self.channels)).from_buffer_copy(pcm)
        n = self._lib.opus_encode(self._state, buf, frame_samples, self._out, len(self._out))
        if n < 0:
            raise OpusError(f"opus_encode: {self._lib.opus_strerror(n).decode()}")
        return self._out.raw[:n]

    def close(self) -> None:
        if self._state:
            self._lib.opus_encoder_destroy(self._state)
            self._state = None
