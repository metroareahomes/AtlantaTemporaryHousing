"""Settings come from the add-on options file on HA, or from env / .env locally."""
import json
import os
from dataclasses import dataclass
from pathlib import Path

OPTIONS_PATH = Path("/data/options.json")


@dataclass(frozen=True)
class Settings:
    hostaway_account_id: str
    hostaway_api_key: str
    ha_url: str
    ha_token: str
    db_path: str
    is_addon: bool
    timezone: str = "America/New_York"
    sync_minutes: int = 15
    lock_discovery_minutes: int = 60
    lock_check_minutes: int = 30
    default_checkin_hour: int = 15
    default_checkout_hour: int = 10
    extension_ai_api_key: str = ""


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def load_settings() -> Settings:
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if supervisor_token:
        options = json.loads(OPTIONS_PATH.read_text()) if OPTIONS_PATH.exists() else {}
        return Settings(
            hostaway_account_id=str(options.get("hostaway_account_id", "")),
            hostaway_api_key=str(options.get("hostaway_api_key", "")),
            ha_url="http://supervisor/core",
            ha_token=supervisor_token,
            db_path="/data/stay.db",
            is_addon=True,
            extension_ai_api_key=str(options.get("extension_ai_api_key", "")),
        )

    _load_dotenv(Path(".env"))
    ha_token = os.environ.get("HA_TOKEN", "")
    token_file = Path(os.environ.get("HA_TOKEN_FILE", ".ha_token"))
    if not ha_token and token_file.exists():
        ha_token = token_file.read_text(encoding="utf-8").strip()
    return Settings(
        hostaway_account_id=os.environ.get("HOSTAWAY_ACCOUNT_ID", ""),
        hostaway_api_key=os.environ.get("HOSTAWAY_API_KEY", ""),
        ha_url=os.environ.get("HA_URL", "http://homeassistant.local:8123"),
        ha_token=ha_token,
        db_path=os.environ.get("DB_PATH", "data/stay.db"),
        is_addon=False,
        extension_ai_api_key=os.environ.get("EXTENSION_AI_API_KEY", ""),
    )
