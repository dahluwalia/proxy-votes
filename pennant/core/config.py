"""Settings (from `.env`) and the voting policy (from `config/policy.yaml`).

Design choice: no dotenv dependency and no global state. `load_settings()` reads
`.env` with a ten-line parser (real environment variables win), and
`load_policy()` coerces YAML booleans to Tri so `yes` and `"yes"` both work.
API keys are hidden from repr so they never land in logs or test output.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pennant.core.tri import TriField

ROOT = Path(__file__).resolve().parents[2]
NOTICES_DIR = ROOT / "data" / "notices"
POLICY_PATH = ROOT / "config" / "policy.yaml"
FIXTURES_DIR = ROOT / "tests" / "fixtures"

REQUIRED_KEYS = ("CH_API_KEY", "LLAMA_CLOUD_API_KEY", "ANTHROPIC_API_KEY")
# '<Company>_<year>_Notice_of_AGM.pdf': the company part is everything before the year.
_YEAR = re.compile(r"[\s_.-]+(?:19|20)\d{2}(?=[\s_.-]|$)")


def notice_label(pdf_name: str | Path) -> str:
    """Company part of a notice filename: 'Severn_Trent_2026_Notice_of_AGM.pdf' ->
    'Severn Trent'. Any notice works; a name without a year falls back to its stem."""
    stem = Path(pdf_name).stem
    base = _YEAR.split(stem, maxsplit=1)[0] or stem
    return " ".join(base.replace("_", " ").split())


def slug(text: str) -> str:
    """'Marks & Spencer' -> 'marks-and-spencer'."""
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower().replace("&", " and ")).strip("-")


def meeting_key_for(pdf_name: str | Path) -> str:
    """Meeting key for a notice (used by `pennant votes --meeting`), derived from its
    filename: 'Marks_and_Spencer_2026_Notice_of_AGM.pdf' -> 'marks-and-spencer'."""
    return slug(notice_label(pdf_name))


def notices(notices_dir: Path = NOTICES_DIR) -> dict[str, str]:
    """Meeting key -> PDF filename for every PDF in the notices folder. No notice is named
    in code: put a new PDF in the folder and the pipeline picks it up. Two PDFs that
    would share a key (e.g. two years of one company) are keyed by their full stems."""
    pdfs = sorted(p.name for p in Path(notices_dir).glob("*.pdf"))
    keys = [meeting_key_for(n) for n in pdfs]
    return {(k if keys.count(k) == 1 else slug(Path(n).stem)): n
            for k, n in zip(keys, pdfs, strict=True)}


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    ch_api_key: str | None = Field(default=None, repr=False)
    llama_cloud_api_key: str | None = Field(default=None, repr=False)
    anthropic_api_key: str | None = Field(default=None, repr=False)
    llm_model: str = "claude-sonnet-5-5"  # research: "[provider:]model", classify/backends.py
    extract_model: str = "llamaextract"  # notice reader (extract/backends.py)
    db_path: str = "pennant.db"
    offline: bool = False

    def missing_keys(self) -> list[str]:
        values = {
            "CH_API_KEY": self.ch_api_key,
            "LLAMA_CLOUD_API_KEY": self.llama_cloud_api_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
        }
        return [k for k in REQUIRED_KEYS if not values[k]]

    def require(self, env_name: str) -> str:
        value = getattr(self, env_name.lower(), None)
        if not value:
            raise RuntimeError(f"{env_name} is missing: add it to .env")
        return value


def read_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines; ignores blanks, comments and `export `; strips quotes."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def load_settings(env_file: Path | str | None = None) -> Settings:
    """Settings from `.env` at the repo root, overridden by real environment variables."""
    env = read_env_file(Path(env_file) if env_file else ROOT / ".env")
    env.update({k: v for k, v in os.environ.items() if v})

    def get(name: str) -> str | None:
        return env.get(name) or None

    return Settings(
        ch_api_key=get("CH_API_KEY"),
        llama_cloud_api_key=get("LLAMA_CLOUD_API_KEY"),
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        llm_model=get("LLM_MODEL") or "claude-sonnet-5-5",
        extract_model=get("EXTRACT_MODEL") or "llamaextract",
        db_path=get("PENNANT_DB") or "pennant.db",
        offline=(get("PENNANT_OFFLINE") or "0").lower() in ("1", "true", "yes"),
    )


class Policy(BaseModel):
    """Board-commitment policy flags. Tri flags accept bare or quoted yes/no."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ned_points_threshold: int = 4
    count_seat_under_vote: TriField = "yes"
    aim_counts_as_listed: TriField = "unknown"
    debt_only_listing_counts: TriField = "no"
    chair_designate_counts_as_chair: TriField = "unknown"
    group_dedupe: TriField = "yes"


def load_policy(path: Path | str | None = None) -> Policy:
    data = yaml.safe_load(Path(path or POLICY_PATH).read_text()) or {}
    return Policy.model_validate(data)


def config_hash(policy: Policy, settings: Settings | None = None) -> str:
    """Stable hash of everything that changes answers: the policy and the model name."""
    blob = {"policy": policy.model_dump(), "llm_model": settings.llm_model if settings else None}
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()[:16]
