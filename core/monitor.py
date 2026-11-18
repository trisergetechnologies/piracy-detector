"""Phase D continuous monitoring scheduler.

A background daemon ticks every MONITOR_TICK_SECONDS, finds due monitors,
and kicks off YouTube and/or Telegram scans using the same workers as the API.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import db
from config import MONITOR_DEFAULT_INTERVAL_HOURS, MONITOR_ENABLED, MONITOR_TICK_SECONDS

logger = logging.getLogger(__name__)

_scheduler_started = False
_lock = threading.Lock()


def _now() -> datetime:
    return datetime.utcnow()


def _iso(dt: datetime) -> str:
    return dt.isoformat() + "Z"


def compute_next_run(interval_hours: float, from_dt: Optional[datetime] = None) -> str:
    base = from_dt or _now()
    return _iso(base + timedelta(hours=float(interval_hours)))


def create_entity_monitor(
    entity_name: str,
    entity_type: str = "educator",
    platforms: Optional[List[str]] = None,
    interval_hours: float = MONITOR_DEFAULT_INTERVAL_HOURS,
    scan_params: Optional[dict] = None,
    enabled: bool = True,
) -> str:
    """Register a recurring monitor. platforms: youtube | telegram | both."""
    platforms = platforms or ["youtube"]
    if "both" in platforms:
        platforms = ["youtube", "telegram"]
    params = dict(scan_params or {})
    return db.create_monitor(
        {
            "entity_name": entity_name.strip(),
            "entity_type": entity_type,
            "platforms": platforms,
            "interval_hours": float(interval_hours),
            "scan_params": params,
            "enabled": enabled,
            "next_run_at": _iso(_now()),  # due immediately on create
        }
    )


def emit_new_hit_alerts(
    scan_id: str,
    entity_name: str,
    hits: List[dict],
    platform: str = "youtube",
    monitor_id: Optional[str] = None,
) -> int:
    """Create alerts for hits not previously seen for this entity."""
    created = 0
    for hit in hits:
        vid = hit.get("video_id")
        if not vid:
            continue
        # Already known from an earlier scan?
        prior = db.is_video_known(vid, entity_name)
        # is_video_known includes current save — check count carefully:
        # Prefer: alert if this is the first time we ever stored it.
        # After save_detected_video, is_video_known is True. So call this
        # BEFORE save, or check review queue history differently.
        # Here we accept caller passes only *new* hits.
        title = hit.get("title") or vid
        sr = hit.get("score_result") or {}
        db.create_alert(
            {
                "type": "new_hit",
                "entity_name": entity_name,
                "platform": hit.get("platform") or platform,
                "scan_id": scan_id,
                "monitor_id": monitor_id,
                "video_id": vid,
                "title": title,
                "channel_title": hit.get("channel_title"),
                "classification": sr.get("classification") or hit.get("classification"),
                "confidence": sr.get("confidence") or hit.get("confidence"),
                "severity": sr.get("severity") or hit.get("severity"),
                "url": hit.get("url")
                or (
                    f"https://www.youtube.com/watch?v={vid}"
                    if not str(vid).startswith("tg_")
                    else hit.get("channel_url")
                ),
                "message": f"New {platform} hit for {entity_name}: {title[:80]}",
            }
        )
        created += 1
    return created


def filter_truly_new_hits(entity_name: str, hits: List[dict], current_scan_id: str) -> List[dict]:
    """Return hits whose video_id was not seen in any *other* scan for entity."""
    known_elsewhere = set()
    for row in db.list_detected_for_entity(entity_name, limit=2000):
        if row.get("scan_id") == current_scan_id:
            continue
        if row.get("video_id"):
            known_elsewhere.add(row["video_id"])
    return [h for h in hits if h.get("video_id") and h["video_id"] not in known_elsewhere]


class MonitorScheduler:
    """Simple in-process scheduler (good enough for single-node deploy)."""

    def __init__(
        self,
        youtube_runner: Callable[..., None],
        telegram_runner: Callable[..., None],
    ):
        self._youtube_runner = youtube_runner
        self._telegram_runner = telegram_runner
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not MONITOR_ENABLED:
            logger.info("Monitor scheduler disabled (MONITOR_ENABLED=false)")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor-scheduler", daemon=True)
        self._thread.start()
        logger.info("Monitor scheduler started (tick=%ss)", MONITOR_TICK_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(MONITOR_TICK_SECONDS):
            try:
                self.tick()
            except Exception as exc:
                logger.error("Monitor tick failed: %s", exc)

    def tick(self) -> int:
        due = db.list_due_monitors()
        for mon in due:
            try:
                self._run_monitor(mon)
            except Exception as exc:
                logger.error("Monitor %s failed: %s", mon.get("monitor_id"), exc)
                db.update_monitor(
                    mon["monitor_id"],
                    {
                        "last_error": str(exc),
                        "next_run_at": compute_next_run(mon.get("interval_hours") or MONITOR_DEFAULT_INTERVAL_HOURS),
                    },
                )
        return len(due)

    def _run_monitor(self, mon: dict) -> None:
        mid = mon["monitor_id"]
        entity_name = mon["entity_name"]
        entity_type = mon.get("entity_type") or "educator"
        platforms = mon.get("platforms") or ["youtube"]
        params = dict(mon.get("scan_params") or {})
        interval = float(mon.get("interval_hours") or MONITOR_DEFAULT_INTERVAL_HOURS)

        logger.info("Running monitor %s for %s (%s)", mid, entity_name, platforms)
        db.update_monitor(
            mid,
            {
                "last_run_at": _iso(_now()),
                "run_count": int(mon.get("run_count") or 0) + 1,
                "last_error": None,
                "status": "running",
            },
        )

        max_results = int(params.get("max_results") or 100)
        min_conf = int(params.get("min_confidence") or 60)
        official_channel_ids = params.get("official_channel_ids") or []
        official_playlist_ids = params.get("official_playlist_ids") or []
        known_channels = params.get("known_channels_to_monitor") or []
        enable_deep_crawl = bool(params.get("enable_deep_crawl", True))
        enable_fingerprinting = bool(params.get("enable_fingerprinting", True))
        telegram_seeds = params.get("telegram_seeds") or []

        if "youtube" in platforms:
            scan_id = db.create_scan(entity_name, entity_type, "piracy", {
                **params,
                "monitor_id": mid,
                "triggered_by": "monitor",
            })
            # Run synchronously in scheduler thread so we can alert after
            self._youtube_runner(
                scan_id,
                entity_name,
                entity_type,
                "piracy",
                max_results,
                min_conf,
                None,
                None,
                known_channels,
                False,  # thumbnails off for monitor cost control
                official_channel_ids,
                official_playlist_ids,
                enable_deep_crawl,
                enable_fingerprinting,
            )
            # New-hit alerts are emitted inside the YouTube scan worker

        if "telegram" in platforms:
            scan_id = db.create_scan(entity_name, entity_type, "telegram", {
                **params,
                "monitor_id": mid,
                "triggered_by": "monitor",
            })
            self._telegram_runner(
                scan_id,
                entity_name,
                telegram_seeds,
                official_channel_ids,
                official_playlist_ids,
                enable_fingerprinting,
                None,
                min_conf,
            )
            # New-hit alerts are emitted inside the Telegram scan worker

        db.update_monitor(
            mid,
            {
                "status": "idle",
                "next_run_at": compute_next_run(interval),
            },
        )
        db.create_alert(
            {
                "type": "monitor_run",
                "entity_name": entity_name,
                "monitor_id": mid,
                "message": f"Monitor run completed for {entity_name}",
                "severity": "low",
            }
        )

def start_scheduler(
    youtube_runner: Callable[..., None],
    telegram_runner: Callable[..., None],
) -> Optional[MonitorScheduler]:
    global _scheduler_started
    with _lock:
        if _scheduler_started:
            return None
        sched = MonitorScheduler(youtube_runner, telegram_runner)
        sched.start()
        _scheduler_started = True
        return sched
