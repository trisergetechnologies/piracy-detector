"""Matching and scoring logic: titles, heuristics, transcripts, fingerprints, tiers."""

import random

from core.catalog import catalog_derived_queries, normalize_title, title_tokens
from core.fingerprint import fingerprint_similarity
from core.fingerprint_match import match_against_catalog
from core.heuristics import annotate_and_prioritize, piracy_heuristic_score
from core.scorer import compute_final_score, piracy_evidence
from core.search_engine import expand_piracy_queries, name_variants
from core.transcript_match import build_sketch, compare_sketches, parse_json3, parse_vtt, tier_for
from core.youtube_client import parse_channel_ref, parse_playlist_ref

OFFICIAL_ID = "off00000001"


def _catalog():
    title = "Thermodynamics First Law Explained with Numericals"
    return {
        "video_ids": {OFFICIAL_ID},
        "channel_ids": {"UC" + "o" * 22},
        "entries": [{"video_id": OFFICIAL_ID, "title": title}],
        "title_index": [{
            "video_id": OFFICIAL_ID, "title": title,
            "norm_title": normalize_title(title), "tokens": title_tokens(title),
        }],
        "duration_map": {OFFICIAL_ID: 3605},
    }


# ---------------------------------------------------------------- references


def test_channel_refs_accept_handles_and_urls():
    uc = "UC" + "a" * 22
    assert parse_channel_ref(uc) == ("id", uc)
    assert parse_channel_ref(f"https://www.youtube.com/channel/{uc}/videos") == ("id", uc)
    assert parse_channel_ref("@PhysicsWallah") == ("handle", "PhysicsWallah")
    assert parse_channel_ref("https://www.youtube.com/@vanisoodofficial") == ("handle", "vanisoodofficial")
    assert parse_channel_ref("DrVanisNEETExcellence") == ("name", "DrVanisNEETExcellence")
    assert parse_playlist_ref("https://www.youtube.com/playlist?list=PLabcdefghijkl") == "PLabcdefghijkl"


def test_hindi_titles_survive_normalisation():
    assert "ऊष्मागतिकी" in normalize_title("ऊष्मागतिकी | Lecture 3")
    assert title_tokens("ऊष्मागतिकी का प्रथम नियम")


def test_catalog_queries_cover_whole_catalog():
    index = [{"title": f"Chapter {i} Organic Reactions Mechanism Number{i}",
              "norm_title": normalize_title(f"Chapter {i} Organic Reactions Mechanism Number{i}")}
             for i in range(200)]
    queries = " ".join(catalog_derived_queries(index, "Brand", max_queries=75))
    assert "number0" in queries.lower()
    assert "number190" in queries.lower() or "number192" in queries.lower()  # not just the first titles


def test_query_expansion_has_hinglish_and_misspellings():
    variants = name_variants("Physics Wallah")
    assert "physicswallah" in variants
    assert any(v != "physics wallah" and " " in v for v in variants)  # phonetic misspelling
    queries = expand_piracy_queries("Physics Wallah")
    assert any("free me" in q for q in queries)
    assert any("फ्री" in q for q in queries)


# ---------------------------------------------------------------- heuristics


def _video(**kw):
    base = {
        "video_id": "cand0000001", "channel_id": "UC" + "p" * 22,
        "title": "Thermodynamics First Law Explained with Numericals",
        "description": "", "duration_seconds": 3604,
    }
    base.update(kw)
    return base


def test_heuristics_ignore_popularity():
    cat = _catalog()
    low = piracy_heuristic_score(_video(view_count=12), cat, entity_name="Brand")
    high = piracy_heuristic_score(_video(view_count=5_000_000), cat, entity_name="Brand")
    assert low["heuristic_score"] == high["heuristic_score"]
    assert low["priority"] == high["priority"]


def test_title_and_duration_match_are_signals():
    h = piracy_heuristic_score(_video(), _catalog(), entity_name="Brand")
    assert any(s.startswith("catalog_title_match") for s in h["signals"])
    assert "duration_match_official" in h["signals"]


def test_retitled_copy_gets_duration_signal():
    h = piracy_heuristic_score(_video(title="Lec 4 full"), _catalog())
    assert f"duration_equals_official:{OFFICIAL_ID}" in h["signals"]


def test_ranking_puts_evidence_first_and_drops_official():
    cat = _catalog()
    vids = [
        _video(video_id="noise000001", title="Funny cat video", duration_seconds=40, view_count=9_000_000),
        _video(video_id=OFFICIAL_ID),
        _video(video_id="copy0000001", view_count=3),
    ]
    ranked = annotate_and_prioritize(vids, catalog=cat, entity_name="Brand")
    assert [v["video_id"] for v in ranked] == ["copy0000001", "noise000001"]


# ---------------------------------------------------------------- transcripts


def _speech(seed: int, n: int = 1500):
    rnd = random.Random(seed)
    vocab = [f"w{i}" for i in range(400)]
    return [vocab[rnd.randrange(400)] for _ in range(n)]


