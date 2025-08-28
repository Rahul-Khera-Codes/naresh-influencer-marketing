from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any
from datetime import datetime
import requests
import os
from dotenv import load_dotenv
from math import log10
import re
import json
from db import searches_collection
from datetime import datetime
import time
import random
from functools import lru_cache

load_dotenv()

router = APIRouter()

RAPIDAPI_HOST = "instagram-best-experience.p.rapidapi.com"
RAPIDAPI_BASE = f"https://{RAPIDAPI_HOST}"
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")

OPENAI_KEY = os.getenv("OPENAI_KEY")






def estimate_price_from_followers(followers: int) -> dict:
    # simple pricing: base CPM increases with follower size
    if followers <= 0:
        followers = 0
    cpm = 5 + (log10(followers + 1) * 2)
    # price per post = (followers / 1000) * cpm
    price_per_post = max(10, (followers / 1000.0) * cpm * 10)
    return {"cpm": round(cpm, 2), "price_per_post": round(price_per_post, 2)}


def compute_match_score(query: str | None, influencer: dict) -> int:
    # rudimentary score based on keyword overlap
    score = 50
    if not query:
        return score
    q = query.lower()
    text = " ".join([str(influencer.get(k, "")) for k in ("username", "bio", "category", "location")]).lower()
    matches = sum(1 for token in q.split() if token and token in text)
    score += min(50, matches * 10)
    return max(0, min(100, score))





@router.get("/search/top")
def search_top_influencers(keyword: str, limit: int = 10, user_id: str | None = None):
    """
    Search top influencers by keyword using Mongo cache + RapidAPI.
    - Cache key: normalized keyword + limit
    - First tries exact normalized lookup (keyword lowercased + int limit).
    - If not found, tries case-insensitive lookup with same limit.
    - Stores normalized keyword + raw keyword + limit when saving.
    """
    if not keyword:
        raise HTTPException(status_code=400, detail="keyword is required")

    raw_keyword = keyword.strip()
    key = raw_keyword.lower()
    # include user_id in cache key when provided so cached results are user-scoped
    cache_query = {"keyword": key, "limit": int(limit)}
    if user_id:
        cache_query["user_id"] = str(user_id)

    # Try exact cached entry first -> return immediate if found
    if searches_collection is not None:
        try:
            cached = searches_collection.find_one(cache_query)
            if cached and "results" in cached:
                return {"results": cached["results"], "cached": True}
            # fallback: case-insensitive keyword match (same limit) — include user_id if present
            regex_q = {"keyword": {"$regex": f"^{re.escape(raw_keyword)}$", "$options": "i"}, "limit": int(limit)}
            if user_id:
                regex_q["user_id"] = str(user_id)
            cached = searches_collection.find_one(regex_q)
            if cached and "results" in cached:
                return {"results": cached["results"], "cached": True}
        except Exception as e:
            print("search cache lookup error:", e)

    # Fetch from RapidAPI when not cached
    url = "https://instagram-best-experience.p.rapidapi.com/users_search"
    headers = {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }
    params = {"query": raw_keyword, "count": limit}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15.0)
    except Exception as e:
        # If API fails, try to return any cached entry (ignore limit) before failing
        if searches_collection is not None:
            try:
                fallback = searches_collection.find_one({"keyword": {"$regex": f"^{re.escape(raw_keyword)}$", "$options": "i"}})
                if fallback and "results" in fallback:
                    return {"results": fallback["results"], "cached": True, "stale": True}
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=f"RapidAPI request error: {e}")

    if resp.status_code != 200:
        # try fallback cache before raising
        if searches_collection is not None:
            try:
                fallback = searches_collection.find_one({"keyword": {"$regex": f"^{re.escape(raw_keyword)}$", "$options": "i"}})
                if fallback and "results" in fallback:
                    return {"results": fallback["results"], "cached": True, "stale": True}
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=f"RapidAPI error: {resp.text}")

    try:
        data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Invalid JSON from RapidAPI: {e}")

    if isinstance(data, dict) and "users" in data:
        users_list = data["users"]
    elif isinstance(data, list):
        users_list = data
    else:
        users_list = []

    results = []
    for user in users_list[:limit]:
        if not isinstance(user, dict):
            continue

        pk = user.get("pk") or user.get("id")
        username = user.get("username")
        profile = {
            "pk": pk,
            "username": username,
            "full_name": user.get("full_name") or user.get("name"),
            "followers": user.get("follower_count"),
            "profile_pic": user.get("profile_pic_url"),
            "bio": user.get("biography") or user.get("bio", ""),
        }

        # Enrich per-user: try to fetch profile + insights (best-effort)
        try:
            prof = None
            try:
                prof = fetch_rapid_follower_profile(pk) if pk else None
            except Exception:
                prof = None

            insights = None
            try:
                insights = get_insights(user_id=pk) if pk else None
            except Exception:
                insights = None

            if insights:
                profile.update({
                    "post_count": insights.get("post_count"),
                    "avg_likes": insights.get("avg_likes"),
                    "engagement": insights.get("engagement"),
                    "engagement_rate_percent": insights.get("engagement_rate_percent"),
                    "followers": insights.get("followers") or profile.get("followers"),
                    "total_posts": insights.get("total_posts") or (prof.get("media_count") if prof else None),
                })
            else:
                if prof:
                    profile.update({
                        "followers": prof.get("follower_count") or profile.get("followers"),
                        "total_posts": prof.get("media_count"),
                        "post_count": None,
                        "avg_likes": None,
                        "engagement": None,
                        "engagement_rate_percent": None,
                    })
        except Exception as e:
            print(f"enrichment error for {username or pk}: {e}")

        results.append(profile)
        time.sleep(0.25)

    # Save to Mongo (best-effort) — store normalized keyword + raw + limit
    if searches_collection is not None:
        try:
            doc = {
                "keyword": key,
                "keyword_raw": raw_keyword,
                "limit": int(limit),
                "results": results,
                "created_at": datetime.utcnow(),
            }
            # attach user_id to stored doc when provided
            if user_id:
                doc["user_id"] = str(user_id)
            # use normalized cache_query to upsert so subsequent exact lookups succeed
            searches_collection.replace_one(cache_query, doc, upsert=True)
        except Exception as e:
            print("search cache write error:", e)

    return {"results": results, "cached": False}

