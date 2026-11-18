"""Per-run funnel log: queries -> raw -> filtered -> verified -> flagged.

One JSON file per scan in FUNNEL_LOG_DIR. Every dropped candidate is recorded
with the stage and reason, so a missed pirate copy can be traced to the step
that lost it. Recording never raises and never changes scan behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import Counter
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from config import FUNNEL_LOG_DIR

logger = logging.getLogger(__name__)

_MAX_DROPS = 20000
_MAX_QUERIES = 2000

# Error text from HTTP clients can embed the request URL, including API keys.
# Funnel data is returned in scan summaries, so secrets are stripped on entry.
_SECRET_RE = re.compile(r"(?i)\b(key|api_key|apikey|access_token|token)=[A-Za-z0-9_\-\.]+")


def redact(text: Any) -> str:
    return _SECRET_RE.sub(r"\1=REDACTED", str(text))


class Funnel:
    enabled = True

    def __init__(self, scan_id: str, platform: str, entity_name: str = "") -> None:
        self.scan_id = scan_id
        self.platform = platform
        self.entity_name = entity_name
        self.started_at = datetime.utcnow().isoformat() + "Z"
        self.stages: Dict[str, int] = {}
        self.queries: List[dict] = []
        self.drops: List[dict] = []
        self.drop_counts: Counter = Counter()
        self.errors: Counter = Counter()
        self.error_samples: Dict[str, str] = {}
        self.inaccessible: List[dict] = []
        self.weak: List[dict] = []
        self.notes: Dict[str, Any] = {}
        self._lock = threading.Lock()

    def stage(self, name: str, count: int) -> None:
        self.stages[name] = int(count)

    def query(self, source: str, query: str, raw: int, new: int, error: Optional[str] = None) -> None:
        with self._lock:
            if len(self.queries) < _MAX_QUERIES:
                row = {"source": source, "query": query, "raw": raw, "new": new}
                if error:
                    row["error"] = redact(error)[:200]
                self.queries.append(row)

    def drop(self, item_id: Any, stage: str, reason: str, final: bool = True, **detail: Any) -> None:
        """Record a lost candidate. final=False: skipped at this stage but still alive."""
        with self._lock:
            self.drop_counts[f"{stage}:{reason}"] += 1
            if len(self.drops) < _MAX_DROPS:
                row = {"id": str(item_id), "stage": stage, "reason": reason}
                if not final:
                    row["final"] = False
                if detail:
                    row.update({k: v for k, v in detail.items() if v not in (None, "")})
                self.drops.append(row)

    def drop_many(self, item_ids: Iterable[Any], stage: str, reason: str) -> None:
        for i in item_ids:
            self.drop(i, stage, reason)

    def error(self, kind: str, message: str = "") -> None:
        with self._lock:
            self.errors[kind] += 1
            self.error_samples.setdefault(kind, redact(message)[:300])

    def unreachable(self, what: str, reason: str) -> None:
        self.inaccessible.append({"item": str(what), "reason": redact(reason)[:200]})

    def weak_match(self, item: dict) -> None:
        if len(self.weak) < 500:
            self.weak.append(item)

    def note(self, key: str, value: Any) -> None:
        self.notes[key] = value

    # ------------------------------------------------------------------ output

    def compact(self) -> dict:
        """Small dict for the scan summary (what was scanned / lost / unreachable)."""
        return {
            "stages": dict(self.stages),
            "queries_run": len(self.queries),
            "queries_with_results": sum(1 for q in self.queries if q["raw"]),
            "drop_reasons": dict(self.drop_counts.most_common(25)),
            "errors": {k: {"count": v, "sample": self.error_samples.get(k, "")} for k, v in self.errors.items()},
            "inaccessible": self.inaccessible[:50],
            "weak_matches_not_saved": len(self.weak),
            "notes": self.notes,
        }

    def to_dict(self) -> dict:
        return {
            "scan_id": self.scan_id,
            "platform": self.platform,
            "entity_name": self.entity_name,
            "started_at": self.started_at,
            "finished_at": datetime.utcnow().isoformat() + "Z",
            **self.compact(),
            "queries": self.queries,
            "weak_matches": self.weak,
            "drops": self.drops,
        }

    def save(self) -> Optional[str]:
        try:
            os.makedirs(FUNNEL_LOG_DIR, exist_ok=True)
            path = os.path.join(FUNNEL_LOG_DIR, f"{self.scan_id}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, ensure_ascii=False, indent=1, default=str)
            logger.info(
                "FUNNEL %s [%s] stages=%s drops=%s errors=%s",
                self.scan_id, self.platform, self.stages,
                dict(self.drop_counts.most_common(12)), dict(self.errors),
            )
            return path
        except Exception as exc:  # never break a scan because of logging
            logger.warning("Funnel save failed for %s: %s", self.scan_id, exc)
            return None


class _NullFunnel(Funnel):
    enabled = False

    def __init__(self) -> None:
        super().__init__("none", "none")

    def stage(self, *a: Any, **k: Any) -> None: ...
    def query(self, *a: Any, **k: Any) -> None: ...
    def drop(self, *a: Any, **k: Any) -> None: ...
    def drop_many(self, *a: Any, **k: Any) -> None: ...
    def error(self, *a: Any, **k: Any) -> None: ...
    def unreachable(self, *a: Any, **k: Any) -> None: ...
    def weak_match(self, *a: Any, **k: Any) -> None: ...
    def note(self, *a: Any, **k: Any) -> None: ...
    def save(self) -> Optional[str]:
        return None


_NULL = _NullFunnel()
_current: ContextVar[Optional[Funnel]] = ContextVar("funnel", default=None)


def start(scan_id: str, platform: str, entity_name: str = "") -> Funnel:
    f = Funnel(scan_id, platform, entity_name)
    _current.set(f)
    return f


def current() -> Funnel:
    return _current.get() or _NULL