def _segments(words, start=0.0):
    return [(start + i * 2.0, " ".join(words[i * 8:(i + 1) * 8])) for i in range(len(words) // 8)]


def test_transcript_copy_with_new_intro_matches():
    words = _speech(1)
    official = build_sketch(_segments(words), "hi")
    copy = build_sketch(_segments(_speech(99, 80) + words, start=5.0), "hi")
    res = compare_sketches(copy, official)
    assert res["overlap"] > 0.9
    assert tier_for(res["overlap"], res["shared"]) == "confirmed"


def test_transcript_partial_clip_matches():
    words = _speech(2)
    official = build_sketch(_segments(words), "hi")
    clip = build_sketch(_segments(words[500:900]), "hi")
    res = compare_sketches(clip, official)
    assert res["overlap"] > 0.9
    assert res["official_span"][0] > 100  # clip located inside the official video


def test_transcript_with_asr_noise_still_matches():
    words = _speech(3)
    noisy = list(words)
    rnd = random.Random(7)
    for i in range(0, len(noisy), 12):  # ~8% of words changed
        noisy[i] = f"x{rnd.randrange(999)}"
    res = compare_sketches(build_sketch(_segments(noisy)), build_sketch(_segments(words)))
    assert tier_for(res["overlap"], res["shared"]) in ("confirmed", "likely")


def test_transcript_different_lecture_does_not_match():
    res = compare_sketches(build_sketch(_segments(_speech(4))), build_sketch(_segments(_speech(5))))
    assert res["overlap"] < 0.05
    assert tier_for(res["overlap"], res["shared"]) is None


def test_caption_parsers():
    j3 = '{"events":[{"tStartMs":1500,"segs":[{"utf8":"नमस्ते "},{"utf8":"students"}]},{"tStartMs":2000}]}'
    assert parse_json3(j3) == [(1.5, "नमस्ते students")]
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<c>hello</c> world\n\n00:00:03.000 --> 00:00:05.000\nhello world\nnext line\n"
    assert parse_vtt(vtt) == [(1.0, "hello world"), (3.0, "next line")]


# ---------------------------------------------------------------- audio fingerprints


def _fp(seed: int, n: int = 700):
    rnd = random.Random(seed)
    return [rnd.getrandbits(32) for _ in range(n)]


def test_fingerprint_equal_length_shifted_copy_matches():
    """Two 90 s samples where the copy starts 15 s later (the old code scored this ~0.5)."""
    a = _fp(1, 900)
    official, copy = a[:700], a[120:820]
    assert fingerprint_similarity(official, copy) > 0.95


def test_fingerprint_survives_bit_noise_and_rejects_other_audio():
    a = _fp(2)
    rnd = random.Random(3)
    noisy = [v ^ (1 << rnd.randrange(32)) ^ (1 << rnd.randrange(32)) for v in a]  # ~6% bits flipped
    assert fingerprint_similarity(a, noisy) > 0.9
    assert fingerprint_similarity(a, _fp(4)) < 0.6


def test_spectral_backend_is_never_confirmed():
    vals = [v & 0x7FFF for v in _fp(5)]
    index = [{"video_id": OFFICIAL_ID, "title": "t", "fingerprint": {"backend": "spectral", "values": vals}}]
    m = match_against_catalog({"backend": "spectral", "values": vals}, index)
    assert m["is_match"] and m["tier"] == "likely" and not m["is_strong"]
    index[0]["fingerprint"]["backend"] = "chromaprint"
    m2 = match_against_catalog({"backend": "chromaprint", "values": _fp(6)}, [
        {"video_id": OFFICIAL_ID, "title": "t", "fingerprint": {"backend": "chromaprint", "values": _fp(6)}}])
    assert m2["tier"] == "confirmed"


# ---------------------------------------------------------------- tiers


LLM_DOWN = {"classification": "LEGITIMATE", "confidence": 0, "severity": "low",
            "reason": "LLM unavailable: 429", "llm_error": True}
LLM_LEGIT = {"classification": "LEGITIMATE", "confidence": 90, "severity": "low", "reason": "ok"}
LLM_PIRATED = {"classification": "PIRATED", "confidence": 90, "severity": "high", "reason": "telegram link"}


def _annotated(**kw):
    v = _video(**kw)
    v["heuristic"] = piracy_heuristic_score(v, _catalog(), entity_name="Brand")
    return v


def test_content_match_is_confirmed_even_when_llm_is_down():
    v = _annotated(title="random words here", duration_seconds=900)
    v["fingerprint_match"] = {"is_match": True, "is_strong": True, "tier": "confirmed",
                              "backend": "transcript", "similarity": 0.83, "official_title": "T"}
    ev = piracy_evidence(v, LLM_DOWN)
    assert ev["tier"] == "confirmed" and ev["confidence"] >= 90


def test_title_plus_duration_is_likely_without_llm():
    ev = piracy_evidence(_annotated(), LLM_DOWN)
    assert ev["tier"] == "likely" and 70 <= ev["confidence"] <= 89


def test_llm_only_is_weak_and_capped():
    v = _annotated(title="Brand batch free", duration_seconds=100)
    ev = piracy_evidence(v, LLM_PIRATED)
    assert ev["tier"] == "weak" and ev["confidence"] <= 69


def test_no_evidence_and_llm_says_legit_is_not_flagged():
    v = _annotated(title="Brand interview podcast", duration_seconds=100)
    assert piracy_evidence(v, LLM_LEGIT)["tier"] is None


def test_final_score_reports_tier_and_ignores_popularity():
    def run(views, subs):
        v = _annotated()
        detail = {"snippet": {"title": v["title"]}, "statistics": {"viewCount": str(views)},
                  "contentDetails": {"duration": "PT1H4S"}}
        channel = {"statistics": {"subscriberCount": str(subs), "videoCount": "40"},
                   "snippet": {"title": "Some Channel", "description": "x" * 80}}
        return compute_final_score(LLM_DOWN, {"is_negative_thumbnail": False, "confidence": 0},
                                   detail, channel, enriched_video=v), v

    small, v = run(12, 40)
    big, _ = run(4_000_000, 2_000_000)
    assert small["classification"] == big["classification"] == "PIRATED"
    assert small["reason"].startswith("LIKELY:")
    assert small["confidence"] == big["confidence"]
    assert small["final_score"] == big["final_score"]
    assert "tier:likely" in v["heuristic"]["signals"]