class SummaryPayload(BaseModel):
    username: str
    bio: str | None = None
    # optional metrics to include in the prompt
    post_count: int | None = None
    avg_likes: int | None = None
    engagement: int | None = None
    engagement_rate_percent: float | None = None
    followers: int | None = None
    total_posts: int | None = None
    # profile fields
    user_id: str | None = None
    full_name: str | None = None
    follower_count: int | None = None
    media_count: int | None = None
    profile_pic_url: str | None = None

@router.post("/summary/ai")
def influencer_ai_summary(payload: SummaryPayload):
    """
    Generate a human-friendly AI summary for an influencer.
    Uses OpenAI chat completions. The prompt includes supplied metrics (if provided)
    so the model can produce a concise, simple, non-robotic summary and a short collaboration suggestion.
    """
    if not OPENAI_KEY:
        raise HTTPException(status_code=500, detail="No OpenAI API key configured")

    # system prompt: persona + style instructions (humanized, simple words, friendly)
    system_prompt = (
        "You are an expert influencer marketing analyst who writes short, human-friendly "
        "summaries for brand teams. Use simple, conversational language (not robotic or overly formal). "
        
        "Keep the summary concise (3-5 sentences). Highlight the creator's niche, audience size, "
        "typical engagement, and one quick suggestion for brand collaboration. If metrics are missing, "
        "make reasonable neutral statements (e.g., 'metrics not available')."
    )

    # Build user prompt with all available data (metrics first, then bio)
    parts = [f"Username: @{payload.username}"]
    if payload.full_name:
        parts.append(f"Full name: {payload.full_name}")
    if payload.user_id:
        parts.append(f"User ID: {payload.user_id}")
    # include both follower_count and followers if present
    if payload.follower_count is not None:
        parts.append(f"Follower count (profile): {payload.follower_count}")
    if payload.followers is not None:
        parts.append(f"Followers (enriched): {payload.followers}")
    if payload.media_count is not None:
        parts.append(f"Total posts (profile): {payload.media_count}")
    if payload.total_posts is not None:
        parts.append(f"Total posts (enriched): {payload.total_posts}")
    if payload.post_count is not None:
        parts.append(f"Recent post count used for metrics: {payload.post_count}")
    if payload.avg_likes is not None:
        parts.append(f"Average likes per post: {payload.avg_likes}")
    if payload.engagement is not None:
        parts.append(f"Total engagement (likes+comments): {payload.engagement}")
    if payload.engagement_rate_percent is not None:
        parts.append(f"Engagement rate: {payload.engagement_rate_percent}%")
    if payload.profile_pic_url:
        parts.append(f"Profile image: {payload.profile_pic_url}")
    parts.append(f"Bio: {payload.bio or 'N/A'}")

    user_prompt = "Here is the influencer data:\n\n" + "\n".join(parts) + (
        "\n\nPlease write a short, friendly, easy-to-read summary (3-5 sentences) suitable for a brand "
        "brief. End with one succinct suggestion for a potential brand collaboration or content idea."
    )

    body = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 500,
    }

    try:
        headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=30.0)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenAI request error: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"OpenAI error: {resp.text}")

    data = resp.json()
    text_out = ""
    try:
        text_out = data.get("choices", [])[0].get("message", {}).get("content", "").strip()
    except Exception:
        text_out = ""

    if not text_out:
        raise HTTPException(status_code=502, detail="Failed to generate summary")

    return {"username": payload.username, "summary": text_out}

