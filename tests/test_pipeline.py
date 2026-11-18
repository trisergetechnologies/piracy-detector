"""Discovery/crawl logic and the end-to-end scan worker, with network stubbed out."""

import asyncio
import json
import os

import pytest

import config
from core import funnel, search_engine
from core.catalog import normalize_title, title_tokens
from core.telegram import scanner
from core.telegram.client import FloodStop, resolve_public

OFFICIAL_CH = "UC" + "o" * 22
PIRATE_CH = "UC" + "p" * 22
BIG_CH = "UC" + "b" * 22
TITLE = "Thermodynamics First Law Explained with Numericals"


def _catalog():
    return {
        "video_ids": {"off00000001"},
        "channel_ids": {OFFICIAL_CH},
        "entries": [{"video_id": "off00000001", "title": TITLE, "channel_id": OFFICIAL_CH}],
        "title_index": [{"video_id": "off00000001", "title": TITLE,
                         "norm_title": normalize_title(TITLE), "tokens": title_tokens(TITLE)}],
        "duration_map": {"off00000001": 3605},
        "unresolved": [],
    }


def _hit(vid, channel=PIRATE_CH, title=TITLE, **kw):
    return {"video_id": vid, "title": title, "channel_id": channel, "channel_title": "ch",
            "published_at": "", "thumbnail_url": "", "description": "", "source": "stub", **kw}


class FakeYT:
    """Stands in for YouTubeClient: counts paid searches, can run out of quota."""

    def __init__(self, quota_after=None, subs=None):
        self.search_calls = []
        self.quota_after = quota_after
        self.crawled = []
        self.subs = subs or {}

    def search(self, **params):
        self.search_calls.append(params)
        if self.quota_after is not None and len(self.search_calls) > self.quota_after:
            raise RuntimeError("HttpError 403 quotaExceeded")
        n = len(self.search_calls)
        return {"items": [{"id": {"videoId": f"api{n:08d}"},
                           "snippet": {"title": "x", "channelId": PIRATE_CH}}]}

    def resolve_channel_id(self, ref):
        return ref if str(ref).startswith("UC") else None

    def get_channel_uploads(self, cid, max_results=50):
        self.crawled.append(cid)
        return [_hit(f"up{len(self.crawled):02d}{i:07d}", channel=cid, title=f"upload {i}") for i in range(3)]

    def get_channels_batch(self, ids):
        return {c: {"statistics": {"subscriberCount": str(self.subs.get(c, 10))},
                    "snippet": {"title": "ch", "description": ""}} for c in ids}

    def get_video_details(self, ids):
        return [{"id": i, "snippet": {"title": "", "channelId": "", "description": "full description"},
                 "statistics": {"viewCount": "7"}, "contentDetails": {"duration": "PT1H4S"}} for i in ids]


@pytest.fixture
def stub_search(monkeypatch):
    calls = {"relevance": [], "date": []}

    def fake_ytdlp(query, max_results=30, date_from=None, date_to=None, by_date=False):
        calls["date" if by_date else "relevance"].append(query)
        if "full lecture" in query and not by_date:
            return [_hit("copy0000001"), _hit("big00000001", channel=BIG_CH),
                    _hit("off00000001", channel=OFFICIAL_CH)]
        if by_date and query == calls["date"][0]:
            return [_hit("fresh000001", title="brand new upload")]
        return []

    monkeypatch.setattr(search_engine, "search_ytdlp", fake_ytdlp)
    monkeypatch.setattr(search_engine, "_ytdlp_available", lambda: True)
    monkeypatch.setattr(search_engine, "monitor_rss_channels", lambda ids: [])
    return calls


def test_discovery_uses_date_search_and_never_returns_official(stub_search):
    fn = funnel.start("t-disc", "youtube", "Brand")
    yt = FakeYT()
    vids = search_engine.discover_videos("Brand", scan_type="piracy", yt_client=yt, catalog=_catalog())
    ids = {v["video_id"] for v in vids}
    assert "copy0000001" in ids and "fresh000001" in ids
    assert "off00000001" not in ids
    assert stub_search["date"], "upload-date ordered search must run"
    assert any("free me" in q for q in stub_search["relevance"])            # Hinglish expansion
    assert any(TITLE in q for q in stub_search["relevance"])                # exact official title
    assert fn.drop_counts["filter:official_content"] == 1


