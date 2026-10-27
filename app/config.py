import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    meta_api_key: str = os.getenv("META_API_KEY", "")
    meta_base_url: str = os.getenv("META_BASE_URL", "https://api.meta.ai/v1")
    meta_model: str = os.getenv("META_MODEL", "muse-spark-1.3")
    meta_model_heavy: str = os.getenv("META_MODEL_HEAVY", "muse-wonder-1.0")   # change planning (L3)
    reasoning_effort: str = os.getenv("META_REASONING_EFFORT", "low")
    database_url: str = os.getenv("DATABASE_URL", "mysql+pymysql://root:@127.0.0.1:3306/muse_servicedesk")
    port: int = int(os.getenv("PORT", "1002"))


settings = Settings()