def fetch_rapid_user_metrics(username: str) -> dict:
    """
    Fetch detailed metrics for a single username from RapidAPI.
    Returns a dict with keys: username, followers, avg_likes, engagement, profile_pic, full_name, bio
    """
    url = f"{RAPIDAPI_BASE}/v1/user/by/username"
    headers = {
        "X-RapidAPI-Host": RAPIDAPI_HOST,
        "X-RapidAPI-Key": RAPIDAPI_KEY,
    }
    params = {"username": username}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15.0)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RapidAPI request error: {e}")

    if resp.status_code != 200:
        # try to parse JSON error, otherwise return a safe empty structure
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        raise HTTPException(status_code=502, detail=f"RapidAPI error: {err}")

    try:
        data = resp.json()
        # print("response data", data)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Invalid JSON from RapidAPI: {e}")

    # Normalize fields (keys differ across APIs)
    followers = data.get("follower_count") or data.get("followers") or data.get("followerCount") or data.get("followers_count")
    avg_likes = data.get("avg_likes") or data.get("average_likes") or data.get("avgLikes") or data.get("avg_like")
    # try derive avg_comments from common keys
    avg_comments = (
        data.get("avg_comments")
        or data.get("average_comments")
        or data.get("comments_avg")
        or data.get("avg_comment")
        or (data.get("comments") if isinstance(data.get("comments"), (int, float, str)) else None)
    )
    profile_pic = data.get("profile_pic_url") or data.get("profile_picture") or data.get("profile_pic") or data.get("avatar")
    full_name = data.get("full_name") or data.get("name") or data.get("fullName")
    bio = data.get("biography") or data.get("bio") or ""

    # try common posts/media count
    posts = data.get("media_count") or data.get("posts") or data.get("media") and None

    # compute engagement metrics
    computed = compute_engagement_metrics(followers=followers, avg_likes=avg_likes, avg_comments=avg_comments, posts=posts)

    return {
        # include raw for debugging if needed
        "raw": data,
    }

@router.get("/profile")
def get_influencer_profile(username: str):
    """
    Return enriched profile metrics for a single username.
    """
    if not username:
        raise HTTPException(status_code=400, detail="username is required")
    try:
        metrics = fetch_rapid_user_metrics(username)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return metrics