def test_api_search_respects_budget_and_uses_date_order(stub_search, monkeypatch):
    monkeypatch.setattr(search_engine, "YOUTUBE_SEARCH_BUDGET_PER_SCAN", 300)
    fn = funnel.start("t-budget", "youtube", "Brand")
    yt = FakeYT()
    search_engine.discover_videos("Brand", scan_type="piracy", yt_client=yt, catalog=_catalog(),
                                  enable_deep_crawl=False)
    assert len(yt.search_calls) == 3
    assert all(c["order"] == "date" for c in yt.search_calls)
    assert all("videoDuration" not in c for c in yt.search_calls)   # short copies are not excluded
    assert fn.notes["api_search_stopped"]["reason"] == "budget"


def test_api_search_stops_on_quota_and_records_it(stub_search):
    fn = funnel.start("t-quota", "youtube", "Brand")
    yt = FakeYT(quota_after=2)
    vids = search_engine.discover_videos("Brand", scan_type="piracy", yt_client=yt, catalog=_catalog(),
                                         enable_deep_crawl=False)
    assert len(yt.search_calls) == 3                       # stopped right after the failure
    assert fn.notes["api_search_stopped"]["reason"] == "quota"
    assert fn.errors["youtube_api_quota"] == 1
    assert any(v["video_id"] == "copy0000001" for v in vids)   # free results survive


def test_deep_crawl_picks_channels_by_evidence_not_size(stub_search):
    funnel.start("t-crawl", "youtube", "Brand")
    yt = FakeYT(subs={BIG_CH: 5_000_000})
    search_engine.discover_videos("Brand", scan_type="piracy", yt_client=yt, catalog=_catalog())
    assert PIRATE_CH in yt.crawled
    assert BIG_CH in yt.crawled          # 5M-subscriber channel hosting an official title is crawled
    assert OFFICIAL_CH not in yt.crawled


# ---------------------------------------------------------------- scan worker


def test_scan_finds_copy_when_llm_is_down(stub_search, monkeypatch):
    """Reproduces production: OpenAI returns 429/401 for every video.
    Before: 0 pirated. Now: the title+duration copy is flagged LIKELY and the
    run explains itself."""
    import app
    import db
    from core import content_analyzer

    yt = FakeYT()
    monkeypatch.setattr(app, "yt_client", yt)
    monkeypatch.setattr(app, "ingest_official_catalog", lambda *a, **k: _catalog())
    monkeypatch.setattr(app, "run_fingerprint_matching",
                        lambda **k: {"enabled": True, "matches": 0, "transcript": {"candidates_tested": 0}})

    class Boom:
        class chat:
            class completions:
                @staticmethod
                def create(**_):
                    raise RuntimeError("Error code: 429 - You have no credits remaining (insufficient_quota)")

    monkeypatch.setattr(content_analyzer, "_get_client", lambda: Boom)
    monkeypatch.setattr(content_analyzer, "_breaker", {"fatal_streak": 0, "open_until": 0.0, "last_error": ""})

    scan_id = db.create_scan("Brand", "educator", "piracy", {})
    app._do_scan_work(scan_id, "Brand", "educator", "piracy", 50, 60, None, None, [], False,
                      [OFFICIAL_CH], [], True, True)

    scan = db.get_scan(scan_id)
    summary = scan["summary"]
    saved = {v["video_id"]: v for v in db.get_detected_videos(scan_id)}
    assert "copy0000001" in saved
    sr = saved["copy0000001"]["score_result"]
    assert sr["classification"] == "PIRATED" and sr["reason"].startswith("LIKELY:")
    assert "off00000001" not in saved
    assert summary["pirated_found"] >= 1 and summary["tiers"]["likely"] >= 1
    assert any("AI classifier unavailable" in w for w in summary["warnings"])
    assert summary["diagnostics"]["errors"]["llm_auth_or_billing"]["count"] >= 1

    # API response shape is unchanged
    row = app._serialize_video(saved["copy0000001"])
    assert set(row) == {
        "video_id", "title", "url", "channel_title", "channel_id", "channel_url", "published_at",
        "views", "likes", "comments", "duration", "thumbnail_url", "classification", "confidence",
        "severity", "reason", "final_score", "thumbnail_analysis", "sub_scores", "heuristic_score",
        "heuristic_signals", "catalog_match", "discovery_source", "fingerprint_match", "platform",
        "message_id", "review_status", "review_note", "scan_id"}

    # Funnel log: every candidate that was not saved has a recorded reason
    with open(os.path.join(config.FUNNEL_LOG_DIR, f"{scan_id}.json"), encoding="utf-8") as fh:
        log = json.load(fh)
    assert log["stages"]["raw_results"] >= log["stages"]["after_filters"] >= log["stages"]["flagged"]
    dropped = {d["id"] for d in log["drops"]}
    analysed_not_saved = log["stages"]["after_filters"] - log["stages"]["flagged"]
    assert analysed_not_saved == sum(
        1 for d in log["drops"] if d["stage"] in ("classify", "threshold"))
    assert "off00000001" in dropped


