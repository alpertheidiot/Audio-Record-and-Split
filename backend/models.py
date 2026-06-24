from pydantic import BaseModel, Field, field_validator
from typing import Union, List, Optional

class AppConfig(BaseModel):
    input_device: Union[str, int] = Field(default="BlackHole 2ch", description="Device name or PortAudio index")
    sample_rate: int = Field(default=48000, description="Sample rate (44100, 48000, 96000)")
    bit_depth: int = Field(default=24, description="Bit depth for WAV master (always 24)")
    channels: int = Field(default=2, description="Number of audio channels (1 or 2)")
    
    start_threshold_db: float = Field(default=-45.0, description="Start gate threshold in dBFS")
    stop_threshold_db: float = Field(default=-50.0, description="Stop gate threshold in dBFS")
    silence_stop_ms: int = Field(default=2000, description="Silence duration in milliseconds before stopping")
    start_debounce_ms: int = Field(default=150, description="Debounce time in milliseconds above start threshold")
    preroll_ms: int = Field(default=300, description="Duration in milliseconds to prepend from pre-roll buffer")
    tail_keep_ms: int = Field(default=100, description="Duration in milliseconds of trailing silence to retain")
    min_recording_ms: int = Field(default=1000, description="Discard recordings shorter than this")
    max_recording_ms: int = Field(default=900000, description="Force finalise recordings reaching this duration (15 min)")
    
    output_format: str = Field(default="mp3", description="Output file format (mp3, flac, aac, wav)")
    output_bitrate: str = Field(default="320k", description="Bitrate for mp3/aac (e.g. 192k, 256k, 320k)")
    aac_vbr: bool = Field(default=True, description="Enable Variable Bitrate (VBR) for AAC instead of CBR")
    keep_wav_master: bool = Field(default=True, description="Keep original lossless WAV master")
    output_dir: str = Field(default="./recordings", description="Output folder for files")
    filename_pattern: str = Field(default="{ts}", description="Filename template: {ts} (timestamp), {n} (index)")

    @field_validator("sample_rate")
    @classmethod
    def validate_sample_rate(cls, v: int) -> int:
        if v not in [44100, 48000, 96000]:
            raise ValueError("Sample rate must be 44100, 48000, or 96000")
        return v

    @field_validator("bit_depth")
    @classmethod
    def validate_bit_depth(cls, v: int) -> int:
        # Always coerce to 24-bit to prevent validation errors with legacy configs
        return 24

    @field_validator("channels")
    @classmethod
    def validate_channels(cls, v: int) -> int:
        if v not in [1, 2]:
            raise ValueError("Channels must be 1 (mono) or 2 (stereo)")
        return v

    @field_validator("start_threshold_db", "stop_threshold_db")
    @classmethod
    def validate_db(cls, v: float) -> float:
        if not (-90.0 <= v <= 0.0):
            raise ValueError("Threshold dBFS must be between -90.0 and 0.0")
        return v

    @field_validator("output_format")
    @classmethod
    def validate_format(cls, v: str) -> str:
        fmt = v.lower().strip()
        if fmt not in ["mp3", "flac", "aac", "wav"]:
            raise ValueError("Output format must be one of: mp3, flac, aac, wav")
        return fmt

    @field_validator("silence_stop_ms", "start_debounce_ms", "preroll_ms", "tail_keep_ms", "min_recording_ms", "max_recording_ms")
    @classmethod
    def validate_ms(cls, v: int) -> int:
        if v < 0:
            raise ValueError("Duration values must be positive integers")
        return v

class DeviceInfo(BaseModel):
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: float
    is_default: bool = False

class RecordingInfo(BaseModel):
    name: str
    path: str
    duration_ms: int
    size_bytes: int
    created_at: str
    format: str
    peak_db: float
    bitrate_kbps: int

class ConvertRequest(BaseModel):
    target_format: str
    bitrate: Optional[str] = "320k"
    aac_vbr: Optional[bool] = True