def _parse_count(value):
    """
    Parse strings like "1.2M", "3,400" or numeric values into an int.
    Returns int or None.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if not s:
        return None
    s = s.replace(",", "")
    m = re.match(r"^([\d\.]+)\s*([kKmMbB]?)$", s)
    if m:
        num = float(m.group(1))
        suf = m.group(2).lower()
        if suf == "k":
            num *= 1_000
        elif suf == "m":
            num *= 1_000_000
        elif suf == "b":
            num *= 1_000_000_000
        return int(num)
    try:
        return int(float(s))
    except Exception:
        return None


def compute_engagement_metrics(followers=None, avg_likes=None, avg_comments=None, posts=None) -> dict:
    """
    Compute engagement totals and engagement rate percent.
    - followers, avg_likes, avg_comments may be numeric or strings like "1.2K".
    - Returns {"engagement": int, "engagement_rate_percent": float | None}
    """
    f = _parse_count(followers)
    likes = _parse_count(avg_likes)
    comments = _parse_count(avg_comments)

    likes = likes or 0
    comments = comments or 0
    total_engagement = likes + comments

    eng_rate = None
    if f and f > 0:
        eng_rate = round((total_engagement / f) * 100, 3)  # percent with 3 decimals

    return {"engagement": int(total_engagement), "engagement_rate_percent": eng_rate}





@router.get("/insights")
def user_insights(username: str | None = None, media_id: str | None = None, user_id: str | None = None):
    """
    Client endpoint. Accepts:
      - ?user_id=... -> fetch aggregated feed metrics for that user (preferred)
      - ?username=... -> will try to resolve user_id from profile (may be slower)
      - media_id is ignored in this aggregated endpoint (feed aggregation)
    Examples:
      GET /influencers/insights?user_id=13460080
      GET /influencers/insights?username=_the_foodigram001
    """
    print(f"[DEBUG] /influencers/insights called with user_id={user_id}, username={username}, media_id={media_id}")
    try:
        metrics = get_insights(username=username, media_id=media_id, user_id=user_id)
        print(f"[DEBUG] /influencers/insights result for user_id={user_id}, username={username}: {metrics}")
    except HTTPException as e:
        print(f"[DEBUG] /influencers/insights HTTPException for user_id={user_id}, username={username}: {e.detail}")
        raise
    except Exception as e:
        print(f"[DEBUG] /influencers/insights error for user_id={user_id}, username={username}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return metrics

def get_insights(username: str = None, media_id: str | None = None, user_id: str | None = None) -> dict:
    """
    Fetch aggregated feed insights for a user (uses user_id / pk).
    Only fetches last 20 posts to avoid rate limits.
    Returns: avg_likes, engagement, engagement_rate_percent, post_count.
    """
    print(f"[DEBUG] get_insights called with user_id={user_id}, username={username}, media_id={media_id}")
    # resolve user_id if only username is provided
    if not user_id and username:
        try:
            profile = fetch_rapid_user_metrics(username)
            user_id = profile.get("pk") or profile.get("id") or (profile.get("raw") or {}).get("pk") or (profile.get("raw") or {}).get("id")
            print(f"[DEBUG] get_insights resolved user_id={user_id} from username={username}")
        except Exception as e:
            print(f"[DEBUG] get_insights failed to resolve user_id from username={username}: {e}")
            user_id = None

    if not user_id:
        print(f"[DEBUG] get_insights missing user_id for username={username}")
        raise HTTPException(status_code=400, detail="user_id (pk) is required to fetch feed insights.")

    headers = {
        "x-rapidapi-host": RAPIDAPI_HOST,
        "x-rapidapi-key": RAPIDAPI_KEY,
    }
    feed_url = f"https://{RAPIDAPI_HOST}/feed"
    params = {"user_id": str(user_id), "count": 20}   # ✅ only last 20 posts

    def fetch_and_parse():
        try:
            print(f"[DEBUG] get_insights requesting feed: {feed_url} params={params}")
            resp = requests.get(feed_url, headers=headers, params=params, timeout=20.0)
            time.sleep(0.5)  # Add delay after feed request
        except Exception as e:
            print(f"[DEBUG] get_insights RapidAPI request error (feed): {e}")
            raise HTTPException(status_code=502, detail=f"RapidAPI request error (feed): {e}")

        if resp.status_code != 200:
            try:
                err = resp.json()
            except Exception:
                err = resp.text
            print(f"[DEBUG] get_insights RapidAPI error (feed): {err}")
            raise HTTPException(status_code=502, detail=f"RapidAPI error (feed): {err}")

        data = resp.json()
        print(f"[DEBUG] get_insights feed data received: {str(data)[:300]}...")  # Print first 300 chars

        items = []
        if isinstance(data, dict):
            if "items" in data:
                items = data["items"]
            elif "media" in data:
                items = data["media"]
            elif "data" in data:
                items = data["data"]

        total_likes = 0
        total_comments = 0
        post_count = 0

        for it in items:
            if not isinstance(it, dict):
                continue
            likes = int(it.get("like_count", 0))
            comments = int(it.get("comment_count", 0))
            total_likes += likes
            total_comments += comments
            post_count += 1

        avg_likes = int(total_likes / post_count) if post_count else 0
        engagement = total_likes + total_comments

        # ---- Fetch followers count ----
        followers = None
        media_count = None
        try:
            profile_data = fetch_rapid_follower_profile(user_id)
            followers = profile_data.get("follower_count")
            media_count = profile_data.get("media_count")
            print(f"[DEBUG] get_insights fetched profile_data for user_id={user_id}: {profile_data}")
        except Exception as e:
            print(f"[DEBUG] get_insights failed to fetch profile_data for user_id={user_id}: {e}")
            followers = None
            media_count = None

        # engagement rate
        engagement_rate = None
        if followers and followers > 0:
            engagement_rate = round((engagement / followers) * 100, 2)

        result = {
            "post_count": post_count,
            "avg_likes": avg_likes,
            "engagement": engagement,
            "engagement_rate_percent": engagement_rate,
            "followers": followers,
            "total_posts": media_count
        }
        print(f"[DEBUG] get_insights result for user_id={user_id}: {result}")
        return result

    # First attempt
    result = fetch_and_parse()

    # Fallback: If followers or engagement_rate_percent is None, retry once after 2s delay
    if result.get("followers") is None or result.get("engagement_rate_percent") is None:
        print(f"[DEBUG] get_insights missing followers or engagement_rate_percent, retrying after 2s...")
        time.sleep(2)
        result = fetch_and_parse()

    return result
def fetch_rapid_follower_profile(user_id: str) -> dict:
    """
    Fetch profile info from RapidAPI /profile endpoint.
    Returns {follower_count, media_count, username, full_name, ...}
    """
    print(f"[DEBUG] fetch_rapid_follower_profile called with user_id={user_id}")
    if not RAPIDAPI_KEY:
        print(f"[DEBUG] fetch_rapid_follower_profile missing RAPIDAPI_KEY")
        raise HTTPException(status_code=500, detail="No RAPIDAPI_KEY configured")

    url = "https://instagram-best-experience.p.rapidapi.com/profile"
    headers = {
        "x-rapidapi-key": RAPIDAPI_KEY,
        "x-rapidapi-host": RAPIDAPI_HOST,
    }
    params = {"user_id": str(user_id)}

    try:
        print(f"[DEBUG] fetch_rapid_follower_profile requesting: {url} params={params}")
        resp = requests.get(url, headers=headers, params=params, timeout=20.0)
        time.sleep(2)  # <-- Add a 2 second delay after the API call
    except Exception as e:
        print(f"[DEBUG] fetch_rapid_follower_profile RapidAPI request error: {e}")
        raise HTTPException(status_code=502, detail=f"RapidAPI request error (profile): {e}")

    if resp.status_code != 200:
        try:
            err = resp.json()
        except Exception:
            err = resp.text
        print(f"[DEBUG] fetch_rapid_follower_profile RapidAPI error: {err}")
        raise HTTPException(status_code=502, detail=f"RapidAPI error (profile): {err}")

    try:
        data = resp.json()
        print(f"[DEBUG] fetch_rapid_follower_profile data received: {data}")
    except Exception as e:
        print(f"[DEBUG] fetch_rapid_follower_profile Invalid JSON: {e}")
        raise HTTPException(status_code=502, detail=f"Invalid JSON from RapidAPI (profile): {e}")

    return {
        "user_id": data.get("pk"),
        "username": data.get("username"),
        "full_name": data.get("full_name"),
        "follower_count": data.get("follower_count"),
        "media_count": data.get("media_count"),
        "profile_pic_url": data.get("profile_pic_url"),
        "bio": data.get("biography"),
    }
    
    
@router.get("/followers")
def get_followers(user_id: str, next_max_id: str | None = None):
    """
    GET /influencers/followers?user_id=12345[&next_max_id=...]
    Returns RapidAPI followers page for the given user_id.
    """
    return fetch_rapid_follower_profile(user_id=user_id)


@router.get("/fetch_rapid_followers")
def get_rapid_followers(user_id: str, next_max_id: str | None = None):
    """
    GET /influencers/fetch_rapid_followers?user_id=12345[&next_max_id=...]
    Returns RapidAPI followers data for the given user_id.
    This endpoint is specifically designed for frontend integration.
    """
    try:
        followers_data = fetch_rapid_follower_profile(user_id=user_id)
        return followers_data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class SummaryRequest(BaseModel):
    username: str
    bio: str | None = None
    # metrics (optional but recommended for deep analysis)
    post_count: int | None = None
    avg_likes: int | None = None
    engagement: int | None = None
    engagement_rate_percent: float | None = None
    followers: int | None = None
    total_posts: int | None = None
    user_id: str | None = None
    full_name: str | None = None
    follower_count: int | None = None
    media_count: int | None = None
    profile_pic_url: str | None = None

@router.post("/summary")
def generate_summary(request: SummaryRequest):
    """
    Generates an in-depth (2-3 page) human-friendly analysis of an influencer.
    Uses provided metrics (if available) to analyze engagement, reach and recommend
    campaign ideas, pricing guidance and next steps.
    """
    if not OPENAI_KEY:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured")

    # System prompt instructs style, structure and desired length (2-3 pages)
    system_prompt = (
        "You are a senior influencer marketing analyst and copywriter. Produce a detailed, "
        "human-friendly report approximately 2-3 paragraphs long (aim for ~400-600 words) based on the "
        "profile and metrics provided. Use simple, conversational language (not robotic). Organize the "
        "output with clear headings and subsections. Sections must include: Executive Summary, Audience & Reach, "
        "Engagement Analysis (use provided metrics and explain what they mean), Content Strategy & Strengths, "
        "Weaknesses & Risks, Recommended Collaboration Types & Creative Angles, Pricing Guidance (estimate), "
        "Actionable Next Steps, and an Appendix with raw metrics. When metrics are missing, explicitly note that and "
        "qualify recommendations. End with 5 concise bullet-point next steps for a brand. Be practical and tactical."
    )

    # Build a detailed user prompt including all available metrics
    parts = [f"Username: @{request.username}"]
    if request.full_name:
        parts.append(f"Full name: {request.full_name}")
    if request.user_id:
        parts.append(f"User ID: {request.user_id}")
    if request.follower_count is not None:
        parts.append(f"Follower count (profile): {request.follower_count}")
    if request.followers is not None:
        parts.append(f"Followers (enriched): {request.followers}")
    if request.media_count is not None:
        parts.append(f"Total posts (profile): {request.media_count}")
    if request.total_posts is not None:
        parts.append(f"Total posts (enriched): {request.total_posts}")
    if request.post_count is not None:
        parts.append(f"Recent posts considered: {request.post_count}")
    if request.avg_likes is not None:
        parts.append(f"Average likes per post: {request.avg_likes}")
    if request.engagement is not None:
        parts.append(f"Total engagement (likes+comments over sample): {request.engagement}")
    if request.engagement_rate_percent is not None:
        parts.append(f"Engagement rate (%): {request.engagement_rate_percent}")
    if request.profile_pic_url:
        parts.append(f"Profile image URL: {request.profile_pic_url}")
    parts.append(f"Bio: {request.bio or 'N/A'}")

    user_prompt = (
        "Please analyze the influencer using the data below and produce the requested 2-3 page report.\n\n"
        + "\n".join(parts)
        + "\n\nDeliverable notes: Use the metrics to calculate and interpret engagement quality, "
        "audience relevance, and likely content performance. Provide realistic collaboration ideas and "
        "give a pricing range (low/typical/high) with rationale. Keep tone friendly, actionable and easy to read."
    )

    body = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 2000,  # allows for long output (adjust if using a different model/token limits)
    }

    try:
        headers = {"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"}
        resp = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json=body, timeout=60.0)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OpenAI request error: {e}")

    if resp.status_code != 200:
        # return helpful debugging info while avoiding leaking keys
        raise HTTPException(status_code=502, detail=f"OpenAI API error: {resp.text}")

    data = resp.json()
    summary = ""
    try:
        summary = data.get("choices", [])[0].get("message", {}).get("content", "").strip()
    except Exception:
        summary = ""

    if not summary:
        raise HTTPException(status_code=502, detail="Failed to generate summary")

    return {"username": request.username, "summary": summary}
