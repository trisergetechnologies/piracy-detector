"""Generate takedown reports (DMCA for piracy, defamation for defamatory content).

Phase D: multi-platform (YouTube + Telegram) and review-status aware packs.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import List, Optional

# ---------------------------------------------------------------------------
# DMCA takedown template (pirated content)
# ---------------------------------------------------------------------------

_DMCA_TEMPLATE = """\
==========================================================
DMCA TAKEDOWN NOTICE
==========================================================
Date       : {date}
Reference  : {report_id}

COMPLAINANT
-----------
Name       : {complainant_name}
Entity     : {entity_name}
Contact    : {contact_email}

INFRINGING CONTENT
------------------
{video_list}

ORIGINAL COPYRIGHTED WORK
--------------------------
The above item(s) contain unauthorised copies / re-uploads of original
content created by or on behalf of {entity_name}.

Platforms covered: YouTube and/or Telegram (see each item).

STATEMENT
---------
I have a good faith belief that the use of the copyrighted material
described above is not authorised by the copyright owner, its agent,
or the law.

I swear, under penalty of perjury, that the information in this
notification is accurate and that I am the copyright owner or am
authorised to act on behalf of the copyright owner.

Signature  : {complainant_name}
Date       : {date}
==========================================================
"""

_DEFAMATION_TEMPLATE = """\
==========================================================
DEFAMATION / HARASSMENT REPORT
==========================================================
Date       : {date}
Reference  : {report_id}

AFFECTED PERSON / ENTITY
--------------------------
Name       : {entity_name}
Type       : {entity_type}
Contact    : {contact_email}

REPORTED ITEMS
--------------
{video_list}

REASON FOR REPORT
-----------------
The above item(s) contain content that is defamatory, harassing, or
otherwise violates platform Community Guidelines as it pertains to
{entity_name}.  Specific concerns include:

{reasons}

We request that the platform review and remove these items in accordance
with its policies against harassment and misinformation.

