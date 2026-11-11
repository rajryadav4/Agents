"""Load and validate dealhawk.yaml. `${VAR}` / `${VAR:-default}` pull from the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

from .brain import LearningConfig
from .feedback import FeedbackConfig
from .llm import LLMConfig
from .matching import Watch
from .notify import NOTIFIER_TYPES, Notifier
from .scoring import ScoringConfig
from .sources import SOURCE_TYPES, Http, Source
from .verify import VerifyConfig


class ConfigError(ValueError):
    pass


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match) -> str:
            v = os.environ.get(m.group(1))
            if v is None:
                if m.group(2) is not None:
                    return m.group(2)
                raise ConfigError(f"environment variable {m.group(1)} is not set")
            return v
        return _ENV.sub(sub, value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


@dataclass
class QuietHours:
    start: str = "23:00"
    end: str = "07:00"
    timezone: str = "UTC"
    except_tier: str = "steal"   # tiers at or above this still break through


@dataclass
class AlertConfig:
    min_score: int = 70
    realert_drop_pct: float = 5.0      # re-alert on the same item only if it drops this much further
    realert_after_hours: float = 24.0  # ...or if this long has passed
    max_per_hour: int = 30
    quiet_hours: QuietHours | None = None
    require_in_stock: bool = True


@dataclass
class Config:
    database: str = "dealhawk.db"
    sources: list[Source] = field(default_factory=list)
    notifiers: list[Notifier] = field(default_factory=list)
    watches: list[Watch] = field(default_factory=list)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    learning: LearningConfig = field(default_factory=LearningConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    verify: VerifyConfig = field(default_factory=VerifyConfig)


def _build(cls, data: dict, where: str):
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}; allowed: {sorted(allowed)}")
    return cls(**data)


def parse_config(raw: dict, base_dir: Path | None = None, http: Http | None = None,
                 env: bool = True) -> Config:
    raw = _expand(raw or {}) if env else (raw or {})
    unknown = set(raw) - {"database", "sources", "notify", "watches", "alerts", "scoring",
                          "learning", "llm", "feedback", "verify"}
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {sorted(unknown)}")

    cfg = Config()
    db = raw.get("database", cfg.database)
    if base_dir and db != ":memory:" and not os.path.isabs(db):
        db = str(base_dir / db)
    cfg.database = db

    alerts = dict(raw.get("alerts") or {})
    if alerts.get("quiet_hours"):
        alerts["quiet_hours"] = _build(QuietHours, alerts["quiet_hours"], "alerts.quiet_hours")
    cfg.alerts = _build(AlertConfig, alerts, "alerts")
    cfg.scoring = _build(ScoringConfig, dict(raw.get("scoring") or {}), "scoring")
    cfg.learning = _build(LearningConfig, dict(raw.get("learning") or {}), "learning")
    cfg.llm = _build(LLMConfig, dict(raw.get("llm") or {}), "llm")
    cfg.feedback = _build(FeedbackConfig, dict(raw.get("feedback") or {}), "feedback")
    cfg.verify = _build(VerifyConfig, dict(raw.get("verify") or {}), "verify")
    if cfg.feedback.public_url and not cfg.feedback.secret:
        raise ConfigError("feedback.public_url is set but feedback.secret is empty — set a long random secret")

    names: set[str] = set()
    for i, s in enumerate(raw.get("sources") or []):
        s = dict(s)
        typ = s.pop("type", None)
        if typ not in SOURCE_TYPES:
            raise ConfigError(f"sources[{i}]: type must be one of {sorted(SOURCE_TYPES)}, got {typ!r}")
        if s.pop("enabled", True) is False:
            continue
        name = s.pop("name", None) or s.get("preset") or f"{typ}-{i}"
        if name in names:
            raise ConfigError(f"sources[{i}]: duplicate name {name!r}")
        names.add(name)
        try:
            cfg.sources.append(SOURCE_TYPES[typ](name=name, http=http, **s))
        except TypeError as e:
            raise ConfigError(f"sources[{i}] ({name}): {e}") from e
        except ValueError as e:
            raise ConfigError(str(e)) from e

    for i, n in enumerate(raw.get("notify") or [{"type": "console"}]):
        n = dict(n)
        typ = n.pop("type", None)
        if typ not in NOTIFIER_TYPES:
            raise ConfigError(f"notify[{i}]: type must be one of {sorted(NOTIFIER_TYPES)}, got {typ!r}")
        try:
            cfg.notifiers.append(NOTIFIER_TYPES[typ](http=http, **n))
        except (TypeError, ValueError) as e:
            raise ConfigError(f"notify[{i}] ({typ}): {e}") from e

    for i, w in enumerate(raw.get("watches") or [{"name": "everything"}]):
        w = dict(w)
        w.setdefault("name", f"watch-{i}")
        for k in ("keywords", "exclude", "retailers", "categories"):
            if isinstance(w.get(k), str):
                w[k] = [w[k]]
        cfg.watches.append(_build(Watch, w, f"watches[{i}]"))
    return cfg


def load_config(path: str | Path, http: Http | None = None) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} not found — copy config.example.yaml to {path.name} to get started")
    with path.open() as f:
        raw = yaml.safe_load(f) or {}
    return parse_config(raw, base_dir=path.parent.resolve(), http=http)
