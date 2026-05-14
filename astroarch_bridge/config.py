import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_IMAGES_DIR = Path.home() / "Pictures" / "Ekos"
DEFAULT_TOKEN_FILE = Path.home() / ".config" / "astroarch-bridge" / "token"


class Settings(BaseSettings):
    """Settings - all of them can be overridden via env vars ASTROARCH_*."""

    model_config = SettingsConfigDict(
        env_prefix="ASTROARCH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- HTTP server ---
    host: str = Field(default="0.0.0.0", description="Bind address (0.0.0.0 for Tailscale)")
    port: int = Field(default=8765, ge=1, le=65535)
    log_level: str = Field(default="INFO")

    # --- Auth ---
    token: str = Field(default="", description="Bearer token. If empty, read from token_file.")
    token_file: Path = Field(default=DEFAULT_TOKEN_FILE)
    auto_generate_token: bool = Field(default=True, description="If true, generates a random token on the first start")

    # --- INDI server ---
    indi_host: str = Field(default="127.0.0.1")
    indi_port: int = Field(default=7624, ge=1, le=65535)
    indi_reconnect_min: float = Field(default=1.0, gt=0)
    indi_reconnect_max: float = Field(default=30.0, gt=0)

    # --- PHD2 ---
    phd2_host: str = Field(default="127.0.0.1")
    phd2_port: int = Field(default=4400, ge=1, le=65535)
    phd2_enabled: bool = Field(default=True)
    phd2_reconnect_min: float = Field(default=2.0, gt=0)
    phd2_reconnect_max: float = Field(default=60.0, gt=0)

    # --- Images ---
    images_dir: Path = Field(default=DEFAULT_IMAGES_DIR)
    image_jpeg_quality: int = Field(default=85, ge=10, le=100)
    image_max_dim: int = Field(default=1600, ge=200, le=8192,
                               description="Biggest axis (X or Y) of the image, in pixels")
    image_stabilize_ms: int = Field(default=500, ge=100, le=5000,
                                    description="waiting (in ms) before reading the FITS.")
    image_thumbnail_dim: int = Field(default=512, ge=64, le=2048)

    # --- WS ---
    ws_state_max_clients: int = Field(default=8, ge=1, le=64)
    ws_frames_max_clients: int = Field(default=4, ge=1, le=32)
    ws_send_queue: int = Field(default=1024, ge=4, le=8192,
                               description="Backpressure: drop oldest message if queue is full"
                                           "Must be >= 2x the number of INDI properties")

    # --- Misc ---
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v = v.upper()
        if v not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return v

    @field_validator("indi_reconnect_max")
    @classmethod
    def _check_max_gt_min(cls, v: float, info) -> float:
        mn = info.data.get("indi_reconnect_min")
        if mn is not None and v < mn:
            raise ValueError("indi_reconnect_max must be >= indi_reconnect_min")
        return v

    def resolve_token(self) -> str:
        """Legge token da config o file; se non esiste lo genera (auto_generate_token=True)."""
        if self.token:
            return self.token
        if self.token_file.exists():
            tok = self.token_file.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        if not self.auto_generate_token:
            raise RuntimeError(
                f"No token in env ASTROARCH_TOKEN nor in {self.token_file}. "
                "Either set ASTROARCH_TOKEN, write a token file, or enable auto_generate_token."
            )
        # Genera token random e salva
        new_tok = secrets.token_urlsafe(32)
        self.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token_file.write_text(new_tok + "\n", encoding="utf-8")
        try:
            os.chmod(self.token_file, 0o600)
        except OSError:
            pass
        return new_tok

    def ensure_paths(self) -> None:
        """Make sure needed folders are created. Idempotent."""
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton settings. Side-effect: ensure paths and tokens on boot."""
    s = Settings()
    s.ensure_paths()
    # Ensure token is fetched on start
    _ = s.resolve_token()
    return s


def reset_settings_cache() -> None:
    """Test only function. Resets the singleton"""
    get_settings.cache_clear()