Submitted by : {complainant_name}
Date         : {date}
==========================================================
"""


def _item_url(v: dict) -> str:
    if v.get("url"):
        return v["url"]
    platform = (v.get("platform") or "youtube").lower()
    vid = v.get("video_id") or ""
    if platform == "telegram" or str(vid).startswith("tg_"):
        return v.get("channel_url") or v.get("url") or ""
    return f"https://www.youtube.com/watch?v={vid}"


def _classification(v: dict) -> str:
    return (
        v.get("classification")
        or (v.get("score_result") or {}).get("classification")
        or ""
    )


def _score_info(v: dict) -> dict:
    sr = v.get("score_result") or {}
    return {
        "severity": v.get("severity") or sr.get("severity") or "N/A",
        "confidence": v.get("confidence") if v.get("confidence") is not None else sr.get("confidence", "N/A"),
        "reason": v.get("reason") or sr.get("reason") or "",
    }


def _format_video_line(v: dict, idx: int) -> str:
    info = _score_info(v)
    platform = v.get("platform") or "youtube"
    fp = v.get("fingerprint_match") or (v.get("score_result") or {}).get("fingerprint_match") or {}
    fp_line = ""
    if fp.get("is_match"):
        fp_line = (
            f"     Fingerprint: {fp.get('similarity', 0):.0%} match → "
            f"{fp.get('official_title') or fp.get('official_video_id')}\n"
        )
    review = v.get("review_status") or "pending"
    return (
        f"  {idx}. [{platform.upper()}] {v.get('title', 'N/A')}\n"
        f"     URL        : {_item_url(v)}\n"
        f"     Channel    : {v.get('channel_title', 'N/A')}\n"
        f"     Views      : {v.get('view_count', v.get('views', 'N/A'))}\n"
        f"     Severity   : {info['severity']}\n"
        f"     Confidence : {info['confidence']}%\n"
        f"     Review     : {review}\n"
        f"{fp_line}"
        f"     Reason     : {info['reason']}\n"
    )


def filter_for_takedown(
    videos: List[dict],
    require_confirmed: bool = False,
    exclude_false_positives: bool = True,
) -> List[dict]:
    """Apply Phase D review-queue rules before packing a report."""
    out = []
    for v in videos:
        status = v.get("review_status") or "pending"
        if exclude_false_positives and status == "false_positive":
            continue
        if require_confirmed and status != "confirmed":
            continue
        out.append(v)
    return out


def generate_dmca_report(
    videos: List[dict],
    entity_name: str,
    complainant_name: str = "",
    contact_email: str = "",
    report_id: str = "",
) -> str:
    video_list = "\n".join(_format_video_line(v, i + 1) for i, v in enumerate(videos))
    return _DMCA_TEMPLATE.format(
        date=datetime.utcnow().strftime("%Y-%m-%d"),
        report_id=report_id or "N/A",
        complainant_name=complainant_name or entity_name,
        entity_name=entity_name,
        contact_email=contact_email or "N/A",
        video_list=video_list or "  (none)",
    )


def generate_defamation_report(
    videos: List[dict],
    entity_name: str,
    entity_type: str = "individual",
    complainant_name: str = "",
    contact_email: str = "",
    report_id: str = "",
) -> str:
    video_list = "\n".join(_format_video_line(v, i + 1) for i, v in enumerate(videos))

    reasons_set: list[str] = []
    for v in videos:
        r = _score_info(v)["reason"]
        if r and r not in reasons_set:
            reasons_set.append(r)
    reasons = "\n".join(f"  - {r}" for r in reasons_set) or "  - Defamatory / harassing content"

    return _DEFAMATION_TEMPLATE.format(
        date=datetime.utcnow().strftime("%Y-%m-%d"),
        report_id=report_id or "N/A",
        entity_name=entity_name,
        entity_type=entity_type,
        complainant_name=complainant_name or entity_name,
        contact_email=contact_email or "N/A",
        video_list=video_list or "  (none)",
        reasons=reasons,
    )


def generate_takedown_report(
    videos: List[dict],
    entity_name: str,
    entity_type: str = "individual",
    complainant_name: str = "",
    contact_email: str = "",
    report_id: str = "",
    require_confirmed: bool = False,
) -> dict:
    """Generate DMCA + defamation packs and CSV for YouTube/Telegram hits."""
    videos = filter_for_takedown(videos, require_confirmed=require_confirmed)

    pirated = [v for v in videos if _classification(v) == "PIRATED"]
    defamatory = [v for v in videos if _classification(v) == "DEFAMATORY"]

    dmca_text = (
        generate_dmca_report(pirated, entity_name, complainant_name, contact_email, report_id)
        if pirated
        else ""
    )
    defam_text = (
        generate_defamation_report(
            defamatory, entity_name, entity_type, complainant_name, contact_email, report_id
        )
        if defamatory
        else ""
    )

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "platform", "video_id", "title", "url", "channel", "classification",
        "confidence", "severity", "review_status", "reason", "views",
        "fingerprint_match",
    ])
    for v in videos:
        info = _score_info(v)
        fp = v.get("fingerprint_match") or {}
        writer.writerow([
            v.get("platform") or "youtube",
            v.get("video_id", ""),
            v.get("title", ""),
            _item_url(v),
            v.get("channel_title", ""),
            _classification(v),
            info["confidence"],
            info["severity"],
            v.get("review_status") or "pending",
            info["reason"],
            v.get("view_count", v.get("views", "")),
            fp.get("similarity", ""),
        ])

    return {
        "report_id": report_id,
        "dmca_report": dmca_text,
        "defamation_report": defam_text,
        "csv": buf.getvalue(),
        "video_count": len(videos),
        "pirated_count": len(pirated),
        "defamatory_count": len(defamatory),
        "platforms": sorted({(v.get("platform") or "youtube") for v in videos}),
    }
