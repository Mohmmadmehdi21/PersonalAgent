"""Deterministic clip gates shared by preparation and review reporting."""
import math


def assess_candidate(clip, duration, transcript, *, hook_min_score=7, rights_status="unverified"):
    """Return a traceable pending/needs_review/rejected decision without using model claims as proof."""
    def reject(reason):
        return {"status": "rejected", "reason": reason, "transcript_words": 0}

    if not isinstance(clip, dict):
        return reject("candidate is not a JSON object")
    try:
        start, end = float(clip.get("start")), float(clip.get("end"))
        total = float(duration)
        raw_score = clip.get("hook_score")
        score = int(raw_score)
        if isinstance(raw_score, bool) or float(raw_score) != score:
            return reject("hook score must be an integer from 1 to 10")
    except (TypeError, ValueError, OverflowError):
        return reject("missing or invalid timestamps or hook score")
    if not all(math.isfinite(value) for value in (start, end, total)):
        return reject("timestamps or source duration are not finite numbers")
    if start < 0 or end <= start or end > total + 0.25:
        return reject("clip timestamps fall outside the source video")
    length = end - start
    if not 25 <= length <= 60:
        return reject("clip duration must be between 25 and 60 seconds")
    if not 1 <= score <= 10:
        return reject("hook score must be from 1 to 10")
    if score < hook_min_score:
        return reject(f"hook score {score} is below configured minimum {hook_min_score}")
    title = str(clip.get("title", "")).strip()
    commentary = str(clip.get("commentary_script", "")).strip()
    if not title:
        return reject("candidate has no title")
    if len(commentary.split()) < 15:
        return reject("commentary has fewer than 15 words")

    transcript_words = 0
    for segment in transcript or []:
        try:
            if float(segment.get("end", 0)) > start and float(segment.get("start", 0)) < end:
                transcript_words += len(str(segment.get("text", "")).split())
        except (AttributeError, TypeError, ValueError):
            continue
    review_reasons = []
    if transcript_words < 24:
        review_reasons.append(f"sparse transcript coverage ({transcript_words} words in selected window)")
    if score == hook_min_score:
        review_reasons.append("hook score is exactly at the configured minimum")
    if len(commentary.split()) < 20:
        review_reasons.append("commentary is close to the minimum length")
    analysis_status = str(clip.get("analysis_status", "legacy_cache_unverified"))
    if analysis_status != "ai_success":
        review_reasons.append(f"clip analysis came from {analysis_status}, not a successful AI response")
    if rights_status == "unverified":
        review_reasons.append("reuse permission/license is not documented in source metadata")
    elif rights_status == "permission_note_provided":
        review_reasons.append("source permission note is declared but not independently verified")

    return {
        "status": "needs_review" if review_reasons else "pending",
        "reason": "; ".join(review_reasons) if review_reasons else "passed duration, transcript, commentary, hook and rights checks",
        "transcript_words": transcript_words,
    }


def rights_status(*, is_own=False, permission_note="", source_license=""):
    if is_own:
        return "self_owned"
    if str(permission_note or "").strip():
        return "permission_note_provided"
    license_text = str(source_license or "").lower()
    if "creative commons" in license_text or "creativecommon" in license_text or "cc-by" in license_text or "cc by" in license_text:
        return "creative_commons_metadata"
    return "unverified"
