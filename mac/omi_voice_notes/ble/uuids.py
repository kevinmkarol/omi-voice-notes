# ABOUTME: Every UUID and constant the Omi CV1 firmware exposes over BLE, taken from the firmware source (transport.c, storage.c, button.c).
# ABOUTME: Keep this the single place such numbers live; docs/PROTOCOL.md is the human-readable copy.

DEVICE_NAME = "Omi"

AUDIO_SERVICE = "19b10000-e8f2-537e-4f6c-d104768a1214"
AUDIO_DATA = "19b10001-e8f2-537e-4f6c-d104768a1214"
AUDIO_CODEC = "19b10002-e8f2-537e-4f6c-d104768a1214"
AUDIO_SPEAKER = "19b10003-e8f2-537e-4f6c-d104768a1214"  # absent on CV1

SETTINGS_SERVICE = "19b10010-e8f2-537e-4f6c-d104768a1214"
SETTINGS_DIM_RATIO = "19b10011-e8f2-537e-4f6c-d104768a1214"
SETTINGS_MIC_GAIN = "19b10012-e8f2-537e-4f6c-d104768a1214"
SETTINGS_CHARGING = "19b10013-e8f2-537e-4f6c-d104768a1214"

FEATURES_SERVICE = "19b10020-e8f2-537e-4f6c-d104768a1214"
FEATURES = "19b10021-e8f2-537e-4f6c-d104768a1214"

TIME_SYNC_SERVICE = "19b10030-e8f2-537e-4f6c-d104768a1214"
TIME_WRITE = "19b10031-e8f2-537e-4f6c-d104768a1214"  # u32 LE epoch seconds
TIME_READ = "19b10032-e8f2-537e-4f6c-d104768a1214"

STORAGE_SERVICE = "30295780-4301-eabd-2904-2849adfeae43"
STORAGE_CONTROL = "30295781-4301-eabd-2904-2849adfeae43"  # write commands, receive notifications
STORAGE_STATUS = "30295782-4301-eabd-2904-2849adfeae43"  # 16 bytes u32 LE x4

BUTTON_SERVICE = "23ba7924-0000-1000-7450-346eac492e92"
BUTTON_TRIGGER = "23ba7925-0000-1000-7450-346eac492e92"

BATTERY_SERVICE = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

DIS_SERVICE = "0000180a-0000-1000-8000-00805f9b34fb"
DIS_MANUFACTURER = "00002a29-0000-1000-8000-00805f9b34fb"
DIS_MODEL = "00002a24-0000-1000-8000-00805f9b34fb"
DIS_HARDWARE_REV = "00002a27-0000-1000-8000-00805f9b34fb"
DIS_FIRMWARE_REV = "00002a26-0000-1000-8000-00805f9b34fb"

# Codec ids on AUDIO_CODEC
CODEC_PCM16_16K = 0
CODEC_PCM16_8K = 1
CODEC_MULAW_16K = 10
CODEC_MULAW_8K = 11
CODEC_OPUS_16K = 20  # Opus, 160-sample (10 ms) frames
CODEC_OPUS_16K_FS320 = 21  # Opus, 320-sample (20 ms) frames: what CV1 firmware 3.0.21 reports
OPUS_CODECS = {CODEC_OPUS_16K, CODEC_OPUS_16K_FS320}
CODEC_NAMES = {
    0: "pcm16/16k",
    1: "pcm16/8k",
    10: "mulaw/16k",
    11: "mulaw/8k",
    20: "opus/16k/10ms",
    21: "opus/16k/20ms",
}

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_SAMPLES = 320  # one Opus frame = 20 ms at 16 kHz

# Button events (first int32 LE of the 8-byte notification)
BUTTON_SINGLE_TAP = 1
BUTTON_DOUBLE_TAP = 2
BUTTON_LONG_TAP = 3
BUTTON_DOWN = 4
BUTTON_UP = 5
# 6 and 7 exist only on the tap-to-record firmware (firmware/ in this repo): sent after
# every single tap, they say whether the pendant is now recording or has muted its microphone.
BUTTON_RECORDING_ON = 6
BUTTON_MUTED = 7
BUTTON_NAMES = {
    1: "single_tap",
    2: "double_tap",
    3: "long_tap",
    4: "down",
    5: "up",
    6: "recording_on",
    7: "muted",
}

# The ring-buffer storage protocol shipped with firmware 3.0.20.
MIN_RING_FIRMWARE = (3, 0, 20)


def parse_firmware(text: str | None) -> tuple[int, int, int] | None:
    """'3.0.21' -> (3, 0, 21); None for anything unparsable."""
    if not text:
        return None
    parts = text.strip().split(".")
    if len(parts) < 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2].split("+")[0].split("-")[0])
    except ValueError:
        return None


def has_ring_storage(firmware: str | None) -> bool:
    version = parse_firmware(firmware)
    return version is not None and version >= MIN_RING_FIRMWARE
