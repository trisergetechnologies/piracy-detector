"""Database layer – MongoDB primary, in-memory fallback.

Collections / tables:
    scans             – one doc per API scan invocation
    detected_videos   – every video classified as DEFAMATORY or PIRATED
    channels          – aggregated info on channels that post offending content
    takedown_reports  – generated takedown requests
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import MONGODB_DB_NAME, MONGODB_URI

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Try connecting to MongoDB; fall back to in-memory
# ---------------------------------------------------------------------------

_mongo_client = None
_db = None

try:
    from pymongo import MongoClient
    from pymongo.errors import ServerSelectionTimeoutError

    _mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=3000)
    _mongo_client.admin.command("ping")
    _db = _mongo_client[MONGODB_DB_NAME]
    logger.info("Connected to MongoDB at %s", MONGODB_URI)
except Exception as exc:
    logger.warning("MongoDB unavailable (%s) – using in-memory storage", exc)
    _db = None

# ---------------------------------------------------------------------------
# In-memory fallback storage
# ---------------------------------------------------------------------------

_memory: Dict[str, List[dict]] = {
    "scans": [],
    "detected_videos": [],
    "channels": [],
    "takedown_reports": [],
    "fingerprints": [],
    "telegram_seeds": [],
    "monitors": [],
    "alerts": [],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _col(name: str):
    """Return a MongoDB collection if available, else None."""
    return _db[name] if _db is not None else None


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ---------------------------------------------------------------------------
# Scans
# ---------------------------------------------------------------------------


def create_scan(entity_name: str, entity_type: str, scan_type: str, params: dict) -> str:
    """Create a new scan record. Returns the scan_id."""
    scan_id = f"scan_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{_new_id()}"
    doc = {
        "scan_id": scan_id,
        "entity_name": entity_name,
        "entity_type": entity_type,
        "scan_type": scan_type,
        "params": params,
        "status": "running",
        "created_at": _now(),
        "completed_at": None,
        "summary": {},
    }
    col = _col("scans")
    if col is not None:
        col.insert_one(doc)
    else:
        _memory["scans"].append(doc)
    return scan_id


def update_scan_progress(scan_id: str, progress: str) -> None:
    """Update the progress message for a running scan."""
    col = _col("scans")
    if col is not None:
        col.update_one({"scan_id": scan_id}, {"$set": {"progress": progress}})
    else:
        for s in _memory["scans"]:
            if s["scan_id"] == scan_id:
                s["progress"] = progress
                break


def complete_scan(scan_id: str, summary: dict) -> None:
    col = _col("scans")
    update = {"status": "completed", "completed_at": _now(), "summary": summary, "progress": "done"}
    if col is not None:
        col.update_one({"scan_id": scan_id}, {"$set": update})
    else:
        for s in _memory["scans"]:
            if s["scan_id"] == scan_id:
                s.update(update)
                break


def get_scan(scan_id: str) -> Optional[dict]:
    col = _col("scans")
    if col is not None:
        doc = col.find_one({"scan_id": scan_id}, {"_id": 0})
        return doc
    for s in _memory["scans"]:
        if s["scan_id"] == scan_id:
            return s
    return None


def list_scans(limit: int = 50) -> List[dict]:
    col = _col("scans")
    if col is not None:
        return list(col.find({}, {"_id": 0}).sort("created_at", -1).limit(limit))
    return sorted(_memory["scans"], key=lambda s: s["created_at"], reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Detected videos
# ---------------------------------------------------------------------------


def save_detected_video(scan_id: str, video: dict) -> None:
    """Save a video that was classified as DEFAMATORY or PIRATED."""
    doc = {
        "scan_id": scan_id,
        "video_id": video.get("video_id"),
        "saved_at": _now(),
        "review_status": video.get("review_status") or "pending",
        "platform": video.get("platform") or "youtube",
        **video,
    }
    # Ensure defaults win only if missing in **video overwrite — re-apply
    doc["review_status"] = video.get("review_status") or doc.get("review_status") or "pending"
    doc["platform"] = video.get("platform") or doc.get("platform") or "youtube"
    col = _col("detected_videos")
    if col is not None:
        col.update_one(
            {"video_id": doc["video_id"], "scan_id": scan_id},
            {"$set": doc},
            upsert=True,
        )
    else:
        existing = next(
            (
                v
                for v in _memory["detected_videos"]
                if v.get("video_id") == doc["video_id"] and v.get("scan_id") == scan_id
            ),
            None,
        )
        if existing:
            existing.update(doc)
        else:
            _memory["detected_videos"].append(doc)


def get_detected_videos(scan_id: str) -> List[dict]:
    col = _col("detected_videos")
    if col is not None:
        return list(col.find({"scan_id": scan_id}, {"_id": 0}).sort("saved_at", -1))
    return [v for v in _memory["detected_videos"] if v.get("scan_id") == scan_id]


def is_video_known(video_id: str, entity_name: str) -> bool:
    """Check if this video has been flagged before (any scan for this entity)."""
    col = _col("detected_videos")
    if col is not None:
        return col.find_one({"video_id": video_id, "entity_name": entity_name}) is not None
    return any(
        v.get("video_id") == video_id and v.get("entity_name") == entity_name
        for v in _memory["detected_videos"]
    )


def update_review_status(
    scan_id: str,
    video_id: str,
    review_status: str,
    note: str = "",
) -> Optional[dict]:
    """Set review_status: pending | confirmed | false_positive."""
    if review_status not in ("pending", "confirmed", "false_positive"):
        raise ValueError("Invalid review_status")
    update = {
        "review_status": review_status,
        "reviewed_at": _now(),
        "review_note": note or "",
    }
    col = _col("detected_videos")
    if col is not None:
        col.update_one({"scan_id": scan_id, "video_id": video_id}, {"$set": update})
        return col.find_one({"scan_id": scan_id, "video_id": video_id}, {"_id": 0})
    for v in _memory["detected_videos"]:
        if v.get("scan_id") == scan_id and v.get("video_id") == video_id:
            v.update(update)
            return v
    return None


def list_review_queue(
    status: str = "pending",
    entity_name: Optional[str] = None,
    limit: int = 100,
) -> List[dict]:
    col = _col("detected_videos")
    query: dict = {"review_status": status}
    if entity_name:
        query["entity_name"] = entity_name
    if col is not None:
        return list(col.find(query, {"_id": 0}).sort("saved_at", -1).limit(limit))
    rows = [
        v for v in _memory["detected_videos"]
        if v.get("review_status", "pending") == status
        and (not entity_name or v.get("entity_name") == entity_name)
    ]
    return sorted(rows, key=lambda v: v.get("saved_at", ""), reverse=True)[:limit]


def list_detected_for_entity(entity_name: str, limit: int = 500) -> List[dict]:
    col = _col("detected_videos")
    if col is not None:
        return list(
            col.find({"entity_name": entity_name}, {"_id": 0})
            .sort("saved_at", -1)
            .limit(limit)
        )
    rows = [v for v in _memory["detected_videos"] if v.get("entity_name") == entity_name]
    return sorted(rows, key=lambda v: v.get("saved_at", ""), reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def upsert_channel(channel_id: str, data: dict) -> None:
    col = _col("channels")
    if col is not None:
        col.update_one(
            {"channel_id": channel_id},
            {"$set": data, "$inc": {"offending_video_count": 1}},
            upsert=True,
        )
    else:
        existing = next((c for c in _memory["channels"] if c.get("channel_id") == channel_id), None)
        if existing:
            existing.update(data)
            existing["offending_video_count"] = existing.get("offending_video_count", 0) + 1
        else:
            data["channel_id"] = channel_id
            data["offending_video_count"] = 1
            _memory["channels"].append(data)


def list_channels(limit: int = 50) -> List[dict]:
    col = _col("channels")
    if col is not None:
        return list(col.find({}, {"_id": 0}).sort("offending_video_count", -1).limit(limit))
    return sorted(_memory["channels"], key=lambda c: c.get("offending_video_count", 0), reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Fingerprints (Phase B)
# ---------------------------------------------------------------------------


def save_fingerprint(video_id: str, payload: dict) -> None:
    """Persist a fingerprint payload keyed by video_id (+ backend)."""
    doc = {
        "video_id": video_id,
        "backend": payload.get("backend", "unknown"),
        "saved_at": _now(),
        **payload,
    }
    col = _col("fingerprints")
    if col is not None:
        col.update_one(
            {"video_id": video_id, "backend": doc["backend"]},
            {"$set": doc},
            upsert=True,
        )
    else:
        existing = next(
            (
                f
                for f in _memory["fingerprints"]
                if f.get("video_id") == video_id and f.get("backend") == doc["backend"]
            ),
            None,
        )
        if existing:
            existing.update(doc)
        else:
            _memory["fingerprints"].append(doc)


def get_fingerprint(video_id: str, backend: Optional[str] = None) -> Optional[dict]:
    col = _col("fingerprints")
    query: dict = {"video_id": video_id}
    if backend:
        query["backend"] = backend
    if col is not None:
        return col.find_one(query, {"_id": 0})
    for f in _memory["fingerprints"]:
        if f.get("video_id") != video_id:
            continue
        if backend and f.get("backend") != backend:
            continue
        return f
    return None


# ---------------------------------------------------------------------------
# Telegram seeds (Phase C)
# ---------------------------------------------------------------------------


def save_telegram_seeds(entity_name: str, seeds: List[dict], scan_id: Optional[str] = None) -> None:
    """Upsert harvested Telegram seeds for an entity."""
    col = _col("telegram_seeds")
    for s in seeds or []:
        doc = {
            "entity_name": entity_name,
            "kind": s.get("kind"),
            "value": s.get("value"),
            "url": s.get("url"),
            "source": s.get("source"),
            "scan_id": scan_id,
            "updated_at": _now(),
            **{k: v for k, v in s.items() if k not in ("kind", "value", "url", "source")},
        }
        if col is not None:
            col.update_one(
                {
                    "entity_name": entity_name,
                    "kind": doc["kind"],
                    "value": doc["value"],
                },
                {"$set": doc},
                upsert=True,
            )
        else:
            existing = next(
                (
                    x
                    for x in _memory["telegram_seeds"]
                    if x.get("entity_name") == entity_name
                    and x.get("kind") == doc["kind"]
                    and x.get("value") == doc["value"]
                ),
                None,
            )
            if existing:
                existing.update(doc)
            else:
                _memory["telegram_seeds"].append(doc)


def list_telegram_seeds(entity_name: Optional[str] = None, limit: int = 200) -> List[dict]:
    col = _col("telegram_seeds")
    if col is not None:
        q = {"entity_name": entity_name} if entity_name else {}
        return list(col.find(q, {"_id": 0}).sort("updated_at", -1).limit(limit))
    rows = _memory["telegram_seeds"]
    if entity_name:
        rows = [r for r in rows if r.get("entity_name") == entity_name]
    return sorted(rows, key=lambda r: r.get("updated_at", ""), reverse=True)[:limit]


# ---------------------------------------------------------------------------
# Monitors + Alerts (Phase D)
# ---------------------------------------------------------------------------


def create_monitor(doc: dict) -> str:
    monitor_id = f"mon_{_new_id()}"
    record = {
        "monitor_id": monitor_id,
        "created_at": _now(),
        "updated_at": _now(),
        "enabled": True,
        "last_run_at": None,
        "next_run_at": doc.get("next_run_at") or _now(),
        "run_count": 0,
        **doc,
    }
    col = _col("monitors")
    if col is not None:
        col.insert_one(record)
    else:
        _memory["monitors"].append(record)
    return monitor_id


def update_monitor(monitor_id: str, fields: dict) -> Optional[dict]:
    fields = {**fields, "updated_at": _now()}
    col = _col("monitors")
    if col is not None:
        col.update_one({"monitor_id": monitor_id}, {"$set": fields})
        return col.find_one({"monitor_id": monitor_id}, {"_id": 0})
    for m in _memory["monitors"]:
        if m.get("monitor_id") == monitor_id:
            m.update(fields)
            return m
    return None


def get_monitor(monitor_id: str) -> Optional[dict]:
    col = _col("monitors")
    if col is not None:
        return col.find_one({"monitor_id": monitor_id}, {"_id": 0})
    return next((m for m in _memory["monitors"] if m.get("monitor_id") == monitor_id), None)


def list_monitors(limit: int = 100) -> List[dict]:
    col = _col("monitors")
    if col is not None:
        return list(col.find({}, {"_id": 0}).sort("created_at", -1).limit(limit))
    return sorted(_memory["monitors"], key=lambda m: m.get("created_at", ""), reverse=True)[:limit]


def list_due_monitors(now_iso: Optional[str] = None) -> List[dict]:
    now_iso = now_iso or _now()
    col = _col("monitors")
    if col is not None:
        return list(
            col.find(
                {"enabled": True, "next_run_at": {"$lte": now_iso}},
                {"_id": 0},
            )
        )
    return [
        m
        for m in _memory["monitors"]
        if m.get("enabled") and (m.get("next_run_at") or "") <= now_iso
    ]


def delete_monitor(monitor_id: str) -> bool:
    col = _col("monitors")
    if col is not None:
        res = col.delete_one({"monitor_id": monitor_id})
        return res.deleted_count > 0
    before = len(_memory["monitors"])
    _memory["monitors"] = [m for m in _memory["monitors"] if m.get("monitor_id") != monitor_id]
    return len(_memory["monitors"]) < before


def create_alert(doc: dict) -> str:
    alert_id = f"alrt_{_new_id()}"
    record = {
        "alert_id": alert_id,
        "created_at": _now(),
        "read": False,
        **doc,
    }
    col = _col("alerts")
    if col is not None:
        col.insert_one(record)
    else:
        _memory["alerts"].append(record)
    return alert_id


def list_alerts(
    unread_only: bool = False,
    limit: int = 100,
) -> List[dict]:
    col = _col("alerts")
    query: dict = {"read": False} if unread_only else {}
    if col is not None:
        return list(col.find(query, {"_id": 0}).sort("created_at", -1).limit(limit))
    rows = _memory["alerts"]
    if unread_only:
        rows = [a for a in rows if not a.get("read")]
    return sorted(rows, key=lambda a: a.get("created_at", ""), reverse=True)[:limit]


def mark_alert_read(alert_id: str, read: bool = True) -> Optional[dict]:
    col = _col("alerts")
    if col is not None:
        col.update_one({"alert_id": alert_id}, {"$set": {"read": read}})
        return col.find_one({"alert_id": alert_id}, {"_id": 0})
    for a in _memory["alerts"]:
        if a.get("alert_id") == alert_id:
            a["read"] = read
            return a
    return None


def mark_all_alerts_read() -> int:
    col = _col("alerts")
    if col is not None:
        res = col.update_many({"read": False}, {"$set": {"read": True}})
        return res.modified_count
    n = 0
    for a in _memory["alerts"]:
        if not a.get("read"):
            a["read"] = True
            n += 1
    return n


# ---------------------------------------------------------------------------
# Takedown reports
# ---------------------------------------------------------------------------


def save_takedown_report(report: dict) -> str:
    report_id = f"rpt_{_new_id()}"
    doc = {"report_id": report_id, "created_at": _now(), **report}
    col = _col("takedown_reports")
    if col is not None:
        col.insert_one(doc)
    else:
        _memory["takedown_reports"].append(doc)
    return report_id


def get_takedown_report(report_id: str) -> Optional[dict]:
    col = _col("takedown_reports")
    if col is not None:
        return col.find_one({"report_id": report_id}, {"_id": 0})
    return next((r for r in _memory["takedown_reports"] if r.get("report_id") == report_id), None)


# ---------------------------------------------------------------------------
# Status helper
# ---------------------------------------------------------------------------

def db_status() -> str:
    return "MongoDB" if _db is not None else "In-Memory"