# ---------------------------------------------------------------- telegram


def test_entity_terms_drop_generic_words():
    assert [t.lower() for t in scanner._entity_terms("Quant Finance By Subhadra")] == [
        "quant finance by subhadra", "quant finance", "by subhadra", "quantsubhadra"]
    pw = [t.lower() for t in scanner._entity_terms("Physics Wallah")]
    assert "physics" not in pw and "physics wallah" in pw and "physicswallah" in pw
    assert "va-nee-ni" in [t.lower() for t in scanner._entity_terms("VA-NEE-NI Excellence")]
    assert "gurukul commerce" in [
        t.lower() for t in scanner._entity_terms("Gurukul Commerce Academy (GCA Akola)")]


def test_telegram_search_queries_have_variants():
    qs = scanner.search_queries("Physics Wallah")
    assert qs[0] == "Physics Wallah"
    assert "Physics Wallah free" in qs and "physicswallah" in qs
    assert len(qs) <= config.TELEGRAM_MAX_SEARCH_QUERIES


def test_media_matches_catalog_by_name_and_duration():
    cat = _catalog()
    media = {"filename": "Thermodynamics_First_Law_Explained_with_Numericals.mp4", "duration": 3606}
    m = scanner.media_catalog_match(media, "", cat)
    assert m["title_sim"] >= 0.45 and m["duration_match"] and m["official_video_id"] == "off00000001"
    renamed = scanner.media_catalog_match({"filename": "L4.mkv", "duration": 3605}, "", cat)
    assert renamed["duration_match"] and not renamed["title_sim"]
    assert not scanner.media_catalog_match({"filename": "L4.mkv", "duration": 900}, "", cat)["duration_match"]


def test_message_tiers():
    fp = {"is_match": True, "tier": "confirmed", "similarity": 0.93, "backend": "chromaprint"}
    assert scanner.classify_message({"media": True, "fp": fp})[0] == "confirmed"
    tier, conf, _ = scanner.classify_message({"media": True, "title_sim": 0.8, "duration_match": True})
    assert tier == "likely" and 70 <= conf <= 89
    tier, conf, _ = scanner.classify_message({"entity": True, "dump_kw": True})
    assert tier == "weak" and conf <= 69
    assert scanner.classify_message({"media": True})[0] is None           # a bare file is not a hit
    assert scanner.classify_message({"entity": True})[0] is None          # a bare mention is not a hit


def test_private_invites_are_never_joined():
    class Client:
        def __getattr__(self, name):
            raise AssertionError(f"client.{name} must not be used for a private invite")

    entity, why = asyncio.run(resolve_public(Client(), {"kind": "invite", "value": "AbCdEf"}))
    assert entity is None and why == "private_invite_not_joined"


def test_seed_order_resumes_deferred_first():
    seeds = [{"kind": "public", "value": n} for n in ("old", "new", "deferred")]
    state = {"scanned": {"public:old": 100.0}, "deferred": ["public:deferred"]}
    assert [s["value"] for s in scanner.order_seeds(seeds, state)] == ["deferred", "new", "old"]


class _Entity:
    def __init__(self, name, verified=False):
        self.id, self.username, self.verified = abs(hash(name)) % 10**9, name, verified
        self.broadcast = True


def _run_tg_scan(monkeypatch, channels, flood_on=None, seeds=None, official=None, search=None):
    """channels: {username: [message dicts]}"""
    resolved = []

    class Client:
        async def disconnect(self):
            pass

    async def ensure(_c):
        return True

    async def fake_resolve(_c, seed):
        if seed.get("kind") == "invite":
            return None, "private_invite_not_joined"
        name = seed["value"]
        resolved.append(name)
        if name == flood_on:
            raise FloodStop(900, f"resolve:{name}")
        if name not in channels:
            return None, "resolve_failed: no such channel"
        return _Entity(name), ""

    async def fake_meta(_c, entity):
        return {"id": entity.id, "title": f"{entity.username} title", "username": entity.username,
                "about": "", "participants_count": 5, "url": f"https://t.me/{entity.username}"}

    async def fake_messages(_c, entity, limit=150):
        return channels[entity.username]

    async def fake_search(_c, query, limit=20):
        return [{"kind": "public", "value": n, "url": f"https://t.me/{n}", "source": f"search:{query}"}
                for n in (search or [])]

    monkeypatch.setattr(scanner, "_build_client", lambda: Client())
    monkeypatch.setattr(scanner, "_ensure_connected", ensure)
    monkeypatch.setattr(scanner, "resolve_public", fake_resolve)
    monkeypatch.setattr(scanner, "fetch_channel_meta", fake_meta)
    monkeypatch.setattr(scanner, "iter_recent_messages", fake_messages)
    monkeypatch.setattr(scanner, "search_public_channels", fake_search)
    monkeypatch.setattr(scanner, "telegram_configured", lambda: True)
    result = scanner.run_telegram_scan(
        "Physics Wallah", manual_seeds=seeds or [], official_telegram=official, catalog=_catalog(),
        enable_fingerprinting=False)
    return result, resolved


def _msg(i, text="", media=None, fwd=None):
    return {"message_id": i, "date": "", "text": text, "media": media, "views": 3, "forwards": 0,
            "forwarded_from": fwd}


VIDEO = {"kind": "video", "filename": "Thermodynamics_First_Law_Explained_with_Numericals.mp4",
         "mime": "video/mp4", "size": 10**8, "duration": 3605}


def test_telegram_scan_works_without_seeds_and_crawls_forwards(monkeypatch):
    funnel.start("t-tg1", "telegram", "Physics Wallah")
    channels = {
        "pw_free_batch": [
            _msg(1, "Physics Wallah paid batch free", media=VIDEO, fwd={"username": "source_dump", "title": "s"}),
            _msg(2, "good morning"),
            _msg(3, "join https://t.me/+PrivateHash123 for more"),
        ],
        "source_dump": [_msg(9, "", media=VIDEO)],
    }
    result, resolved = _run_tg_scan(monkeypatch, channels, search=["pw_free_batch"])
    assert result["ok"] and result["mode"] == "full"
    assert resolved[:2] == ["pw_free_batch", "source_dump"]      # found by search, then by forward
    by_channel = {h["channel_title"]: h for h in result["hits"]}
    assert by_channel["pw_free_batch title"]["reason"].startswith("LIKELY:")
    assert by_channel["source_dump title"]["reason"].startswith("LIKELY:")   # name + duration, no text
    assert all(h["confidence"] >= 70 for h in result["hits"])
    assert "tier:likely" in by_channel["source_dump title"]["heuristic"]["signals"]


def test_telegram_official_channel_is_not_flagged(monkeypatch):
    funnel.start("t-tg2", "telegram", "Physics Wallah")
    channels = {"physicswallah": [_msg(1, "Physics Wallah full course free", media=VIDEO)]}
    result, _ = _run_tg_scan(monkeypatch, channels, seeds=["@physicswallah"], official=["@physicswallah"])
    assert result["hits"] == []
    assert result["stats"]["official_channels_skipped_hits"] == 1


def test_telegram_floodwait_stops_cleanly_and_resumes(monkeypatch):
    fn = funnel.start("t-tg3", "telegram", "Physics Wallah")
    channels = {"first": [_msg(1, "Physics Wallah paid batch free", media=VIDEO)], "second": [], "third": []}
    result, resolved = _run_tg_scan(monkeypatch, channels, flood_on="second",
                                    seeds=["@first", "@second", "@third"])
    assert result["ok"] and len(result["hits"]) == 1               # hits before the stop are kept
    assert resolved == ["first", "second"]
    assert "FloodWait 900s" in result["stats"]["stopped_reason"]
    assert fn.errors["telegram_floodwait"] == 1
    state = scanner.load_state("Physics Wallah")
    assert state["deferred"] == ["public:second", "public:third"]

    _, resolved2 = _run_tg_scan(monkeypatch, channels, seeds=["@first", "@second", "@third"])
    assert resolved2[:2] == ["second", "third"]                     # deferred channels go first
