import os
import httpx
import urllib.parse
import secrets
import asyncio

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select, case
from collections import defaultdict, Counter, namedtuple

from database import init_db, AsyncSessionLocal
from models import TrackPlay, ArtistCache, ArtistSpotifyID, LikedSong
from tracker import (
    current_track_state, update_state, is_new_track,
    commit_previous_track, get_or_fetch_genre, was_skipped,
    TIMEZONE
)

load_dotenv()

SPOTIFY_CLIENT_ID     = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
REDIRECT_URI          = "http://127.0.0.1:8000/auth-redirect"

SPOTIFY_AUTH_URL        = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL       = "https://accounts.spotify.com/api/token"
SPOTIFY_NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"

SCOPES = "user-read-currently-playing user-read-playback-state user-read-recently-played user-top-read user-follow-read"


token_store    = {}
poller_running = False

import json as _json
TOKEN_FILE = "token_store.json"

def save_token_store():
    try:
        with open(TOKEN_FILE, "w") as f:
            _json.dump(token_store, f)
    except Exception as e:
        print(f"[TOKEN] Failed to save: {e}")

def load_token_store():
    try:
        if os.path.exists(TOKEN_FILE):
            with open(TOKEN_FILE, "r") as f:
                data = _json.load(f)
                token_store.update(data)
                print(f"[TOKEN] Loaded saved tokens")
    except Exception as e:
        print(f"[TOKEN] Failed to load: {e}")


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def refresh_access_token():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": token_store.get("refresh_token"),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        )
    token_data = response.json()
    if "access_token" in token_data:
        token_store["access_token"] = token_data["access_token"]
        save_token_store()  # ← save after refresh
        return True
    return False


async def poll_spotify(app):
    """Background task that polls Spotify every 5 seconds and logs track changes."""
    print(f"[POLLER] Instance started — PID: {os.getpid()}")
    await asyncio.sleep(5)
    while True:
        try:
            if "access_token" in token_store:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        SPOTIFY_NOW_PLAYING_URL,
                        headers={"Authorization": f"Bearer {token_store['access_token']}"},
                    )

                if response.status_code == 401:
                    await refresh_access_token()

                elif response.status_code == 200:
                    data = response.json()
                    item = data.get("item")
                    if item:
                        artist_id = item["artists"][0]["id"] if item["artists"] else None
                        progress  = data.get("progress_ms", 0)
                        duration  = item.get("duration_ms", 1)

                        print(f"[POLLER] Playing: {item['name']}")

                        track = {
                            "track_id":      item["id"],
                            "track_name":    item["name"],
                            "artists":       ", ".join(a["name"] for a in item["artists"]),
                            "album":         item["album"]["name"],
                            "album_art_url": item["album"]["images"][0]["url"] if item["album"]["images"] else None,
                            "is_playing":    data.get("is_playing", False),
                            "progress_ms":   progress,
                            "duration_ms":   duration,
                            "artist_id":     artist_id,
                        }

                        if is_new_track(track):
                            print(f"[POLLER] NEW TRACK — committing: {current_track_state.get('track_name')}")

                            # Commit previous track first
                            try:
                                async with AsyncSessionLocal() as db:
                                    await commit_previous_track(db)
                                skipped = was_skipped(
                                    current_track_state.get("progress_ms", 0),
                                    current_track_state.get("duration_ms", 1)
                                )
                                print(f"[POLLER] Committed: {current_track_state.get('track_name')} | skipped: {skipped}")
                            except Exception as e:
                                print(f"[POLLER] Commit error: {e}")

                            # Update state to new track
                            update_state(track)
                            print(f"[POLLER] State updated to: {track['track_name']}")

                            # Store Spotify IDs for ALL artists in track
                            try:
                                async with AsyncSessionLocal() as db:
                                    all_track_artist_ids = {
                                        a["name"]: a["id"] for a in item["artists"]
                                    }
                                    for aname, aid in all_track_artist_ids.items():
                                        existing_q = select(ArtistSpotifyID).where(
                                            ArtistSpotifyID.artist_name == aname
                                        )
                                        existing = (await db.execute(existing_q)).scalar_one_or_none()
                                        if not existing:
                                            db.add(ArtistSpotifyID(
                                                artist_name = aname,
                                                spotify_id  = aid,
                                            ))
                                    await db.commit()
                            except Exception as e:
                                print(f"[POLLER] Artist ID store error: {e}")

                            # Fetch and store genre for new track
                            if artist_id:
                                try:
                                    async with AsyncSessionLocal() as db:
                                        genre = await get_or_fetch_genre(
                                            track["artists"].split(",")[0].strip(),
                                            artist_id,
                                            db,
                                            token_store["access_token"],
                                            track_name=track["track_name"],
                                        )
                                        current_track_state["primary_genre"] = genre
                                        print(f"[POLLER] Genre stored: {genre}")
                                except Exception as e:
                                    print(f"[POLLER] Genre error: {e}")

                        else:
                            current_track_state["progress_ms"] = track["progress_ms"]

        except Exception as e:
            print(f"[POLLER] Error: {e}")

        await asyncio.sleep(5)




@asynccontextmanager
async def lifespan(app: FastAPI):
    global poller_running
    await init_db()
    load_token_store()  # ← load saved tokens
    if not poller_running:
        poller_running = True
        asyncio.create_task(poll_spotify(app))
        print("[POLLER] Started")
    else:
        print("[POLLER] Already running, skipping")
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")

ArtistRow     = namedtuple("ArtistRow",     ["artists", "plays"])
ArtistSkipRow = namedtuple("ArtistSkipRow", ["artists", "skips"])
ArtistRateRow = namedtuple("ArtistRateRow", ["artists", "plays", "skips", "skip_rate"])


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    is_authenticated = "access_token" in token_store
    return templates.TemplateResponse("index.html", {
        "request":          request,
        "is_authenticated": is_authenticated,
    })


@app.get("/login")
async def login():
    state = secrets.token_urlsafe(16)
    token_store["oauth_state"] = state
    params = {
        "client_id":     SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  REDIRECT_URI,
        "scope":         SCOPES,
        "state":         state,
    }
    auth_url = f"{SPOTIFY_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return RedirectResponse(auth_url)


@app.get("/auth-redirect")
async def auth_redirect(request: Request, code: str = None, state: str = None, error: str = None):
    if error:
        return HTMLResponse(f"<h2>Auth error: {error}</h2>")

    stored_state = token_store.get("oauth_state")
    if state != stored_state:
        return HTMLResponse("<h2>State mismatch.</h2>", status_code=400)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            SPOTIFY_TOKEN_URL,
            data={
                "grant_type":   "authorization_code",
                "code":         code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        )

    token_data = response.json()
    if "access_token" not in token_data:
        return HTMLResponse(f"<h2>Token error: {token_data}</h2>", status_code=400)

    token_store["access_token"]  = token_data["access_token"]
    token_store["refresh_token"] = token_data.get("refresh_token")
    save_token_store()  # ← save after login
    return RedirectResponse("/")


@app.get("/logout")
async def logout():
    token_store.clear()
    save_token_store()  # ← clear saved tokens on logout
    return RedirectResponse("/")


@app.get("/now-playing", response_class=HTMLResponse)
async def now_playing(request: Request):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            SPOTIFY_NOW_PLAYING_URL,
            headers={"Authorization": f"Bearer {token_store['access_token']}"},
        )

    if response.status_code == 401:
        refreshed = await refresh_access_token()
        if refreshed:
            return RedirectResponse("/now-playing")
        token_store.clear()
        return RedirectResponse("/")

    track = None
    if response.status_code == 200:
        data = response.json()
        item = data.get("item")
        if item:
            track = {
                "track_id":      item["id"],
                "track_name":    item["name"],
                "name":          item["name"],
                "artists":       ", ".join(a["name"] for a in item["artists"]),
                "album":         item["album"]["name"],
                "album_art":     item["album"]["images"][0]["url"] if item["album"]["images"] else None,
                "album_art_url": item["album"]["images"][0]["url"] if item["album"]["images"] else None,
                "is_playing":    data.get("is_playing", False),
                "progress_ms":   data.get("progress_ms", 0),
                "duration_ms":   item.get("duration_ms", 1),
                "spotify_url":   item["external_urls"]["spotify"],
            }

    return templates.TemplateResponse("now_playing.html", {
        "request": request,
        "track":   track,
    })


@app.get("/now-playing-json")
async def now_playing_json():
    if "access_token" not in token_store:
        return {}
    async with httpx.AsyncClient() as client:
        response = await client.get(
            SPOTIFY_NOW_PLAYING_URL,
            headers={"Authorization": f"Bearer {token_store['access_token']}"},
        )
    if response.status_code == 200:
        data = response.json()
        item = data.get("item")
        if item:
            return {
                "name":        item["name"],
                "artists":     ", ".join(a["name"] for a in item["artists"]),
                "album_art":   item["album"]["images"][0]["url"] if item["album"]["images"] else None,
                "progress_ms": data.get("progress_ms", 0),
                "duration_ms": item.get("duration_ms", 1),
                "is_playing":  data.get("is_playing", False),
            }
    return {}


@app.get("/queue", response_class=HTMLResponse)
async def queue(request: Request):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.spotify.com/v1/me/player/queue",
            headers={"Authorization": f"Bearer {token_store['access_token']}"},
        )

    if response.status_code == 401:
        refreshed = await refresh_access_token()
        if refreshed:
            return RedirectResponse("/queue")
        token_store.clear()
        return RedirectResponse("/")

    queue_tracks      = []
    currently_playing = None

    if response.status_code == 200:
        data    = response.json()
        current = data.get("currently_playing")
        if current and current.get("type") == "track":
            currently_playing = {
                "name":        current["name"],
                "artists":     ", ".join(a["name"] for a in current["artists"]),
                "album":       current["album"]["name"],
                "album_art":   current["album"]["images"][0]["url"] if current["album"]["images"] else None,
                "duration_ms": current["duration_ms"],
                "spotify_url": current["external_urls"]["spotify"],
            }
        for item in data.get("queue", [])[:15]:
            if item.get("type") == "track":
                queue_tracks.append({
                    "name":        item["name"],
                    "artists":     ", ".join(a["name"] for a in item["artists"]),
                    "album":       item["album"]["name"],
                    "album_art":   item["album"]["images"][0]["url"] if item["album"]["images"] else None,
                    "duration_ms": item["duration_ms"],
                    "spotify_url": item["external_urls"]["spotify"],
                })

    return templates.TemplateResponse("queue.html", {
        "request":           request,
        "currently_playing": currently_playing,
        "queue_tracks":      queue_tracks,
    })


@app.get("/recently-played", response_class=HTMLResponse)
async def recently_played(request: Request):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.spotify.com/v1/me/player/recently-played?limit=50",
            headers={"Authorization": f"Bearer {token_store['access_token']}"},
        )

    if response.status_code == 401:
        refreshed = await refresh_access_token()
        if refreshed:
            return RedirectResponse("/recently-played")
        token_store.clear()
        return RedirectResponse("/")

    tracks = []
    if response.status_code == 200:
        for item in response.json().get("items", []):
            track = item["track"]
            tracks.append({
                "name":        track["name"],
                "artists":     ", ".join(a["name"] for a in track["artists"]),
                "album":       track["album"]["name"],
                "album_art":   track["album"]["images"][0]["url"] if track["album"]["images"] else None,
                "duration_ms": track["duration_ms"],
                "played_at":   item["played_at"],
                "spotify_url": track["external_urls"]["spotify"],
            })

    return templates.TemplateResponse("recently_played.html", {
        "request": request,
        "tracks":  tracks,
    })


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "", db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    results = []
    if q:
        search_q = (
            select(TrackPlay)
            .where(
                TrackPlay.track_name.ilike(f"%{q}%") |
                TrackPlay.artists.ilike(f"%{q}%") |
                TrackPlay.album.ilike(f"%{q}%")
            )
            .order_by(TrackPlay.listened_at.desc())
            .limit(50)
        )
        results = (await db.execute(search_q)).scalars().fetchall()

    return templates.TemplateResponse("search.html", {
        "request": request,
        "query":   q,
        "results": results,
    })


@app.get("/stats", response_class=HTMLResponse)
async def stats(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    # ── Totals ────────────────────────────────────────────────────────────────
    total_plays    = await db.scalar(select(func.count()).select_from(TrackPlay)) or 0
    total_skips    = await db.scalar(select(func.count()).select_from(TrackPlay).where(TrackPlay.was_skipped == True)) or 0
    total_complete = total_plays - total_skips
    skip_rate      = round((total_skips / total_plays * 100), 1) if total_plays else 0
    total_ms       = await db.scalar(select(func.sum(TrackPlay.progress_ms)).select_from(TrackPlay)) or 0
    total_minutes  = round(total_ms / 60000, 1)
    total_hours    = round(total_minutes / 60, 1)
    unique_tracks  = await db.scalar(select(func.count(func.distinct(TrackPlay.track_id))).select_from(TrackPlay)) or 0

    # Unique artists — split featured artists
    all_unique_q    = select(TrackPlay.artists)
    all_unique_rows = (await db.execute(all_unique_q)).fetchall()
    unique_artist_set = set()
    for row in all_unique_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    unique_artist_set.add(a.lower())
    unique_artists = len(unique_artist_set)

    # ── Average progress before skip ──────────────────────────────────────────
    avg_skip_pct_row = await db.scalar(
        select(func.avg(TrackPlay.progress_pct))
        .where(TrackPlay.was_skipped == True)
    )
    avg_skip_pct = round(avg_skip_pct_row or 0, 1)

    # ── Most active day ───────────────────────────────────────────────────────
    busiest_day_q = (
        select(
            func.strftime('%Y-%m-%d', TrackPlay.listened_at).label("day"),
            func.count().label("cnt")
        )
        .group_by(func.strftime('%Y-%m-%d', TrackPlay.listened_at))
        .order_by(func.count().desc())
        .limit(1)
    )
    busiest_day_row = (await db.execute(busiest_day_q)).fetchone()
    busiest_day     = busiest_day_row.day if busiest_day_row else "N/A"
    busiest_day_cnt = busiest_day_row.cnt if busiest_day_row else 0

    # ── Longest no-skip streak ────────────────────────────────────────────────
    all_plays_q    = select(TrackPlay.was_skipped).order_by(TrackPlay.listened_at.asc())
    all_skips_col  = (await db.execute(all_plays_q)).fetchall()
    longest_streak = current_streak = 0
    for row in all_skips_col:
        if not row.was_skipped:
            current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            current_streak = 0

    # ── Top tracks ────────────────────────────────────────────────────────────
    most_played_q = (
        select(TrackPlay.track_name, TrackPlay.artists, func.count().label("plays"))
        .group_by(TrackPlay.track_id)
        .order_by(func.count().desc())
        .limit(10)
    )
    most_played = (await db.execute(most_played_q)).fetchall()

    most_skipped_q = (
        select(TrackPlay.track_name, TrackPlay.artists, func.count().label("skips"))
        .where(TrackPlay.was_skipped == True)
        .group_by(TrackPlay.track_id)
        .order_by(func.count().desc())
        .limit(10)
    )
    most_skipped = (await db.execute(most_skipped_q)).fetchall()

    always_completed_q = (
        select(
            TrackPlay.track_name,
            TrackPlay.artists,
            func.count().label("plays"),
            func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)).label("skips")
        )
        .group_by(TrackPlay.track_id)
        .having(
            func.count() >= 3,
            func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)) == 0
        )
        .order_by(func.count().desc())
        .limit(10)
    )
    always_completed = (await db.execute(always_completed_q)).fetchall()

    # ── Artists — split featured artists ─────────────────────────────────────
    all_plays_artists_q = select(TrackPlay.artists)
    all_artist_rows     = (await db.execute(all_plays_artists_q)).fetchall()
    artist_counter      = Counter()
    for row in all_artist_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    artist_counter[a] += 1
    most_played_artists = [ArtistRow(a, c) for a, c in artist_counter.most_common(10)]

    skipped_artists_q = select(TrackPlay.artists).where(TrackPlay.was_skipped == True)
    skipped_rows      = (await db.execute(skipped_artists_q)).fetchall()
    skip_counter      = Counter()
    for row in skipped_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    skip_counter[a] += 1
    most_skipped_artists = [ArtistSkipRow(a, c) for a, c in skip_counter.most_common(10)]

    all_plays_for_rate_q = select(TrackPlay.artists, TrackPlay.was_skipped)
    all_rate_rows        = (await db.execute(all_plays_for_rate_q)).fetchall()
    artist_plays         = Counter()
    artist_skips         = Counter()
    for row in all_rate_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    artist_plays[a] += 1
                    if row.was_skipped:
                        artist_skips[a] += 1
    best_artists = []
    for artist, plays in artist_plays.items():
        if plays >= 5:
            skips     = artist_skips.get(artist, 0)
            skip_rate_val = round(skips / plays * 100, 1)
            best_artists.append(ArtistRateRow(artist, plays, skips, skip_rate_val))
    best_artists = sorted(best_artists, key=lambda x: x.skip_rate)[:10]

    # ── Top albums ────────────────────────────────────────────────────────────
    most_played_albums_q = (
        select(TrackPlay.album, TrackPlay.artists, func.count().label("plays"))
        .group_by(TrackPlay.album)
        .order_by(func.count().desc())
        .limit(10)
    )
    most_played_albums = (await db.execute(most_played_albums_q)).fetchall()

    most_skipped_albums_q = (
        select(TrackPlay.album, TrackPlay.artists, func.count().label("skips"))
        .where(TrackPlay.was_skipped == True)
        .group_by(TrackPlay.album)
        .order_by(func.count().desc())
        .limit(10)
    )
    most_skipped_albums = (await db.execute(most_skipped_albums_q)).fetchall()

    # ── Time of day ───────────────────────────────────────────────────────────
    hour_q = (
        select(TrackPlay.hour_of_day, func.count().label("cnt"))
        .group_by(TrackPlay.hour_of_day)
    )
    hour_rows   = (await db.execute(hour_q)).fetchall()
    hour_data   = {r.hour_of_day: r.cnt for r in hour_rows if r.hour_of_day is not None}
    hour_labels = [f"{h:02d}:00" for h in range(24)]
    hour_values = [hour_data.get(h, 0) for h in range(24)]

    hour_skip_q = (
        select(
            TrackPlay.hour_of_day,
            func.count().label("total"),
            func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)).label("skips")
        )
        .group_by(TrackPlay.hour_of_day)
    )
    hour_skip_rows   = (await db.execute(hour_skip_q)).fetchall()
    hour_skip_map    = {r.hour_of_day: round(r.skips / r.total * 100, 1) for r in hour_skip_rows if r.total > 0 and r.hour_of_day is not None}
    hour_skip_values = [hour_skip_map.get(h, 0) for h in range(24)]

    # ── Day of week ───────────────────────────────────────────────────────────
    dow_order  = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    dow_q      = select(TrackPlay.day_of_week, func.count().label("cnt")).group_by(TrackPlay.day_of_week)
    dow_rows   = (await db.execute(dow_q)).fetchall()
    dow_map    = {r.day_of_week: r.cnt for r in dow_rows}
    dow_values = [dow_map.get(d, 0) for d in dow_order]

    # ── Month ─────────────────────────────────────────────────────────────────
    month_order  = ["January","February","March","April","May","June","July","August","September","October","November","December"]
    month_q      = select(TrackPlay.month, func.count().label("cnt")).group_by(TrackPlay.month)
    month_rows   = (await db.execute(month_q)).fetchall()
    month_map    = {r.month: r.cnt for r in month_rows}
    month_values = [month_map.get(m, 0) for m in month_order]

    # ── This week ─────────────────────────────────────────────────────────────
    week_ago = datetime.now(TIMEZONE) - timedelta(days=7)

    top_track_week_q = (
        select(TrackPlay.track_name, TrackPlay.artists, func.count().label("plays"))
        .where(TrackPlay.listened_at >= week_ago)
        .group_by(TrackPlay.track_id)
        .order_by(func.count().desc())
        .limit(5)
    )
    top_tracks_week = (await db.execute(top_track_week_q)).fetchall()

    top_artist_week_q = select(TrackPlay.artists).where(TrackPlay.listened_at >= week_ago)
    week_artist_rows  = (await db.execute(top_artist_week_q)).fetchall()
    week_artist_counter = Counter()
    for row in week_artist_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    week_artist_counter[a] += 1
    top_artists_week = [ArtistRow(a, c) for a, c in week_artist_counter.most_common(5)]

    # ── Recent history ────────────────────────────────────────────────────────
    recent_q = (
        select(TrackPlay)
        .order_by(TrackPlay.listened_at.desc())
        .limit(20)
    )
    recent = (await db.execute(recent_q)).scalars().fetchall()

    return templates.TemplateResponse("stats.html", {
        "request":              request,
        "total_plays":          total_plays,
        "total_skips":          total_skips,
        "total_complete":       total_complete,
        "skip_rate":            skip_rate,
        "total_minutes":        total_minutes,
        "total_hours":          total_hours,
        "unique_tracks":        unique_tracks,
        "unique_artists":       unique_artists,
        "avg_skip_pct":         avg_skip_pct,
        "busiest_day":          busiest_day,
        "busiest_day_cnt":      busiest_day_cnt,
        "longest_streak":       longest_streak,
        "most_played":          most_played,
        "most_skipped":         most_skipped,
        "always_completed":     always_completed,
        "most_played_artists":  most_played_artists,
        "most_skipped_artists": most_skipped_artists,
        "best_artists":         best_artists,
        "most_played_albums":   most_played_albums,
        "most_skipped_albums":  most_skipped_albums,
        "hour_labels":          hour_labels,
        "hour_values":          hour_values,
        "hour_skip_values":     hour_skip_values,
        "dow_order":            dow_order,
        "dow_values":           dow_values,
        "month_order":          month_order,
        "month_values":         month_values,
        "top_tracks_week":      top_tracks_week,
        "top_artists_week":     top_artists_week,
        "recent":               recent,
    })


@app.get("/recap", response_class=HTMLResponse)
async def recap(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    today     = datetime.now(TIMEZONE).date()
    today_str = today.strftime("%Y-%m-%d")

    today_plays_q = (
        select(TrackPlay)
        .where(func.strftime('%Y-%m-%d', TrackPlay.listened_at) == today_str)
        .order_by(TrackPlay.listened_at.asc())
    )
    today_plays = (await db.execute(today_plays_q)).scalars().fetchall()

    total_today     = len(today_plays)
    skips_today     = sum(1 for p in today_plays if p.was_skipped)
    completed_today = total_today - skips_today
    skip_rate_today = round((skips_today / total_today * 100), 1) if total_today else 0
    ms_today        = sum(p.progress_ms for p in today_plays)
    minutes_today   = round(ms_today / 60000, 1)

    track_counts = {}
    for p in today_plays:
        track_counts[p.track_id] = track_counts.get(p.track_id, {"name": p.track_name, "artists": p.artists, "art": p.album_art_url, "count": 0})
        track_counts[p.track_id]["count"] += 1
    top_track_today = max(track_counts.values(), key=lambda x: x["count"]) if track_counts else None

    artist_counter_today = Counter()
    for p in today_plays:
        if p.artists:
            for a in p.artists.split(","):
                a = a.strip()
                if a:
                    artist_counter_today[a] += 1
    top_artist_today = artist_counter_today.most_common(1)[0] if artist_counter_today else None

    first_track          = today_plays[0] if today_plays else None
    last_track           = today_plays[-1] if today_plays else None
    unique_tracks_today  = len(set(p.track_id for p in today_plays))
    unique_artists_today = len(set(
        a.strip()
        for p in today_plays if p.artists
        for a in p.artists.split(",")
        if a.strip()
    ))

    hour_breakdown = {}
    for p in today_plays:
        h = p.hour_of_day or 0
        hour_breakdown[h] = hour_breakdown.get(h, 0) + 1
    peak_hour = max(hour_breakdown.items(), key=lambda x: x[1]) if hour_breakdown else None

    return templates.TemplateResponse("recap.html", {
        "request":              request,
        "today":                today_str,
        "today_plays":          today_plays,
        "total_today":          total_today,
        "skips_today":          skips_today,
        "completed_today":      completed_today,
        "skip_rate_today":      skip_rate_today,
        "minutes_today":        minutes_today,
        "top_track_today":      top_track_today,
        "top_artist_today":     top_artist_today,
        "first_track":          first_track,
        "last_track":           last_track,
        "unique_tracks_today":  unique_tracks_today,
        "unique_artists_today": unique_artists_today,
        "peak_hour":            peak_hour,
    })


@app.get("/clock", response_class=HTMLResponse)
async def listening_clock(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    hour_q = (
        select(TrackPlay.hour_of_day, func.count().label("cnt"))
        .group_by(TrackPlay.hour_of_day)
    )
    hour_rows   = (await db.execute(hour_q)).fetchall()
    hour_data   = {r.hour_of_day: r.cnt for r in hour_rows if r.hour_of_day is not None}
    hour_values = [hour_data.get(h, 0) for h in range(24)]
    max_plays   = max(hour_values) if any(hour_values) else 1

    hour_skip_q = (
        select(
            TrackPlay.hour_of_day,
            func.count().label("total"),
            func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)).label("skips")
        )
        .group_by(TrackPlay.hour_of_day)
    )
    hour_skip_rows  = (await db.execute(hour_skip_q)).fetchall()
    hour_skip_map   = {
        r.hour_of_day: round(r.skips / r.total * 100, 1)
        for r in hour_skip_rows
        if r.total > 0 and r.hour_of_day is not None
    }
    hour_skip_rates = [hour_skip_map.get(h, 0) for h in range(24)]

    return templates.TemplateResponse("clock.html", {
        "request":         request,
        "hour_values":     hour_values,
        "hour_skip_rates": hour_skip_rates,
        "max_plays":       max_plays,
    })


@app.get("/skip-heatmap", response_class=HTMLResponse)
async def skip_heatmap(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    plays_q = (
        select(TrackPlay.progress_pct, TrackPlay.was_skipped, TrackPlay.track_name, TrackPlay.artists)
        .where(TrackPlay.was_skipped == True)
        .order_by(TrackPlay.progress_pct.asc())
    )
    skipped_plays = (await db.execute(plays_q)).fetchall()

    buckets = {i: 0 for i in range(0, 80, 5)}
    for play in skipped_plays:
        bucket = min(int(play.progress_pct // 5) * 5, 75)
        buckets[bucket] = buckets.get(bucket, 0) + 1

    bucket_labels = [f"{k}-{k+5}%" for k in sorted(buckets.keys())]
    bucket_values = [buckets[k] for k in sorted(buckets.keys())]

    bucket_tracks = {}
    for play in skipped_plays:
        bucket    = min(int(play.progress_pct // 5) * 5, 75)
        key       = f"{bucket}-{bucket+5}%"
        if key not in bucket_tracks:
            bucket_tracks[key] = {}
        track_key = f"{play.track_name} — {play.artists}"
        bucket_tracks[key][track_key] = bucket_tracks[key].get(track_key, 0) + 1

    top_per_bucket = {}
    for bucket, tracks in bucket_tracks.items():
        top = max(tracks.items(), key=lambda x: x[1])
        top_per_bucket[bucket] = {"track": top[0], "count": top[1]}

    total_skips = len(skipped_plays)
    early_skips = sum(1 for p in skipped_plays if p.progress_pct < 20)
    mid_skips   = sum(1 for p in skipped_plays if 20 <= p.progress_pct < 50)
    late_skips  = sum(1 for p in skipped_plays if p.progress_pct >= 50)

    return templates.TemplateResponse("skip_heatmap.html", {
        "request":        request,
        "bucket_labels":  bucket_labels,
        "bucket_values":  bucket_values,
        "top_per_bucket": top_per_bucket,
        "total_skips":    total_skips,
        "early_skips":    early_skips,
        "mid_skips":      mid_skips,
        "late_skips":     late_skips,
    })


@app.get("/streaks", response_class=HTMLResponse)
async def streaks(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    all_plays_q = select(TrackPlay).order_by(TrackPlay.listened_at.asc())
    all_plays   = (await db.execute(all_plays_q)).scalars().fetchall()

    days_with_plays = set()
    for p in all_plays:
        days_with_plays.add(p.listened_at.date())

    sorted_days        = sorted(days_with_plays)
    longest_day_streak = temp_streak = 0
    prev_day           = None
    for day in sorted_days:
        if prev_day is None or (day - prev_day).days == 1:
            temp_streak += 1
        else:
            temp_streak = 1
        longest_day_streak = max(longest_day_streak, temp_streak)
        prev_day = day

    today     = datetime.now(TIMEZONE).date()
    yesterday = today - timedelta(days=1)
    current_day_streak = temp_streak if (today in days_with_plays or yesterday in days_with_plays) else 0

    longest_noskip = temp_noskip = 0
    for play in all_plays:
        if not play.was_skipped:
            temp_noskip += 1
        else:
            longest_noskip = max(longest_noskip, temp_noskip)
            temp_noskip    = 0
    longest_noskip = max(longest_noskip, temp_noskip)

    current_noskip = 0
    for play in reversed(all_plays):
        if not play.was_skipped:
            current_noskip += 1
        else:
            break

    # Artist loyalty streak — use primary artist
    best_artist_streak = {"artist": None, "count": 0}
    temp_artist        = {"artist": None, "count": 0}
    for play in all_plays:
        primary = play.artists.split(",")[0].strip() if play.artists else None
        if primary == temp_artist["artist"]:
            temp_artist["count"] += 1
        else:
            if temp_artist["count"] > best_artist_streak["count"]:
                best_artist_streak = dict(temp_artist)
            temp_artist = {"artist": primary, "count": 1}
    if temp_artist["count"] > best_artist_streak["count"]:
        best_artist_streak = dict(temp_artist)

    thirty_days_ago = datetime.now(TIMEZONE) - timedelta(days=30)
    recent_plays_q  = (
        select(TrackPlay)
        .where(TrackPlay.listened_at >= thirty_days_ago)
        .order_by(TrackPlay.listened_at.asc())
    )
    recent_plays = (await db.execute(recent_plays_q)).scalars().fetchall()

    daily_counts = defaultdict(int)
    for p in recent_plays:
        daily_counts[p.listened_at.strftime("%m/%d")] += 1

    day_labels = []
    day_values = []
    for i in range(30):
        d = (datetime.now(TIMEZONE) - timedelta(days=29 - i)).strftime("%m/%d")
        day_labels.append(d)
        day_values.append(daily_counts.get(d, 0))

    total_active_days  = len(days_with_plays)
    total_days_tracked = (today - sorted_days[0]).days + 1 if sorted_days else 0
    activity_rate      = round(total_active_days / total_days_tracked * 100, 1) if total_days_tracked else 0

    return templates.TemplateResponse("streaks.html", {
        "request":            request,
        "current_day_streak": current_day_streak,
        "longest_day_streak": longest_day_streak,
        "current_noskip":     current_noskip,
        "longest_noskip":     longest_noskip,
        "best_artist_streak": best_artist_streak,
        "day_labels":         day_labels,
        "day_values":         day_values,
        "total_active_days":  total_active_days,
        "activity_rate":      activity_rate,
    })




@app.get("/evolution", response_class=HTMLResponse)
async def taste_evolution(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    months_q = (
        select(TrackPlay.month, func.count().label("cnt"))
        .group_by(TrackPlay.month)
        .order_by(func.count().desc())
    )
    months_with_data = (await db.execute(months_q)).fetchall()
    month_names      = [r.month for r in months_with_data]

    # Evolution — split featured artists
    evolution = {}
    for month in month_names:
        month_plays_q   = select(TrackPlay.artists).where(TrackPlay.month == month)
        month_play_rows = (await db.execute(month_plays_q)).fetchall()
        month_counter   = Counter()
        for row in month_play_rows:
            if row.artists:
                for a in row.artists.split(","):
                    a = a.strip()
                    if a:
                        month_counter[a] += 1
        evolution[month] = [{"artist": a, "plays": c} for a, c in month_counter.most_common(5)]

    # Top track per month
    top_track_per_month = {}
    for month in month_names:
        track_q = (
            select(TrackPlay.track_name, TrackPlay.artists, func.count().label("plays"))
            .where(TrackPlay.month == month)
            .group_by(TrackPlay.track_id)
            .order_by(func.count().desc())
            .limit(1)
        )
        result = (await db.execute(track_q)).fetchone()
        if result:
            top_track_per_month[month] = {
                "name":    result.track_name,
                "artists": result.artists.split(",")[0].strip() if result.artists else result.artists,
                "plays":   result.plays,
            }

    # Top 5 artists overall — split featured
    all_artists_plays_q = select(TrackPlay.artists)
    all_artists_rows    = (await db.execute(all_artists_plays_q)).fetchall()
    all_artist_counter  = Counter()
    for row in all_artists_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    all_artist_counter[a] += 1
    top_5_artists = [a for a, _ in all_artist_counter.most_common(5)]

    # Chart datasets
    chart_datasets = []
    neon_colors    = ['#ff00ff', '#00ffff', '#ffff00', '#00ff88', '#ff00aa']
    for i, artist in enumerate(top_5_artists):
        data_points = []
        for month in month_names:
            month_plays_q   = select(TrackPlay.artists).where(TrackPlay.month == month)
            month_play_rows = (await db.execute(month_plays_q)).fetchall()
            count = sum(
                1 for row in month_play_rows
                if row.artists and any(
                    a.strip().lower() == artist.lower()
                    for a in row.artists.split(",")
                )
            )
            data_points.append(count)
        chart_datasets.append({
            "label":           artist,
            "data":            data_points,
            "borderColor":     neon_colors[i % len(neon_colors)],
            "backgroundColor": neon_colors[i % len(neon_colors)] + "33",
            "tension":         0.4,
            "fill":            False,
        })

    return templates.TemplateResponse("evolution.html", {
        "request":             request,
        "month_names":         month_names,
        "evolution":           evolution,
        "top_track_per_month": top_track_per_month,
        "chart_datasets":      chart_datasets,
    })


@app.get("/avoiding", response_class=HTMLResponse)
async def what_are_you_avoiding(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    avoiding_q = (
        select(
            TrackPlay.track_id,
            TrackPlay.track_name,
            TrackPlay.artists,
            TrackPlay.album,
            TrackPlay.album_art_url,
            func.count().label("attempts"),
            func.avg(TrackPlay.progress_pct).label("avg_pct"),
            func.max(TrackPlay.progress_pct).label("max_pct"),
            func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)).label("skips"),
        )
        .group_by(TrackPlay.track_id)
        .having(
            func.count() >= 5,
            func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)) == func.count()
        )
        .order_by(func.count().desc())
    )
    avoiding = (await db.execute(avoiding_q)).fetchall()

    almost_avoiding_q = (
        select(
            TrackPlay.track_id,
            TrackPlay.track_name,
            TrackPlay.artists,
            TrackPlay.album_art_url,
            func.count().label("attempts"),
            func.avg(TrackPlay.progress_pct).label("avg_pct"),
        )
        .group_by(TrackPlay.track_id)
        .having(
            func.count() >= 3,
            func.avg(TrackPlay.progress_pct) < 30,
        )
        .order_by(func.count().desc())
        .limit(10)
    )
    almost_avoiding = (await db.execute(almost_avoiding_q)).fetchall()

    return templates.TemplateResponse("avoiding.html", {
        "request":         request,
        "avoiding":        avoiding,
        "almost_avoiding": almost_avoiding,
    })


@app.get("/vibe", response_class=HTMLResponse)
async def vibe(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    # ── Mood distribution ─────────────────────────────────────────────────────
    mood_q = (
        select(TrackPlay.auto_mood, func.count().label("cnt"))
        .where(TrackPlay.auto_mood != None)
        .group_by(TrackPlay.auto_mood)
        .order_by(func.count().desc())
    )
    mood_rows   = (await db.execute(mood_q)).fetchall()
    mood_labels = [r.auto_mood for r in mood_rows]
    mood_values = [r.cnt for r in mood_rows]

    # ── Mood by hour ──────────────────────────────────────────────────────────
    mood_hour_q = (
        select(TrackPlay.hour_of_day, TrackPlay.auto_mood, func.count().label("cnt"))
        .where(TrackPlay.auto_mood != None)
        .group_by(TrackPlay.hour_of_day, TrackPlay.auto_mood)
    )
    mood_hour_rows = (await db.execute(mood_hour_q)).fetchall()
    hour_mood_map  = {}
    for r in mood_hour_rows:
        if r.hour_of_day not in hour_mood_map:
            hour_mood_map[r.hour_of_day] = {}
        hour_mood_map[r.hour_of_day][r.auto_mood] = r.cnt
    dominant_mood_by_hour = {
        h: max(moods.items(), key=lambda x: x[1])[0]
        for h, moods in hour_mood_map.items()
    }
    hour_mood_labels = [f"{h:02d}:00" for h in range(24)]
    hour_mood_values = [dominant_mood_by_hour.get(h, "—") for h in range(24)]

    # ── Mood by day of week ───────────────────────────────────────────────────
    dow_order  = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    mood_dow_q = (
        select(TrackPlay.day_of_week, TrackPlay.auto_mood, func.count().label("cnt"))
        .where(TrackPlay.auto_mood != None)
        .group_by(TrackPlay.day_of_week, TrackPlay.auto_mood)
    )
    mood_dow_rows = (await db.execute(mood_dow_q)).fetchall()
    dow_mood_map  = {}
    for r in mood_dow_rows:
        if r.day_of_week not in dow_mood_map:
            dow_mood_map[r.day_of_week] = {}
        dow_mood_map[r.day_of_week][r.auto_mood] = r.cnt
    dominant_mood_by_dow = {
        d: max(moods.items(), key=lambda x: x[1])[0]
        for d, moods in dow_mood_map.items()
    }
    dow_mood_values = [dominant_mood_by_dow.get(d, "—") for d in dow_order]

    # ── Genre distribution ────────────────────────────────────────────────────
    all_genre_plays_q = select(TrackPlay.primary_genre).where(TrackPlay.primary_genre != None)
    all_genre_rows    = (await db.execute(all_genre_plays_q)).fetchall()

    genre_counter = Counter()
    for row in all_genre_rows:
        if row.primary_genre:
            for g in row.primary_genre.split(","):
                g = g.strip()
                if g:
                    genre_counter[g.lower()] += 1

    top_genres   = genre_counter.most_common(15)
    genre_labels = [g[0] for g in top_genres]
    genre_values = [g[1] for g in top_genres]

    # ── Genre to mood map ─────────────────────────────────────────────────────
    genre_mood_plays_q = (
        select(TrackPlay.primary_genre, TrackPlay.auto_mood)
        .where(TrackPlay.primary_genre != None)
        .where(TrackPlay.auto_mood != None)
    )
    genre_mood_rows  = (await db.execute(genre_mood_plays_q)).fetchall()
    genre_mood_tally = {}
    for row in genre_mood_rows:
        if row.primary_genre:
            for g in row.primary_genre.split(","):
                g = g.strip().lower()
                if g:
                    if g not in genre_mood_tally:
                        genre_mood_tally[g] = Counter()
                    genre_mood_tally[g][row.auto_mood] += 1

    genre_mood_map = {
        g: counts.most_common(1)[0][0]
        for g, counts in genre_mood_tally.items()
        if counts
    }
    genre_mood_map = dict(
        sorted(genre_mood_map.items(),
               key=lambda x: sum(genre_mood_tally[x[0]].values()),
               reverse=True)[:20]
    )

    # ── Recent tracks ─────────────────────────────────────────────────────────
    recent_q = (
        select(TrackPlay)
        .where(TrackPlay.auto_mood != None)
        .order_by(TrackPlay.listened_at.desc())
        .limit(20)
    )
    recent = (await db.execute(recent_q)).scalars().fetchall()

    # ── Current vibe ──────────────────────────────────────────────────────────
    current_vibe_q = (
        select(TrackPlay.auto_mood)
        .where(TrackPlay.auto_mood != None)
        .order_by(TrackPlay.listened_at.desc())
        .limit(10)
    )
    current_vibe_rows = (await db.execute(current_vibe_q)).fetchall()
    if current_vibe_rows:
        mood_counter = Counter(r.auto_mood for r in current_vibe_rows)
        current_vibe = mood_counter.most_common(1)[0][0]
    else:
        current_vibe = "UNKNOWN"

    return templates.TemplateResponse("vibe.html", {
        "request":          request,
        "mood_labels":      mood_labels,
        "mood_values":      mood_values,
        "hour_mood_labels": hour_mood_labels,
        "hour_mood_values": hour_mood_values,
        "dow_order":        dow_order,
        "dow_mood_values":  dow_mood_values,
        "genre_labels":     genre_labels,
        "genre_values":     genre_values,
        "genre_mood_map":   genre_mood_map,
        "recent":           recent,
        "current_vibe":     current_vibe,
    })

@app.get("/calendar", response_class=HTMLResponse)
async def calendar(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    # Get all plays with dates
    all_plays_q = (
        select(TrackPlay.listened_at, TrackPlay.was_skipped, TrackPlay.progress_ms)
        .order_by(TrackPlay.listened_at.asc())
    )
    all_plays = (await db.execute(all_plays_q)).fetchall()

    # Build daily stats
    daily_plays    = Counter()
    daily_skips    = Counter()
    daily_minutes  = defaultdict(float)

    for play in all_plays:
        day = play.listened_at.strftime("%Y-%m-%d")
        daily_plays[day]   += 1
        daily_minutes[day] += play.progress_ms / 60000
        if play.was_skipped:
            daily_skips[day] += 1

    # Build full year grid — last 365 days
    today     = datetime.now(TIMEZONE).date()
    start     = today - timedelta(days=364)
    all_dates = []
    d         = start
    while d <= today:
        date_str = d.strftime("%Y-%m-%d")
        all_dates.append({
            "date":    date_str,
            "plays":   daily_plays.get(date_str, 0),
            "skips":   daily_skips.get(date_str, 0),
            "minutes": round(daily_minutes.get(date_str, 0), 1),
            "dow":     d.weekday(),
            "month":   d.strftime("%b"),
            "day":     d.day,
        })
        d += timedelta(days=1)

    max_plays = max((d["plays"] for d in all_dates), default=1)

    # Summary stats
    active_days    = sum(1 for d in all_dates if d["plays"] > 0)
    total_plays    = sum(d["plays"] for d in all_dates)
    total_minutes  = round(sum(d["minutes"] for d in all_dates), 1)
    best_day       = max(all_dates, key=lambda d: d["plays"])

    return templates.TemplateResponse("calendar.html", {
        "request":       request,
        "all_dates":     all_dates,
        "max_plays":     max_plays,
        "active_days":   active_days,
        "total_plays":   total_plays,
        "total_minutes": total_minutes,
        "best_day":      best_day,
    })


@app.get("/sessions", response_class=HTMLResponse)
async def peak_sessions(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    all_plays_q = (
        select(TrackPlay)
        .order_by(TrackPlay.listened_at.asc())
    )
    all_plays = (await db.execute(all_plays_q)).scalars().fetchall()

    # Detect sessions — gap of more than 30 minutes = new session
    SESSION_GAP_MINUTES = 30
    sessions            = []
    current_session     = []

    for play in all_plays:
        if not current_session:
            current_session.append(play)
        else:
            last = current_session[-1]
            gap  = (play.listened_at - last.listened_at).total_seconds() / 60
            if gap <= SESSION_GAP_MINUTES:
                current_session.append(play)
            else:
                sessions.append(current_session)
                current_session = [play]

    if current_session:
        sessions.append(current_session)

    # Build session stats
    session_stats = []
    for session in sessions:
        if len(session) < 2:
            continue
        start_time   = session[0].listened_at
        end_time     = session[-1].listened_at
        duration_min = round((end_time - start_time).total_seconds() / 60, 1)
        if duration_min < 5:
            continue

        track_count  = len(session)
        skip_count   = sum(1 for p in session if p.was_skipped)
        skip_rate    = round(skip_count / track_count * 100, 1) if track_count else 0
        total_ms     = sum(p.progress_ms for p in session)
        minutes      = round(total_ms / 60000, 1)

        # Dominant mood
        mood_counts  = Counter(p.auto_mood for p in session if p.auto_mood)
        dominant_mood = mood_counts.most_common(1)[0][0] if mood_counts else "UNKNOWN"

        # Top artist
        artist_counts = Counter()
        for p in session:
            if p.artists:
                for a in p.artists.split(","):
                    a = a.strip()
                    if a:
                        artist_counts[a] += 1
        top_artist = artist_counts.most_common(1)[0][0] if artist_counts else "Unknown"

        # Top genre
        genre_counts = Counter()
        for p in session:
            if p.primary_genre:
                for g in p.primary_genre.split(","):
                    g = g.strip()
                    if g:
                        genre_counts[g.lower()] += 1
        top_genre = genre_counts.most_common(1)[0][0] if genre_counts else "Unknown"

        # First and last track
        first_track = session[0].track_name
        last_track  = session[-1].track_name

        session_stats.append({
            "start":        start_time.strftime("%m/%d %H:%M"),
            "end":          end_time.strftime("%H:%M"),
            "date":         start_time.strftime("%A, %B %d"),
            "duration_min": duration_min,
            "track_count":  track_count,
            "skip_count":   skip_count,
            "skip_rate":    skip_rate,
            "minutes":      minutes,
            "mood":         dominant_mood,
            "top_artist":   top_artist,
            "top_genre":    top_genre,
            "first_track":  first_track,
            "last_track":   last_track,
            "album_art":    session[0].album_art_url,
        })

    # Sort by duration
    session_stats = sorted(session_stats, key=lambda x: x["duration_min"], reverse=True)

    # Summary
    total_sessions   = len(session_stats)
    avg_duration     = round(sum(s["duration_min"] for s in session_stats) / total_sessions, 1) if total_sessions else 0
    longest_session  = session_stats[0] if session_stats else None
    avg_tracks       = round(sum(s["track_count"] for s in session_stats) / total_sessions, 1) if total_sessions else 0

    return templates.TemplateResponse("sessions.html", {
        "request":         request,
        "sessions":        session_stats[:50],
        "total_sessions":  total_sessions,
        "avg_duration":    avg_duration,
        "longest_session": longest_session,
        "avg_tracks":      avg_tracks,
    })

@app.get("/artist-graph", response_class=HTMLResponse)
async def artist_graph(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    from datetime import timedelta
    from itertools import combinations

    # Get ALL tracks
    all_plays_q = (
        select(TrackPlay.track_name, TrackPlay.artists, TrackPlay.album_art_url)
        .group_by(TrackPlay.track_id)
    )
    all_plays = (await db.execute(all_plays_q)).fetchall()

    connections  = defaultdict(lambda: {"count": 0, "tracks": set()})
    artist_plays = Counter()

    # Count all plays
    all_artists_q   = select(TrackPlay.artists)
    all_artist_rows = (await db.execute(all_artists_q)).fetchall()
    for row in all_artist_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    artist_plays[a] += 1

    # Build connections for tracks with multiple artists
    for play in all_plays:
        artists = [a.strip() for a in play.artists.split(",") if a.strip()]
        if len(artists) > 1:
            for a1, a2 in combinations(sorted(artists), 2):
                key = (a1, a2)
                connections[key]["count"]  += 1
                connections[key]["tracks"].add(play.track_name)

    all_artists = set(artist_plays.keys())

    if not all_artists:
        return templates.TemplateResponse("artist_graph.html", {
            "request":    request,
            "graph_data": '{"nodes": [], "links": []}',
            "node_count": 0,
            "link_count": 0,
        })

    # Build map of artist name -> spotify ID
    # First try the dedicated ArtistSpotifyID table
    artist_id_map = {}

    id_table_q    = select(ArtistSpotifyID)
    id_table_rows = (await db.execute(id_table_q)).fetchall()
    for row in id_table_rows:
        artist_id_map[row[0].artist_name] = row[0].spotify_id

    # Fall back to track history for any still missing
    artist_id_q    = (
        select(TrackPlay.artists, TrackPlay.artist_spotify_id)
        .where(TrackPlay.artist_spotify_id != None)
        .group_by(TrackPlay.artists)
    )
    artist_id_rows = (await db.execute(artist_id_q)).fetchall()
    for row in artist_id_rows:
        if row.artists:
            primary = row.artists.split(",")[0].strip()
            if primary not in artist_id_map:
                artist_id_map[primary] = row.artist_spotify_id

    print(f"[GRAPH] Artist ID map has {len(artist_id_map)} entries")

    # Fetch artist images concurrently with caching
    artist_images = {}

    async def fetch_artist_image(artist_name: str):
        # Check cache — only use if fetched within last 7 days and has a valid URL
        async with AsyncSessionLocal() as cache_db:
            cache_q = select(ArtistCache).where(
                ArtistCache.artist_name == artist_name,
                ArtistCache.track_name  == None,
                ArtistCache.image_url   != None,
                ArtistCache.fetched_at  >= datetime.now(TIMEZONE) - timedelta(days=7),
            ).limit(1)
            cached = (await cache_db.execute(cache_q)).scalar_one_or_none()
            if cached and cached.image_url:
                artist_images[artist_name] = cached.image_url
                return

        async def save_image_to_cache(name: str, image_url: str):
            if not image_url:
                return
            async with AsyncSessionLocal() as cache_db:
                existing_q = select(ArtistCache).where(
                    ArtistCache.artist_name == name,
                    ArtistCache.track_name  == None,
                ).limit(1)
                existing = (await cache_db.execute(existing_q)).scalar_one_or_none()
                if existing:
                    existing.image_url = image_url
                    await cache_db.commit()
                else:
                    entry = ArtistCache(
                        artist_name = name,
                        track_name  = None,
                        image_url   = image_url,
                    )
                    cache_db.add(entry)
                    await cache_db.commit()

        try:
            spotify_id = artist_id_map.get(artist_name)

            if spotify_id:
                # Use direct artist ID lookup — guaranteed correct
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"https://api.spotify.com/v1/artists/{spotify_id}",
                        headers={"Authorization": f"Bearer {token_store['access_token']}"},
                    )
                if resp.status_code == 200:
                    data      = resp.json()
                    images    = data.get("images", [])
                    image_url = images[-1]["url"] if images else None
                    artist_images[artist_name] = image_url
                    await save_image_to_cache(artist_name, image_url)
                    return
                elif resp.status_code == 429:
                    artist_images[artist_name] = None
                    return

            # No stored ID — fall back to strict exact name search only
            encoded = urllib.parse.quote(artist_name)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"https://api.spotify.com/v1/search?q={encoded}&type=artist&limit=5",
                    headers={"Authorization": f"Bearer {token_store['access_token']}"},
                )

            if resp.status_code == 429:
                artist_images[artist_name] = None
                return

            if resp.status_code == 200:
                items        = resp.json().get("artists", {}).get("items", [])
                artist_lower = artist_name.lower().strip()
                matched      = None

                for item in items:
                    if item["name"].lower().strip() == artist_lower:
                        matched = item
                        break

                if matched:
                    images    = matched.get("images", [])
                    image_url = images[-1]["url"] if images else None
                    artist_images[artist_name] = image_url
                    await save_image_to_cache(artist_name, image_url)
                    # Also store the Spotify ID for future use
                    try:
                        async with AsyncSessionLocal() as id_db:
                            existing_q = select(ArtistSpotifyID).where(
                                ArtistSpotifyID.artist_name == artist_name
                            )
                            existing = (await id_db.execute(existing_q)).scalar_one_or_none()
                            if not existing:
                                id_db.add(ArtistSpotifyID(
                                    artist_name = artist_name,
                                    spotify_id  = matched["id"],
                                ))
                                await id_db.commit()
                    except Exception:
                        pass
                else:
                    artist_images[artist_name] = None

        except Exception:
            artist_images[artist_name] = None

    # Prioritize artists that have a known Spotify ID — they load fastest
    # and are guaranteed correct. Put those first, unknown ones after.
    known_id_artists   = [a for a in all_artists if a in artist_id_map]
    unknown_id_artists = [a for a in all_artists if a not in artist_id_map]

    # Process known ID artists first with larger batches since they're fast
    artist_list = known_id_artists + unknown_id_artists

    batch_size = 10
    for i in range(0, len(artist_list), batch_size):
        batch = artist_list[i:i + batch_size]
        # Use smaller delay for known ID artists, larger for search fallback
        is_known_batch = all(a in artist_id_map for a in batch)
        await asyncio.gather(*[fetch_artist_image(a) for a in batch])
        await asyncio.sleep(0.1 if is_known_batch else 0.3)

    # Build nodes
    connected_artists = set()
    for (a1, a2) in connections:
        connected_artists.add(a1)
        connected_artists.add(a2)

    nodes = []
    for artist in all_artists:
        nodes.append({
            "id":        artist,
            "image":     artist_images.get(artist),
            "plays":     artist_plays.get(artist, 1),
            "connected": artist in connected_artists,
        })

    # Build links
 # Build links — deduplicate track names within each connection
    links = []
    for (a1, a2), data in connections.items():
        # Deduplicate track names case-insensitively
        seen_tracks  = set()
        unique_tracks = []
        for track in data["tracks"]:
            if track.lower().strip() not in seen_tracks:
                seen_tracks.add(track.lower().strip())
                unique_tracks.append(track)
        links.append({
            "source": a1,
            "target": a2,
            "count":  len(unique_tracks),
            "tracks": unique_tracks,
        })

    import json
    graph_data = json.dumps({"nodes": nodes, "links": links})

    return templates.TemplateResponse("artist_graph.html", {
        "request":    request,
        "graph_data": graph_data,
        "node_count": len(nodes),
        "link_count": len(links),
    })



@app.get("/rave", response_class=HTMLResponse)
async def concert(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            SPOTIFY_NOW_PLAYING_URL,
            headers={"Authorization": f"Bearer {token_store['access_token']}"},
        )

    if response.status_code == 401:
        refreshed = await refresh_access_token()
        if refreshed:
            return RedirectResponse("/rave")
        token_store.clear()
        return RedirectResponse("/")

    track = None

    if response.status_code == 200:
        data = response.json()
        item = data.get("item")
        if item:
            track = {
                "name":        item["name"],
                "artists":     ", ".join(a["name"] for a in item["artists"]),
                "album_art":   item["album"]["images"][0]["url"] if item["album"]["images"] else None,
                "is_playing":  data.get("is_playing", False),
                "progress_ms": data.get("progress_ms", 0),
                "duration_ms": item.get("duration_ms", 1),
            }

    # Get mood from most recent committed track in DB
    recent_mood_q = (
        select(TrackPlay.auto_mood)
        .where(TrackPlay.auto_mood != None)
        .order_by(TrackPlay.listened_at.desc())
        .limit(1)
    )
    recent_mood_row = (await db.execute(recent_mood_q)).scalar_one_or_none()
    auto_mood       = recent_mood_row if recent_mood_row else "NEUTRAL"

    return templates.TemplateResponse("rave.html", {
        "request":   request,
        "track":     track,
        "auto_mood": auto_mood,
    })


@app.get("/mosaic", response_class=HTMLResponse)
async def mosaic(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    albums_q = (
        select(
            TrackPlay.album,
            TrackPlay.artists,
            TrackPlay.album_art_url,
            func.count().label("plays"),
            func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)).label("skips"),
            func.avg(TrackPlay.progress_pct).label("avg_pct"),
        )
        .where(TrackPlay.album_art_url != None)
        .group_by(TrackPlay.album)
        .order_by(func.count().desc())
    )
    albums = (await db.execute(albums_q)).fetchall()

    album_list = []
    for a in albums:
        raw_avg  = a.avg_pct or 0
        avg_pct  = 100 if raw_avg >= 95 else round(raw_avg, 1)
        album_list.append({
            "album":     a.album,
            "artists":   a.artists.split(",")[0].strip() if a.artists else a.artists,
            "art":       a.album_art_url,
            "plays":     a.plays,
            "skips":     a.skips,
            "avg_pct":   avg_pct,
            "skip_rate": round((a.skips / a.plays * 100) if a.plays else 0, 1),
        })

    import json
    return templates.TemplateResponse("mosaic.html", {
        "request":    request,
        "album_list": json.dumps(album_list),
        "count":      len(album_list),
    })

@app.get("/time-machine", response_class=HTMLResponse)
async def time_machine(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    # Get all available dates
    dates_q = (
        select(func.strftime('%Y-%m-%d', TrackPlay.listened_at).label("day"))
        .group_by(func.strftime('%Y-%m-%d', TrackPlay.listened_at))
        .order_by(func.strftime('%Y-%m-%d', TrackPlay.listened_at).desc())
    )
    available_dates = [r.day for r in (await db.execute(dates_q)).fetchall()]

    return templates.TemplateResponse("time_machine.html", {
        "request":         request,
        "available_dates": available_dates,
        "selected_date":   None,
        "plays":           [],
        "stats":           None,
    })


@app.get("/artist/{artist_name}", response_class=HTMLResponse)
async def artist_deep_dive(request: Request, artist_name: str, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    import urllib.parse
    artist_name = urllib.parse.unquote(artist_name)

    plays_q = (
        select(TrackPlay)
        .where(TrackPlay.artists.ilike(f"%{artist_name}%"))
        .order_by(TrackPlay.listened_at.asc())
    )
    plays = (await db.execute(plays_q)).scalars().fetchall()

    # Filter to exact artist match
    plays = [
        p for p in plays
        if any(a.strip().lower() == artist_name.lower()
               for a in (p.artists or "").split(","))
    ]

    if not plays:
        return templates.TemplateResponse("artist_dive.html", {
            "request":     request,
            "artist_name": artist_name,
            "found":       False,
        })

    total     = len(plays)
    skips     = sum(1 for p in plays if p.was_skipped)
    completes = total - skips
    skip_rate = round(skips / total * 100, 1) if total else 0
    comp_rate = round(completes / total * 100, 1) if total else 0
    minutes   = round(sum(p.progress_ms for p in plays) / 60000, 1)
    hours     = round(minutes / 60, 2)

    # ── Streak ────────────────────────────────────────────────────────────
    days_with_plays = sorted(set(p.listened_at.date() for p in plays))
    max_streak = temp_streak = 1
    for i in range(1, len(days_with_plays)):
        if (days_with_plays[i] - days_with_plays[i-1]).days == 1:
            temp_streak += 1
            max_streak   = max(max_streak, temp_streak)
        else:
            temp_streak = 1

    # ── Current streak ────────────────────────────────────────────────────
    today          = datetime.now(TIMEZONE).date()
    yesterday      = today - timedelta(days=1)
    current_streak = 0
    for day in reversed(days_with_plays):
        if day == today or day == yesterday:
            current_streak += 1
        elif (today - day).days <= current_streak + 1:
            current_streak += 1
        else:
            break

    # ── Peak hour ─────────────────────────────────────────────────────────
    hour_counter    = Counter(p.hour_of_day for p in plays if p.hour_of_day is not None)
    peak_hour       = hour_counter.most_common(1)[0][0] if hour_counter else None
    peak_hour_count = hour_counter.most_common(1)[0][1] if hour_counter else 0
    hour_dist       = [hour_counter.get(h, 0) for h in range(24)]

    # ── Peak day ──────────────────────────────────────────────────────────
    dow_counter    = Counter(p.day_of_week for p in plays if p.day_of_week)
    peak_dow       = dow_counter.most_common(1)[0][0] if dow_counter else None
    peak_dow_count = dow_counter.most_common(1)[0][1] if dow_counter else 0

    # ── Mood breakdown ────────────────────────────────────────────────────
    mood_counter  = Counter(p.auto_mood for p in plays if p.auto_mood and p.auto_mood != "UNKNOWN")
    dominant_mood = mood_counter.most_common(1)[0][0] if mood_counter else "UNKNOWN"
    mood_variety  = len(mood_counter)

    # ── Genre ─────────────────────────────────────────────────────────────
    genre_counter = Counter()
    for p in plays:
        if p.primary_genre:
            for g in p.primary_genre.split(","):
                g = g.strip().lower()
                if g:
                    genre_counter[g] += 1
    top_genres    = [g for g, _ in genre_counter.most_common(5)]
    genre_variety = len(genre_counter)

    # ── Track breakdown ───────────────────────────────────────────────────
    track_plays = Counter()
    track_skips = Counter()
    track_art   = {}
    track_album = {}
    for p in plays:
        track_plays[p.track_name]  += 1
        track_art[p.track_name]     = p.album_art_url
        track_album[p.track_name]   = p.album
        if p.was_skipped:
            track_skips[p.track_name] += 1

    most_played_tracks = []
    for track, count in track_plays.most_common(10):
        skip_count = track_skips.get(track, 0)
        most_played_tracks.append({
            "name":      track,
            "plays":     count,
            "skips":     skip_count,
            "skip_rate": round(skip_count / count * 100, 1) if count else 0,
            "art":       track_art.get(track),
            "album":     track_album.get(track),
        })

    always_completed = [
        {"name": t, "plays": track_plays[t], "art": track_art.get(t)}
        for t in track_plays
        if track_skips.get(t, 0) == 0 and track_plays[t] >= 3
    ]
    always_completed = sorted(always_completed, key=lambda x: x["plays"], reverse=True)[:5]

    most_skipped_tracks = []
    for track, skip_count in track_skips.most_common(5):
        count = track_plays[track]
        most_skipped_tracks.append({
            "name":      track,
            "plays":     count,
            "skips":     skip_count,
            "skip_rate": round(skip_count / count * 100, 1) if count else 0,
            "art":       track_art.get(track),
        })

    # ── Most skipped track ────────────────────────────────────────────────
    most_skipped_track = track_skips.most_common(1)[0] if track_skips else None

    # ── First and last play ───────────────────────────────────────────────
    first_play = plays[0]
    last_play  = plays[-1]

    # ── Days since first play ─────────────────────────────────────────────
    days_since_first = (datetime.now(TIMEZONE).date() - first_play.listened_at.date()).days

    # ── Active days ───────────────────────────────────────────────────────
    active_days = len(days_with_plays)

    # ── Consistency ───────────────────────────────────────────────────────
    consistency = min(100.0, round(active_days / max(days_since_first, 1) * 100, 1)) if days_since_first > 0 else 100

    # ── Plays per active day ──────────────────────────────────────────────
    plays_per_day = round(total / active_days, 2) if active_days else 0

    # ── Average plays per week ────────────────────────────────────────────
    weeks          = max(days_since_first / 7, 1)
    plays_per_week = round(total / weeks, 1)

    # ── Binge days (5+ plays in one day) ──────────────────────────────────
    day_plays  = Counter(p.listened_at.strftime("%Y-%m-%d") for p in plays)
    binge_days = sum(1 for c in day_plays.values() if c >= 5)

    # ── Busiest day ───────────────────────────────────────────────────────
    busiest_day       = day_plays.most_common(1)[0] if day_plays else None
    busiest_day_val   = busiest_day[0] if busiest_day else None
    busiest_day_count = busiest_day[1] if busiest_day else 0

    # ── Albums ────────────────────────────────────────────────────────────
    album_counter = Counter(p.album for p in plays if p.album)
    top_album     = album_counter.most_common(1)[0] if album_counter else None
    unique_albums = len(album_counter)
    unique_tracks_count = len(set(p.track_id for p in plays))

    # ── Average progress ──────────────────────────────────────────────────
    avg_progress = round(sum(p.progress_pct or 0 for p in plays) / total, 1) if total else 0

    # ── Average skip point ────────────────────────────────────────────────
    skip_plays     = [p for p in plays if p.was_skipped]
    avg_skip_point = round(sum(p.progress_pct or 0 for p in skip_plays) / len(skip_plays), 1) if skip_plays else None

    # ── Night owl / morning / weekend ─────────────────────────────────────
    night_pct   = round(sum(1 for p in plays if p.hour_of_day is not None and 0 <= p.hour_of_day < 4) / total * 100, 1) if total else 0
    morning_pct = round(sum(1 for p in plays if p.hour_of_day is not None and 5 <= p.hour_of_day < 10) / total * 100, 1) if total else 0
    weekend_pct = round(sum(1 for p in plays if p.day_of_week in ["Saturday", "Sunday"]) / total * 100, 1) if total else 0

    # ── Comebacks (listened after 7+ day gap) ─────────────────────────────
    comebacks = 0
    for i in range(1, len(days_with_plays)):
        if (days_with_plays[i] - days_with_plays[i-1]).days >= 7:
            comebacks += 1

    # ── Loyalty score ─────────────────────────────────────────────────────
    loyalty = round(
        (comp_rate * 0.4) +
        (min(total, 100) / 100 * 30) +
        (min(max_streak, 30) / 30 * 20) +
        (min(active_days, 50) / 50 * 10),
        1
    )

    # ── Time of day breakdown ─────────────────────────────────────────────
    time_buckets = {
        "morning":   sum(1 for p in plays if p.hour_of_day is not None and 5  <= p.hour_of_day < 12),
        "afternoon": sum(1 for p in plays if p.hour_of_day is not None and 12 <= p.hour_of_day < 17),
        "evening":   sum(1 for p in plays if p.hour_of_day is not None and 17 <= p.hour_of_day < 21),
        "night":     sum(1 for p in plays if p.hour_of_day is not None and (21 <= p.hour_of_day or p.hour_of_day < 5)),
    }

    # ── Longest gap ───────────────────────────────────────────────────────
    longest_gap = 0
    for i in range(1, len(days_with_plays)):
        gap         = (days_with_plays[i] - days_with_plays[i-1]).days
        longest_gap = max(longest_gap, gap)

    # ── Most played month ─────────────────────────────────────────────────
    monthly              = Counter(p.listened_at.strftime("%Y-%m") for p in plays)
    sorted_months        = sorted(monthly.keys())
    monthly_labels       = sorted_months
    monthly_values       = [monthly[m] for m in sorted_months]
    most_played_month    = monthly.most_common(1)[0] if monthly else None

    # ── Monthly skip rates ────────────────────────────────────────────────
    monthly_skip_rates = []
    for month in sorted_months:
        month_plays = [p for p in plays if p.listened_at.strftime("%Y-%m") == month]
        month_skips = sum(1 for p in month_plays if p.was_skipped)
        rate        = round(month_skips / len(month_plays) * 100, 1) if month_plays else 0
        monthly_skip_rates.append(rate)

    # ── Skip streaks ──────────────────────────────────────────────────────
    skip_streak = max_skip_streak = 0
    for p in plays:
        if p.was_skipped:
            skip_streak += 1
            max_skip_streak = max(max_skip_streak, skip_streak)
        else:
            skip_streak = 0

    # ── Complete streaks ──────────────────────────────────────────────────
    comp_streak = max_comp_streak = 0
    for p in plays:
        if not p.was_skipped:
            comp_streak += 1
            max_comp_streak = max(max_comp_streak, comp_streak)
        else:
            comp_streak = 0

    # ── Rediscoveries ─────────────────────────────────────────────────────
    rediscoveries = 0
    if len(sorted_months) > 1:
        for i in range(1, len(sorted_months)):
            prev      = sorted_months[i-1]
            curr      = sorted_months[i]
            py, pm    = int(prev[:4]), int(prev[5:])
            cy, cm    = int(curr[:4]), int(curr[5:])
            month_gap = (cy - py) * 12 + (cm - pm)
            if month_gap > 1:
                rediscoveries += 1

    # ── Album art ─────────────────────────────────────────────────────────
    album_art = next((p.album_art_url for p in reversed(plays) if p.album_art_url), None)

    # ── Artist image from cache ───────────────────────────────────────────
    artist_image = None
    cache_q = select(ArtistCache).where(
        ArtistCache.artist_name == artist_name,
        ArtistCache.track_name  == None,
        ArtistCache.image_url   != None,
    ).limit(1)
    cached = (await db.execute(cache_q)).scalar_one_or_none()
    if cached:
        artist_image = cached.image_url

    # ── Top co-listened artists ───────────────────────────────────────────
    from datetime import timedelta as td
    co_artists = Counter()
    for p in plays:
        window_start = p.listened_at - td(minutes=30)
        window_end   = p.listened_at + td(minutes=30)
        nearby_q     = (
            select(TrackPlay.artists)
            .where(TrackPlay.listened_at >= window_start)
            .where(TrackPlay.listened_at <= window_end)
            .where(TrackPlay.artists != p.artists)
        )
        nearby = (await db.execute(nearby_q)).fetchall()
        for row in nearby:
            if row.artists:
                for a in row.artists.split(","):
                    a = a.strip()
                    if a and a.lower() != artist_name.lower():
                        co_artists[a] += 1

    top_co_artists = [{"name": a, "count": c} for a, c in co_artists.most_common(8)]

    # ── Unique albums count ───────────────────────────────────────────────
    unique_albums_count = len(set(p.album for p in plays if p.album))

    return templates.TemplateResponse("artist_dive.html", {
        "request":             request,
        "artist_name":         artist_name,
        "found":               True,
        "total":               total,
        "skips":               skips,
        "completes":           completes,
        "skip_rate":           skip_rate,
        "comp_rate":           comp_rate,
        "minutes":             minutes,
        "hours":               hours,
        "max_streak":          max_streak,
        "current_streak":      current_streak,
        "peak_hour":           peak_hour,
        "peak_hour_count":     peak_hour_count,
        "peak_dow":            peak_dow,
        "peak_dow_count":      peak_dow_count,
        "dominant_mood":       dominant_mood,
        "mood_variety":        mood_variety,
        "mood_breakdown":      dict(mood_counter.most_common(8)),
        "top_genres":          top_genres,
        "genre_variety":       genre_variety,
        "most_played_tracks":  most_played_tracks,
        "always_completed":    always_completed,
        "most_skipped_tracks": most_skipped_tracks,
        "most_skipped_track":  most_skipped_track,
        "top_co_artists":      top_co_artists,
        "monthly_labels":      monthly_labels,
        "monthly_values":      monthly_values,
        "monthly_skip_rates":  monthly_skip_rates,
        "hour_dist":           hour_dist,
        "artist_image":        artist_image,
        "unique_albums":       unique_albums_count,
        "unique_tracks": unique_tracks_count,
        "first_play":          first_play,
        "last_play":           last_play,
        "days_since_first":    days_since_first,
        "active_days":         active_days,
        "consistency":         consistency,
        "plays_per_day":       plays_per_day,
        "plays_per_week":      plays_per_week,
        "binge_days":          binge_days,
        "busiest_day":         busiest_day_val,
        "busiest_day_count":   busiest_day_count,
        "top_album":           top_album[0] if top_album else None,
        "avg_progress":        avg_progress,
        "avg_skip_point":      avg_skip_point,
        "night_pct":           night_pct,
        "morning_pct":         morning_pct,
        "weekend_pct":         weekend_pct,
        "comebacks":           comebacks,
        "loyalty":             loyalty,
        "time_buckets":        time_buckets,
        "longest_gap":         longest_gap,
        "most_played_month":   most_played_month[0] if most_played_month else None,
        "most_played_month_count": most_played_month[1] if most_played_month else 0,
        "max_skip_streak":     max_skip_streak,
        "max_comp_streak":     max_comp_streak,
        "rediscoveries":       rediscoveries,
    })





@app.get("/artist-search", response_class=HTMLResponse)
async def artist_search(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    all_artists_q   = select(TrackPlay.artists)
    all_artist_rows = (await db.execute(all_artists_q)).fetchall()
    artist_counter  = Counter()
    for row in all_artist_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    artist_counter[a] += 1

    artists = sorted(
        [{"name": a, "plays": c} for a, c in artist_counter.items()],
        key=lambda x: x["plays"],
        reverse=True
    )

    import json
    return templates.TemplateResponse("artist_search.html", {
        "request": request,
        "artists": json.dumps(artists),
        "count":   len(artists),
    })


@app.get("/debug-artist-ids")
async def debug_artist_ids(db: AsyncSession = Depends(get_db)):
    all_artists_q   = select(TrackPlay.artists)
    all_artist_rows = (await db.execute(all_artists_q)).fetchall()
    artist_counter  = Counter()
    for row in all_artist_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    artist_counter[a] += 1

    artist_id_q    = (
        select(TrackPlay.artists, TrackPlay.artist_spotify_id)
        .where(TrackPlay.artist_spotify_id != None)
        .group_by(TrackPlay.artists)
    )
    artist_id_rows = (await db.execute(artist_id_q)).fetchall()
    artist_id_map  = {}
    for row in artist_id_rows:
        if row.artists:
            primary = row.artists.split(",")[0].strip()
            if primary not in artist_id_map:
                artist_id_map[primary] = row.artist_spotify_id

    missing = [a for a in artist_counter if a not in artist_id_map]
    return {
        "total_artists":   len(artist_counter),
        "have_id":         len(artist_id_map),
        "missing_id":      len(missing),
        "missing_artists": sorted(missing),
    }

@app.get("/debug-fetch-genre/{artist_name}")
async def debug_fetch_genre(artist_name: str, db: AsyncSession = Depends(get_db)):
    import urllib.parse
    artist_name  = urllib.parse.unquote(artist_name)
    LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
    encoded      = urllib.parse.quote(artist_name)
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"https://ws.audioscrobbler.com/2.0/?method=artist.gettoptags"
            f"&artist={encoded}&api_key={LASTFM_API_KEY}&format=json"
        )
    if resp.status_code == 200:
        tags = resp.json().get("toptags", {}).get("tag", [])
        return {
            "all_tags": [{"name": t["name"], "count": t.get("count",0)} for t in tags],
            "strong_tags": [t["name"] for t in tags if t.get("count", 0) >= 10],
        }
    return {"error": resp.status_code}

@app.get("/debug-fetch-discogs/{artist_name}")
async def debug_fetch_discogs(artist_name: str):
    import urllib.parse
    DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN")
    encoded       = urllib.parse.quote(artist_name)
    headers       = {
        "User-Agent":    "RaveFM/1.0",
        "Authorization": f"Discogs token={DISCOGS_TOKEN}",
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        search_resp = await client.get(
            f"https://api.discogs.com/database/search?q={encoded}&type=artist",
            headers=headers,
        )
    if search_resp.status_code != 200:
        return {"error": search_resp.status_code}

    results = search_resp.json().get("results", [])
    if not results:
        return {"error": "no results"}

    artist_id = None
    for r in results[:3]:
        if r.get("title", "").lower() == artist_name.lower():
            artist_id = r["id"]
            break
    if not artist_id:
        artist_id = results[0]["id"]

    async with httpx.AsyncClient(timeout=8.0) as client:
        releases_resp = await client.get(
            f"https://api.discogs.com/artists/{artist_id}/releases"
            f"?per_page=10&sort=year&sort_order=desc",
            headers=headers,
        )

    releases = releases_resp.json().get("releases", [])
    master_id = None
    for r in releases:
        if r.get("type") == "master" and r.get("role") == "Main":
            master_id = r["id"]
            break

    if not master_id:
        return {"error": "no master found", "releases": releases[:3]}

    async with httpx.AsyncClient(timeout=8.0) as client:
        master_resp = await client.get(
            f"https://api.discogs.com/masters/{master_id}",
            headers=headers,
        )

    master_data = master_resp.json()
    return {
        "artist_id":  artist_id,
        "master_id":  master_id,
        "title":      master_data.get("title"),
        "styles":     master_data.get("styles", []),
        "genres":     master_data.get("genres", []),
    }

@app.get("/debug-discogs-artist/{artist_name}")
async def debug_discogs_artist(artist_name: str):
    import urllib.parse
    DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN")
    encoded       = urllib.parse.quote(artist_name)
    headers       = {
        "User-Agent":    "RaveFM/1.0",
        "Authorization": f"Discogs token={DISCOGS_TOKEN}",
    }
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(
            f"https://api.discogs.com/database/search?q={encoded}&type=artist",
            headers=headers,
        )
    if resp.status_code != 200:
        return {"error": resp.status_code}

    results = resp.json().get("results", [])
    return {
        "searching_for": artist_name,
        "all_results": [
            {
                "id":    r.get("id"),
                "title": r.get("title"),
                "uri":   r.get("uri"),
                "thumb": r.get("thumb"),
            }
            for r in results[:10]
        ]
    }

@app.get("/debug-filter-test")
async def debug_filter_test():
    from tracker import filter_implausible_genres

    test1_input  = ["Heavy Metal", "Hip Hop", "Electronic", "Funk"]
    test1_output = filter_implausible_genres(test1_input)

    test2_input  = ["Heavy Metal"]
    test2_output = filter_implausible_genres(test2_input)

    test3_input  = ["Heavy Metal", "dubstep", "glitch-hop"]
    test3_output = filter_implausible_genres(test3_input)

    return {
        "test1": {"input": test1_input,  "output": test1_output},
        "test2": {"input": test2_input,  "output": test2_output},
        "test3": {"input": test3_input,  "output": test3_output},
    }

@app.get("/clear-genre-cache")
async def clear_genre_cache(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import delete
    await db.execute(delete(ArtistCache))
    await db.commit()
    return {"message": "Genre cache cleared"}

@app.get("/cleanup-bad-genres")
async def cleanup_bad_genres(db: AsyncSession = Depends(get_db)):
    from tracker import filter_implausible_genres

    # Get all plays that have a primary_genre
    plays_q = select(TrackPlay).where(TrackPlay.primary_genre != None)
    plays   = (await db.execute(plays_q)).scalars().fetchall()

    updated = 0
    for play in plays:
        if not play.primary_genre:
            continue

        genre_list    = [g.strip() for g in play.primary_genre.split(",") if g.strip()]
        filtered      = filter_implausible_genres(genre_list)
        filtered_str  = ", ".join(filtered)

        if filtered_str != play.primary_genre:
            print(f"[CLEANUP] {play.track_name} by {play.artists}: '{play.primary_genre}' -> '{filtered_str}'")
            play.primary_genre = filtered_str
            updated += 1

    await db.commit()
    return {
        "message": f"Updated {updated} tracks",
        "total_checked": len(plays),
    }

@app.get("/compare", response_class=HTMLResponse)
async def compare_artists(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    all_artists_q   = select(TrackPlay.artists)
    all_artist_rows = (await db.execute(all_artists_q)).fetchall()
    artist_counter  = Counter()
    for row in all_artist_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    artist_counter[a] += 1

    eligible_artists = sorted(
        [{"name": a, "plays": c} for a, c in artist_counter.items() if c >= 2],
        key=lambda x: x["plays"],
        reverse=True
    )

    import json
    return templates.TemplateResponse("compare.html", {
        "request":          request,
        "eligible_artists": json.dumps(eligible_artists),
    })


@app.post("/compare", response_class=HTMLResponse)
async def compare_artists_result(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    form    = await request.form()
    artist1 = form.get("artist1", "").strip()
    artist2 = form.get("artist2", "").strip()

    all_artists_q   = select(TrackPlay.artists)
    all_artist_rows = (await db.execute(all_artists_q)).fetchall()
    artist_counter  = Counter()
    for row in all_artist_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    artist_counter[a] += 1

    eligible_artists = sorted(
        [{"name": a, "plays": c} for a, c in artist_counter.items() if c >= 2],
        key=lambda x: x["plays"],
        reverse=True
    )

    async def get_full_stats(artist_name: str) -> dict:
        plays_q = (
            select(TrackPlay)
            .where(TrackPlay.artists.ilike(f"%{artist_name}%"))
            .order_by(TrackPlay.listened_at.asc())
        )
        plays = (await db.execute(plays_q)).scalars().fetchall()

        # Filter to only plays where artist is actually listed
        plays = [
            p for p in plays
            if any(a.strip().lower() == artist_name.lower()
                   for a in (p.artists or "").split(","))
        ]

        if not plays:
            return None

        total      = len(plays)
        skips      = sum(1 for p in plays if p.was_skipped)
        completes  = total - skips
        skip_rate  = round(skips / total * 100, 1) if total else 0
        comp_rate  = round(completes / total * 100, 1) if total else 0
        minutes    = round(sum(p.progress_ms for p in plays) / 60000, 1)
        hours      = round(minutes / 60, 2)

        # Streak
        days_with  = sorted(set(p.listened_at.date() for p in plays))
        max_streak = temp = 1
        for i in range(1, len(days_with)):
            if (days_with[i] - days_with[i-1]).days == 1:
                temp += 1
                max_streak = max(max_streak, temp)
            else:
                temp = 1

        # Peak hour
        hour_counter = Counter(p.hour_of_day for p in plays if p.hour_of_day is not None)
        peak_hour    = hour_counter.most_common(1)[0][0] if hour_counter else None
        peak_hour_count = hour_counter.most_common(1)[0][1] if hour_counter else 0

        # Peak day
        dow_counter  = Counter(p.day_of_week for p in plays if p.day_of_week)
        peak_dow     = dow_counter.most_common(1)[0][0] if dow_counter else None
        peak_dow_count = dow_counter.most_common(1)[0][1] if dow_counter else 0

        # Mood
        mood_counter  = Counter(p.auto_mood for p in plays if p.auto_mood and p.auto_mood != "UNKNOWN")
        dominant_mood = mood_counter.most_common(1)[0][0] if mood_counter else "UNKNOWN"
        mood_variety  = len(mood_counter)

        # Genre
        genre_counter = Counter()
        for p in plays:
            if p.primary_genre:
                for g in p.primary_genre.split(","):
                    g = g.strip().lower()
                    if g:
                        genre_counter[g] += 1
        top_genre    = genre_counter.most_common(1)[0][0] if genre_counter else None
        genre_variety = len(genre_counter)

        # Track stats
        track_plays  = Counter(p.track_name for p in plays)
        track_skips  = Counter(p.track_name for p in plays if p.was_skipped)
        top_track    = track_plays.most_common(1)[0] if track_plays else None
        unique_tracks = len(track_plays)

        # Most skipped track
        most_skipped_track = track_skips.most_common(1)[0] if track_skips else None

        # Avg progress
        avg_progress = round(sum(p.progress_pct or 0 for p in plays) / total, 1) if total else 0

        # First and last play
        first_play = plays[0]
        last_play  = plays[-1]

        # Days since first play
        from datetime import date as date_type
        days_since_first = (datetime.now(TIMEZONE).date() - first_play.listened_at.date()).days

        # Active days
        active_days = len(days_with)

        # Plays per active day
        plays_per_day = round(total / active_days, 2) if active_days else 0

        # Binge sessions — days with 5+ plays
        day_plays = Counter(p.listened_at.strftime("%Y-%m-%d") for p in plays)
        binge_days = sum(1 for c in day_plays.values() if c >= 5)

        # Most played in one day
        busiest_day     = day_plays.most_common(1)[0] if day_plays else None
        busiest_day_count = busiest_day[1] if busiest_day else 0

        # Albums
        album_counter = Counter(p.album for p in plays if p.album)
        top_album     = album_counter.most_common(1)[0] if album_counter else None
        unique_albums = len(album_counter)

        # Loyalty score — custom formula
        # High plays + low skip rate + long streak = high loyalty
        loyalty = round(
            (comp_rate * 0.4) +
            (min(total, 100) / 100 * 30) +
            (min(max_streak, 30) / 30 * 20) +
            (min(active_days, 50) / 50 * 10),
            1
        )

        # Consistency — how regularly you listen (active days / days since first)
        consistency = min(100.0, round(active_days / max(days_since_first, 1) * 100, 1)) if days_since_first > 0 else 100

        # Night owl score — % of plays between midnight and 4am
        night_plays  = sum(1 for p in plays if p.hour_of_day is not None and 0 <= p.hour_of_day < 4)
        night_pct    = round(night_plays / total * 100, 1) if total else 0

        # Morning person score
        morning_plays = sum(1 for p in plays if p.hour_of_day is not None and 5 <= p.hour_of_day < 10)
        morning_pct   = round(morning_plays / total * 100, 1) if total else 0

        # Weekend vs weekday
        weekend_plays = sum(1 for p in plays if p.day_of_week in ["Saturday", "Sunday"])
        weekend_pct   = round(weekend_plays / total * 100, 1) if total else 0

        # Skip early vs late — avg skip point
        skip_plays    = [p for p in plays if p.was_skipped]
        avg_skip_point = round(sum(p.progress_pct or 0 for p in skip_plays) / len(skip_plays), 1) if skip_plays else None

        # Comeback score — listened to after a gap of 7+ days
        comebacks = 0
        for i in range(1, len(days_with)):
            if (days_with[i] - days_with[i-1]).days >= 7:
                comebacks += 1

        # Album art for display
        album_art = next((p.album_art_url for p in reversed(plays) if p.album_art_url), None)

        # Monthly trend — plays per month
        monthly = Counter(p.listened_at.strftime("%Y-%m") for p in plays)
        sorted_months  = sorted(monthly.keys())
        monthly_labels = sorted_months
        monthly_values = [monthly[m] for m in sorted_months]

        # Hour distribution
        hour_dist = [hour_counter.get(h, 0) for h in range(24)]

        return {
            "name":              artist_name,
            "total":             total,
            "skips":             skips,
            "completes":         completes,
            "skip_rate":         skip_rate,
            "comp_rate":         comp_rate,
            "minutes":           minutes,
            "hours":             hours,
            "max_streak":        max_streak,
            "peak_hour":         peak_hour,
            "peak_hour_count":   peak_hour_count,
            "peak_dow":          peak_dow,
            "peak_dow_count":    peak_dow_count,
            "dominant_mood":     dominant_mood,
            "mood_variety":      mood_variety,
            "top_genre":         top_genre,
            "genre_variety":     genre_variety,
            "top_track":         top_track,
            "unique_tracks":     unique_tracks,
            "most_skipped_track": most_skipped_track,
            "avg_progress":      avg_progress,
            "avg_skip_point":    avg_skip_point,
            "first_play":        first_play.listened_at.strftime("%Y-%m-%d"),
            "last_play":         last_play.listened_at.strftime("%Y-%m-%d"),
            "days_since_first":  days_since_first,
            "active_days":       active_days,
            "plays_per_day":     plays_per_day,
            "binge_days":        binge_days,
            "busiest_day":       busiest_day[0] if busiest_day else None,
            "busiest_day_count": busiest_day_count,
            "top_album":         top_album[0] if top_album else None,
            "unique_albums":     unique_albums,
            "loyalty":           loyalty,
            "consistency":       consistency,
            "night_pct":         night_pct,
            "morning_pct":       morning_pct,
            "weekend_pct":       weekend_pct,
            "comebacks":         comebacks,
            "album_art":         album_art,
            "monthly_labels":    monthly_labels,
            "monthly_values":    monthly_values,
            "hour_dist":         hour_dist,
            "mood_counts":       dict(mood_counter.most_common(6)),
        }

    stats1 = await get_full_stats(artist1)
    stats2 = await get_full_stats(artist2)

    # ── Graph distance ────────────────────────────────────────────────────
    graph_distance  = None
    connecting_songs = []

    if stats1 and stats2:
        # Build adjacency from shared tracks
        all_plays_q = (
            select(TrackPlay.track_name, TrackPlay.artists)
            .distinct(TrackPlay.track_id)
            .group_by(TrackPlay.track_id)
        )
        all_plays = (await db.execute(all_plays_q)).fetchall()

        from itertools import combinations
        adjacency = defaultdict(set)
        edge_songs = defaultdict(set)

        for play in all_plays:
            artists = [a.strip() for a in play.artists.split(",") if a.strip()]
            if len(artists) > 1:
                for a1, a2 in combinations(sorted(artists), 2):
                    adjacency[a1].add(a2)
                    adjacency[a2].add(a1)
                    edge_songs[(a1, a2)].add(play.track_name)
                    edge_songs[(a2, a1)].add(play.track_name)

        # Direct connection
        if artist2 in adjacency.get(artist1, set()):
            graph_distance   = 1
            key              = tuple(sorted([artist1, artist2]))
            connecting_songs = list(edge_songs.get(key, edge_songs.get((artist1, artist2), set())))

        else:
            # BFS to find shortest path
            from collections import deque
            queue   = deque([(artist1, [artist1])])
            visited = {artist1}
            found   = False

            while queue and not found:
                current, path = queue.popleft()
                for neighbor in adjacency.get(current, set()):
                    if neighbor == artist2:
                        graph_distance = len(path)
                        found          = True
                        # Build the connection chain
                        full_path = path + [artist2]
                        for i in range(len(full_path) - 1):
                            a, b     = full_path[i], full_path[i+1]
                            key      = tuple(sorted([a, b]))
                            songs    = list(edge_songs.get(key, edge_songs.get((a, b), set())))
                            connecting_songs.append({
                                "from":  a,
                                "to":    b,
                                "songs": songs[:3],
                            })
                        break
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append((neighbor, path + [neighbor]))

    import json
    return templates.TemplateResponse("compare.html", {
        "request":          request,
        "eligible_artists": json.dumps(eligible_artists),
        "artist1":          artist1,
        "artist2":          artist2,
        "stats1":           stats1,
        "stats2":           stats2,
        "graph_distance":   graph_distance,
        "connecting_songs": connecting_songs,
    })

@app.get("/sync-liked-songs")
async def sync_liked_songs(db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return {"error": "not authenticated"}

    all_tracks = []
    offset     = 0
    limit      = 50

    while True:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.spotify.com/v1/me/tracks?limit={limit}&offset={offset}",
                headers={"Authorization": f"Bearer {token_store['access_token']}"},
            )
        if resp.status_code != 200:
            break

        data  = resp.json()
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            track = item["track"]
            if not track:
                continue
            all_tracks.append({
                "track_id":      track["id"],
                "track_name":    track["name"],
                "artists":       ", ".join(a["name"] for a in track["artists"]),
                "album":         track["album"]["name"],
                "album_art_url": track["album"]["images"][0]["url"] if track["album"]["images"] else None,
                "duration_ms":   track["duration_ms"],
                "added_at":      item["added_at"],
                "spotify_url":   track["external_urls"]["spotify"],
            })

        if len(items) < limit:
            break
        offset += limit

    # Upsert all tracks
    saved = 0
    for t in all_tracks:
        existing_q = select(LikedSong).where(LikedSong.track_id == t["track_id"])
        existing   = (await db.execute(existing_q)).scalar_one_or_none()

        from datetime import datetime as dt
        added_at = None
        try:
            added_at = dt.fromisoformat(t["added_at"].replace("Z", "+00:00"))
        except Exception:
            pass

        if existing:
            existing.track_name    = t["track_name"]
            existing.artists       = t["artists"]
            existing.album         = t["album"]
            existing.album_art_url = t["album_art_url"]
            existing.duration_ms   = t["duration_ms"]
            existing.added_at      = added_at
            existing.spotify_url   = t["spotify_url"]
        else:
            db.add(LikedSong(
                track_id      = t["track_id"],
                track_name    = t["track_name"],
                artists       = t["artists"],
                album         = t["album"],
                album_art_url = t["album_art_url"],
                duration_ms   = t["duration_ms"],
                added_at      = added_at,
                spotify_url   = t["spotify_url"],
            ))
            saved += 1

    await db.commit()
    return {"synced": len(all_tracks), "new": saved}

@app.get("/liked-songs", response_class=HTMLResponse)
async def liked_songs(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    # Check if we have data
    total_q = select(func.count()).select_from(LikedSong)
    total   = await db.scalar(total_q) or 0

    if total == 0:
        return templates.TemplateResponse("liked_songs.html", {
            "request": request,
            "total":   0,
            "synced":  False,
        })

    # Fetch all liked songs
    all_q    = select(LikedSong).order_by(LikedSong.added_at.asc())
    all_songs = (await db.execute(all_q)).scalars().fetchall()

    # ── Basic counts ──────────────────────────────────────────────────────
    total_songs   = len(all_songs)
    total_minutes = round(sum((s.duration_ms or 0) for s in all_songs) / 60000, 1)
    total_hours   = round(total_minutes / 60, 1)

    # ── Artists ───────────────────────────────────────────────────────────
    artist_counter = Counter()
    for s in all_songs:
        if s.artists:
            for a in s.artists.split(","):
                a = a.strip()
                if a:
                    artist_counter[a] += 1

    total_artists   = len(artist_counter)
    top_artists     = [{"name": a, "count": c} for a, c in artist_counter.most_common(15)]
    solo_artists    = sum(1 for c in artist_counter.values() if c == 1)
    repeat_artists  = total_artists - solo_artists

    # ── Albums ────────────────────────────────────────────────────────────
    album_counter  = Counter((s.album, s.artists.split(",")[0].strip() if s.artists else "") for s in all_songs)
    total_albums   = len(album_counter)
    top_albums     = [{"album": k[0], "artist": k[1], "count": v} for k, v in album_counter.most_common(10)]



    # ── Moods ─────────────────────────────────────────────────────────────
    mood_counter  = Counter(s.auto_mood for s in all_songs if s.auto_mood and s.auto_mood != "UNKNOWN")
    top_moods     = [{"mood": m, "count": c} for m, c in mood_counter.most_common(10)]
    dominant_mood = mood_counter.most_common(1)[0][0] if mood_counter else "UNKNOWN"

    # ── Added at patterns ─────────────────────────────────────────────────
    songs_with_date = [s for s in all_songs if s.added_at]

    hour_counter = Counter(s.added_at.hour for s in songs_with_date)
    dow_counter  = Counter(s.added_at.strftime("%A") for s in songs_with_date)
    month_counter = Counter(s.added_at.strftime("%Y-%m") for s in songs_with_date)

    hour_dist    = [hour_counter.get(h, 0) for h in range(24)]
    dow_order    = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    dow_dist     = [dow_counter.get(d, 0) for d in dow_order]

    sorted_months  = sorted(month_counter.keys())
    monthly_labels = sorted_months
    monthly_values = [month_counter[m] for m in sorted_months]

    peak_add_hour = hour_counter.most_common(1)[0][0] if hour_counter else None
    peak_add_dow  = dow_counter.most_common(1)[0][0] if dow_counter else None

    # ── First and latest liked ────────────────────────────────────────────
    first_liked  = songs_with_date[0]  if songs_with_date else None
    latest_liked = songs_with_date[-1] if songs_with_date else None

    # ── Library age ───────────────────────────────────────────────────────
    if first_liked:
        from datetime import timezone as tz
        first_dt      = first_liked.added_at.replace(tzinfo=tz.utc) if first_liked.added_at.tzinfo is None else first_liked.added_at
        library_age   = (datetime.now(TIMEZONE) - first_dt.astimezone(TIMEZONE)).days
    else:
        library_age   = 0

    # ── Songs added per period ────────────────────────────────────────────
    adds_per_month = round(total_songs / max(len(sorted_months), 1), 1)

    # ── Longest adding streak (consecutive days) ──────────────────────────
    add_days = sorted(set(s.added_at.date() for s in songs_with_date))
    add_streak = max_add_streak = 1
    for i in range(1, len(add_days)):
        if (add_days[i] - add_days[i-1]).days == 1:
            add_streak += 1
            max_add_streak = max(max_add_streak, add_streak)
        else:
            add_streak = 1

    # ── Cross-reference with play history ────────────────────────────────
    liked_ids_q  = select(LikedSong.track_id)
    liked_ids    = set(r.track_id for r in (await db.execute(liked_ids_q)).fetchall())

    played_q     = select(TrackPlay.track_id).where(TrackPlay.track_id.in_(liked_ids))
    played_ids   = set(r.track_id for r in (await db.execute(played_q)).fetchall())

    never_played      = liked_ids - played_ids
    played_liked      = liked_ids & played_ids
    never_played_pct  = round(len(never_played) / total_songs * 100, 1) if total_songs else 0
    played_liked_pct  = round(len(played_liked) / total_songs * 100, 1) if total_songs else 0

    # ── Most skipped liked songs ──────────────────────────────────────────
    skipped_liked_q = (
        select(TrackPlay.track_id, TrackPlay.track_name, TrackPlay.artists,
               TrackPlay.album_art_url,
               func.count().label("plays"),
               func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)).label("skips"))
        .where(TrackPlay.track_id.in_(liked_ids))
        .group_by(TrackPlay.track_id)
        .having(func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)) > 0)
        .order_by((func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)) * 1.0 / func.count()).desc())
        .limit(5)
    )
    skipped_liked = (await db.execute(skipped_liked_q)).fetchall()

    # ── Recently added ────────────────────────────────────────────────────
    recent_liked = sorted(songs_with_date, key=lambda s: s.added_at, reverse=True)[:20]


    # ── Average song duration ─────────────────────────────────────────────
    durations     = [s.duration_ms for s in all_songs if s.duration_ms]
    avg_duration  = round(sum(durations) / len(durations) / 1000, 1) if durations else 0
    avg_duration_fmt = f"{int(avg_duration // 60)}:{int(avg_duration % 60):02d}" if avg_duration else "—"

    return templates.TemplateResponse("liked_songs.html", {
        "request":          request,
        "synced":           True,
        "total_songs":      total_songs,
        "total_hours":      total_hours,
        "total_minutes":    total_minutes,
        "total_artists":    total_artists,
        "total_albums":     total_albums,
        "top_artists":    top_artists,
        "all_artists":    [{"name": a, "count": c} for a, c in artist_counter.most_common()],
        "top_albums":       top_albums,
        "top_moods":        top_moods,
        "dominant_mood":    dominant_mood,
        "hour_dist":        hour_dist,
        "dow_dist":         dow_dist,
        "dow_order":        dow_order,
        "monthly_labels":   monthly_labels,
        "monthly_values":   monthly_values,
        "peak_add_hour":    peak_add_hour,
        "peak_add_dow":     peak_add_dow,
        "first_liked":      first_liked,
        "latest_liked":     latest_liked,
        "library_age":      library_age,
        "adds_per_month":   adds_per_month,
        "max_add_streak":   max_add_streak,
        "never_played":     len(never_played),
        "never_played_pct": never_played_pct,
        "played_liked":     len(played_liked),
        "played_liked_pct": played_liked_pct,
        "skipped_liked":    skipped_liked,
        "recent_liked":     recent_liked,
        "avg_duration_fmt": avg_duration_fmt,
        "solo_artists":     solo_artists,
        "repeat_artists":   repeat_artists,
        "mood_counter":     dict(mood_counter),
    })


@app.get("/liked-songs-graph", response_class=HTMLResponse)
async def liked_songs_graph(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    from itertools import combinations
    from datetime import timedelta as td2

    all_q     = select(LikedSong).order_by(LikedSong.added_at.asc())
    all_songs = (await db.execute(all_q)).scalars().fetchall()

    if not all_songs:
        return templates.TemplateResponse("artist_graph.html", {
            "request":     request,
            "graph_data":  '{"nodes": [], "links": []}',
            "node_count":  0,
            "link_count":  0,
            "graph_title": "LIKED SONGS GRAPH",
            "graph_sub":   "◈ NO DATA YET — SYNC YOUR LIBRARY FIRST ◈",
            "plays_label": "liked songs",
        })

    connections  = defaultdict(lambda: {"count": 0, "tracks": set()})
    artist_count = Counter()

    for song in all_songs:
        if not song.artists:
            continue
        artists = [a.strip() for a in song.artists.split(",") if a.strip()]
        for a in artists:
            artist_count[a] += 1
        if len(artists) > 1:
            for a1, a2 in combinations(sorted(artists), 2):
                key = (a1, a2)
                connections[key]["count"]  += 1
                connections[key]["tracks"].add(song.track_name)

    # Only include artists that have connections OR appear on 3+ liked songs
    # connected_artists_set = set()
    # for (a1, a2) in connections:
    #     connected_artists_set.add(a1)
    #     connected_artists_set.add(a2)

    # all_artists = {
    #     a for a in artist_count
    #     if a in connected_artists_set or artist_count[a] >= 3
    # }

    # Include all artists from liked songs
    connected_artists_set = set()
    for (a1, a2) in connections:
        connected_artists_set.add(a1)
        connected_artists_set.add(a2)

    all_artists = set(artist_count.keys())

    print(f"[LIKED GRAPH] Filtered from {len(artist_count)} to {len(all_artists)} artists")

    if not all_artists:
        return templates.TemplateResponse("artist_graph.html", {
            "request":     request,
            "graph_data":  '{"nodes": [], "links": []}',
            "node_count":  0,
            "link_count":  0,
            "graph_title": "LIKED SONGS GRAPH",
            "graph_sub":   "◈ NO CONNECTIONS FOUND ◈",
            "plays_label": "liked songs",
        })

    # Build ID map from ArtistSpotifyID table
    artist_id_map = {}
    id_table_q    = select(ArtistSpotifyID)
    id_table_rows = (await db.execute(id_table_q)).fetchall()
    for row in id_table_rows:
        artist_id_map[row[0].artist_name] = row[0].spotify_id

    artist_images = {}

    async def fetch_artist_image(artist_name: str):
        # Check cache first
        async with AsyncSessionLocal() as cache_db:
            cache_q = select(ArtistCache).where(
                ArtistCache.artist_name == artist_name,
                ArtistCache.track_name  == None,
                ArtistCache.image_url   != None,
                ArtistCache.fetched_at  >= datetime.now(TIMEZONE) - td2(days=7),
            ).limit(1)
            cached = (await cache_db.execute(cache_q)).scalar_one_or_none()
            if cached and cached.image_url:
                artist_images[artist_name] = cached.image_url
                return

        async def save_cache(name: str, image_url: str):
            if not image_url:
                return
            async with AsyncSessionLocal() as cache_db:
                existing_q = select(ArtistCache).where(
                    ArtistCache.artist_name == name,
                    ArtistCache.track_name  == None,
                ).limit(1)
                existing = (await cache_db.execute(existing_q)).scalar_one_or_none()
                if existing:
                    existing.image_url = image_url
                    await cache_db.commit()
                else:
                    cache_db.add(ArtistCache(
                        artist_name = name,
                        track_name  = None,
                        image_url   = image_url,
                    ))
                    await cache_db.commit()

        try:
            spotify_id = artist_id_map.get(artist_name)

            if spotify_id:
                # Direct ID lookup — fastest and most accurate
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(
                        f"https://api.spotify.com/v1/artists/{spotify_id}",
                        headers={"Authorization": f"Bearer {token_store['access_token']}"},
                    )
                if resp.status_code == 200:
                    images    = resp.json().get("images", [])
                    image_url = images[-1]["url"] if images else None
                    artist_images[artist_name] = image_url
                    await save_cache(artist_name, image_url)
                    return
                elif resp.status_code == 429:
                    artist_images[artist_name] = None
                    return

            # Fall back to search — exact name match only
            encoded = urllib.parse.quote(artist_name)
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"https://api.spotify.com/v1/search?q={encoded}&type=artist&limit=5",
                    headers={"Authorization": f"Bearer {token_store['access_token']}"},
                )

            if resp.status_code == 429:
                artist_images[artist_name] = None
                return

            if resp.status_code == 200:
                items        = resp.json().get("artists", {}).get("items", [])
                artist_lower = artist_name.lower().strip()
                matched      = None

                for item in items:
                    if item["name"].lower().strip() == artist_lower:
                        matched = item
                        break

                if matched:
                    images    = matched.get("images", [])
                    image_url = images[-1]["url"] if images else None
                    artist_images[artist_name] = image_url
                    await save_cache(artist_name, image_url)

                    # Store Spotify ID for future use
                    try:
                        async with AsyncSessionLocal() as id_db:
                            existing_q = select(ArtistSpotifyID).where(
                                ArtistSpotifyID.artist_name == artist_name
                            )
                            existing = (await id_db.execute(existing_q)).scalar_one_or_none()
                            if not existing:
                                id_db.add(ArtistSpotifyID(
                                    artist_name = artist_name,
                                    spotify_id  = matched["id"],
                                ))
                                await id_db.commit()
                    except Exception:
                        pass
                else:
                    artist_images[artist_name] = None

        except Exception:
            artist_images[artist_name] = None

    # Process in batches — known IDs first since they're faster
    known_id_artists   = [a for a in all_artists if a in artist_id_map]
    unknown_id_artists = [a for a in all_artists if a not in artist_id_map]
    artist_list        = known_id_artists + unknown_id_artists

    batch_size = 5
    for i in range(0, len(artist_list), batch_size):
        batch          = artist_list[i:i + batch_size]
        is_known_batch = all(a in artist_id_map for a in batch)
        await asyncio.gather(*[fetch_artist_image(a) for a in batch])
        await asyncio.sleep(0.1 if is_known_batch else 0.3)

    # Build nodes — only for filtered artists
    nodes = []
    for artist in all_artists:
        nodes.append({
            "id":        artist,
            "image":     artist_images.get(artist),
            "plays":     artist_count.get(artist, 1),
            "connected": artist in connected_artists_set,
        })

    # Build links — only for connections between included artists
    links = []
    for (a1, a2), data in connections.items():
        if a1 not in all_artists or a2 not in all_artists:
            continue
        seen          = set()
        unique_tracks = []
        for track in data["tracks"]:
            if track.lower().strip() not in seen:
                seen.add(track.lower().strip())
                unique_tracks.append(track)
        links.append({
            "source": a1,
            "target": a2,
            "count":  len(unique_tracks),
            "tracks": unique_tracks,
        })

    import json
    graph_data = json.dumps({"nodes": nodes, "links": links})

    return templates.TemplateResponse("artist_graph.html", {
        "request":     request,
        "graph_data":  graph_data,
        "node_count":  len(nodes),
        "link_count":  len(links),
        "graph_title": "LIKED SONGS GRAPH",
        "graph_sub":   f"◈ {len(nodes)} ARTISTS · {len(links)} CONNECTIONS ◈",
        "plays_label": "liked songs",
    })

@app.get("/liked-separation")
async def liked_separation(a1: str, a2: str, db: AsyncSession = Depends(get_db)):
    from itertools import combinations
    from collections import deque

    all_q     = select(LikedSong).where(LikedSong.artists != None)
    all_songs = (await db.execute(all_q)).scalars().fetchall()

    adjacency  = defaultdict(set)
    edge_songs = defaultdict(set)

    for song in all_songs:
        artists = [a.strip() for a in song.artists.split(",") if a.strip()]
        if len(artists) > 1:
            for x, y in combinations(sorted(artists), 2):
                adjacency[x].add(y)
                adjacency[y].add(x)
                edge_songs[(x, y)].add(song.track_name)
                edge_songs[(y, x)].add(song.track_name)

    if a1 not in adjacency and a2 not in adjacency:
        return {"found": False, "reason": "neither artist has collaborations in your liked songs"}

    if a2 in adjacency.get(a1, set()):
        key   = tuple(sorted([a1, a2]))
        songs = list(edge_songs.get(key, edge_songs.get((a1, a2), set())))
        return {
            "found":    True,
            "degrees":  1,
            "path":     [a1, a2],
            "chain":    [{"from": a1, "to": a2, "songs": songs[:5]}],
        }

    queue   = deque([(a1, [a1])])
    visited = {a1}

    while queue:
        current, path = queue.popleft()
        if len(path) > 7:
            break
        for neighbor in adjacency.get(current, set()):
            if neighbor == a2:
                full_path = path + [a2]
                chain     = []
                for i in range(len(full_path) - 1):
                    x, y = full_path[i], full_path[i + 1]
                    key  = tuple(sorted([x, y]))
                    chain.append({
                        "from":  x,
                        "to":    y,
                        "songs": list(edge_songs.get(key, edge_songs.get((x, y), set())))[:3],
                    })
                return {
                    "found":   True,
                    "degrees": len(full_path) - 1,
                    "path":    full_path,
                    "chain":   chain,
                }
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, path + [neighbor]))

    return {"found": False, "reason": "no connection found within 6 degrees"}

@app.get("/top-items", response_class=HTMLResponse)
async def top_items(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    async def fetch_top(item_type: str, time_range: str):
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.spotify.com/v1/me/top/{item_type}?time_range={time_range}&limit=20",
                headers={"Authorization": f"Bearer {token_store['access_token']}"},
            )
            if resp.status_code == 401:
                await refresh_access_token()
                return []
            if resp.status_code != 200:
                return []
            return resp.json().get("items", [])

    # Fetch all 6 combinations concurrently
    (
        artists_short, artists_medium, artists_long,
        tracks_short,  tracks_medium,  tracks_long,
    ) = await asyncio.gather(
        fetch_top("artists", "short_term"),
        fetch_top("artists", "medium_term"),
        fetch_top("artists", "long_term"),
        fetch_top("tracks",  "short_term"),
        fetch_top("tracks",  "medium_term"),
        fetch_top("tracks",  "long_term"),
    )

    # Build your own play count rankings for cross-reference
    all_plays_q   = select(TrackPlay.artists)
    all_play_rows = (await db.execute(all_plays_q)).fetchall()
    own_artist_counter = Counter()
    for row in all_play_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    own_artist_counter[a] += 1

    own_track_q   = select(TrackPlay.track_name, TrackPlay.artists, func.count().label("plays")).group_by(TrackPlay.track_id).order_by(func.count().desc())
    own_track_rows = (await db.execute(own_track_q)).fetchall()
    own_track_rank = {row.track_name.lower().strip(): i + 1 for i, row in enumerate(own_track_rows)}
    own_artist_rank = {name.lower().strip(): i + 1 for i, (name, _) in enumerate(own_artist_counter.most_common())}

    def format_artists(raw: list) -> list:
        out = []
        for i, item in enumerate(raw):
            name  = item["name"]
            image = item["images"][-1]["url"] if item.get("images") else None
            out.append({
                "rank":       i + 1,
                "name":       name,
                "image":      image,
                "genres":     ", ".join(item.get("genres", [])[:3]),
                "spotify_url": item["external_urls"]["spotify"],
                "own_rank":   own_artist_rank.get(name.lower().strip()),
                "own_plays":  own_artist_counter.get(name, 0),
            })
        return out

    def format_tracks(raw: list) -> list:
        out = []
        for i, item in enumerate(raw):
            name    = item["name"]
            artists = ", ".join(a["name"] for a in item["artists"])
            image   = item["album"]["images"][-1]["url"] if item["album"].get("images") else None
            out.append({
                "rank":        i + 1,
                "name":        name,
                "artists":     artists,
                "album":       item["album"]["name"],
                "image":       image,
                "spotify_url": item["external_urls"]["spotify"],
                "own_rank":    own_track_rank.get(name.lower().strip()),
            })
        return out

    return templates.TemplateResponse("top_items.html", {
        "request": request,
        "artists": {
            "short":  format_artists(artists_short),
            "medium": format_artists(artists_medium),
            "long":   format_artists(artists_long),
        },
        "tracks": {
            "short":  format_tracks(tracks_short),
            "medium": format_tracks(tracks_medium),
            "long":   format_tracks(tracks_long),
        },
    })

@app.get("/following", response_class=HTMLResponse)
async def following(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    # Paginate through all followed artists
    followed = []
    url       = "https://api.spotify.com/v1/me/following?type=artist&limit=50"

    while url:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token_store['access_token']}"},
            )
        if resp.status_code == 401:
            await refresh_access_token()
            break
        if resp.status_code != 200:
            break

        data    = resp.json().get("artists", {})
        items   = data.get("items", [])
        for item in items:
            followed.append({
                "name":        item["name"],
                "spotify_id":  item["id"],
                "image":       item["images"][-1]["url"] if item.get("images") else None,
                "genres":      ", ".join(item.get("genres", [])[:3]),
                "spotify_url": item["external_urls"]["spotify"],
            })

        # Spotify uses cursor-based pagination for following
        cursor = data.get("cursors", {}).get("after")
        url    = f"https://api.spotify.com/v1/me/following?type=artist&limit=50&after={cursor}" if cursor else None

    if not followed:
        return templates.TemplateResponse("following.html", {
            "request":      request,
            "followed":     [],
            "loyal":        [],
            "ghost":        [],
            "unrecognized": [],
            "total":        0,
        })

    # Build your own play counts for cross-reference
    all_plays_q   = select(TrackPlay.artists)
    all_play_rows = (await db.execute(all_plays_q)).fetchall()
    own_counter   = Counter()
    for row in all_play_rows:
        if row.artists:
            for a in row.artists.split(","):
                a = a.strip()
                if a:
                    own_counter[a] += 1

    followed_names = {f["name"].lower().strip() for f in followed}

    # Cross-reference
    for f in followed:
        key         = f["name"].lower().strip()
        f["plays"]  = own_counter.get(f["name"], 0)
        # fuzzy match — try case-insensitive
        if f["plays"] == 0:
            for tracked_name, count in own_counter.items():
                if tracked_name.lower().strip() == key:
                    f["plays"] = count
                    break

    # Build the three groups
    loyal        = sorted([f for f in followed if f["plays"] >= 10],  key=lambda x: x["plays"], reverse=True)
    ghost        = sorted([f for f in followed if f["plays"] == 0],   key=lambda x: x["name"])
    casual       = sorted([f for f in followed if 0 < f["plays"] < 10], key=lambda x: x["plays"], reverse=True)

    # Unrecognized fans — play a lot but haven't followed
    followed_lower = {f["name"].lower().strip() for f in followed}
    unrecognized   = []
    for name, count in own_counter.most_common(50):
        if name.lower().strip() not in followed_lower and count >= 10:
            # Get image from cache if available
            cache_q = select(ArtistCache).where(
                ArtistCache.artist_name == name,
                ArtistCache.track_name  == None,
                ArtistCache.image_url   != None,
            ).limit(1)
            cached    = (await db.execute(cache_q)).scalar_one_or_none()
            image_url = cached.image_url if cached else None
            unrecognized.append({
                "name":   name,
                "plays":  count,
                "image":  image_url,
            })

    return templates.TemplateResponse("following.html", {
        "request":      request,
        "followed":     followed,
        "loyal":        loyal,
        "ghost":        ghost,
        "casual":       casual,
        "unrecognized": unrecognized,
        "total":        len(followed),
        "ghost_pct":    round(len(ghost) / len(followed) * 100, 1) if followed else 0,
        "loyal_pct":    round(len(loyal) / len(followed) * 100, 1) if followed else 0,
    })

@app.get("/review", response_class=HTMLResponse)
async def year_review(request: Request, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    # Get all available years
    years_q = (
        select(func.strftime('%Y', TrackPlay.listened_at).label("year"))
        .group_by(func.strftime('%Y', TrackPlay.listened_at))
        .order_by(func.strftime('%Y', TrackPlay.listened_at).desc())
    )
    available_years = [r.year for r in (await db.execute(years_q)).fetchall()]

    if not available_years:
        return templates.TemplateResponse("year_review.html", {
            "request":         request,
            "available_years": [],
            "selected_year":   None,
            "data":            None,
        })

    return templates.TemplateResponse("year_review.html", {
        "request":         request,
        "available_years": available_years,
        "selected_year":   None,
        "data":            None,
    })


@app.get("/review/{year}", response_class=HTMLResponse)
async def year_review_data(request: Request, year: str, db: AsyncSession = Depends(get_db)):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    # Get all available years for selector
    years_q = (
        select(func.strftime('%Y', TrackPlay.listened_at).label("year"))
        .group_by(func.strftime('%Y', TrackPlay.listened_at))
        .order_by(func.strftime('%Y', TrackPlay.listened_at).desc())
    )
    available_years = [r.year for r in (await db.execute(years_q)).fetchall()]

    # Get all plays for this year
    plays_q = (
        select(TrackPlay)
        .where(func.strftime('%Y', TrackPlay.listened_at) == year)
        .order_by(TrackPlay.listened_at.asc())
    )
    plays = (await db.execute(plays_q)).scalars().fetchall()

    if not plays:
        return templates.TemplateResponse("year_review.html", {
            "request":         request,
            "available_years": available_years,
            "selected_year":   year,
            "data":            None,
        })

    total     = len(plays)
    skips     = sum(1 for p in plays if p.was_skipped)
    completes = total - skips
    skip_rate = round(skips / total * 100, 1) if total else 0
    minutes   = round(sum(p.progress_ms for p in plays) / 60000, 1)
    hours     = round(minutes / 60, 1)
    days      = len(set(p.listened_at.date() for p in plays))

    # ── Top artists ───────────────────────────────────────────────────────
    artist_counter = Counter()
    for p in plays:
        if p.artists:
            for a in p.artists.split(","):
                a = a.strip()
                if a:
                    artist_counter[a] += 1

    top_artists = [{"name": a, "plays": c} for a, c in artist_counter.most_common(5)]

    # Get artist images from cache
    for a in top_artists:
        cache_q = select(ArtistCache).where(
            ArtistCache.artist_name == a["name"],
            ArtistCache.track_name  == None,
            ArtistCache.image_url   != None,
        ).limit(1)
        cached = (await db.execute(cache_q)).scalar_one_or_none()
        a["image"] = cached.image_url if cached else None

    # ── Top tracks ────────────────────────────────────────────────────────
    track_counter = Counter()
    track_art     = {}
    track_artists = {}
    for p in plays:
        track_counter[p.track_name] += 1
        track_art[p.track_name]      = p.album_art_url
        track_artists[p.track_name]  = p.artists

    top_tracks = [
        {
            "name":    t,
            "plays":   c,
            "art":     track_art.get(t),
            "artists": track_artists.get(t, ""),
        }
        for t, c in track_counter.most_common(5)
    ]

    # ── Top genre ─────────────────────────────────────────────────────────
    genre_counter = Counter()
    for p in plays:
        if p.primary_genre:
            for g in p.primary_genre.split(","):
                g = g.strip().lower()
                if g:
                    genre_counter[g] += 1
    top_genre  = genre_counter.most_common(1)[0][0] if genre_counter else "—"
    top_genres = [{"genre": g, "count": c} for g, c in genre_counter.most_common(8)]

    # ── Top mood ──────────────────────────────────────────────────────────
    mood_counter  = Counter(p.auto_mood for p in plays if p.auto_mood and p.auto_mood != "UNKNOWN")
    top_mood      = mood_counter.most_common(1)[0][0] if mood_counter else "UNKNOWN"
    mood_breakdown = [{"mood": m, "count": c} for m, c in mood_counter.most_common(8)]

    # ── First and last track ──────────────────────────────────────────────
    first_track = plays[0]
    last_track  = plays[-1]

    # ── Biggest day ───────────────────────────────────────────────────────
    day_counter   = Counter(p.listened_at.strftime("%Y-%m-%d") for p in plays)
    biggest_day   = day_counter.most_common(1)[0] if day_counter else None

    # ── Month by month ────────────────────────────────────────────────────
    monthly = Counter(p.listened_at.strftime("%b") for p in plays)
    month_order  = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    monthly_data = [{"month": m, "plays": monthly.get(m, 0)} for m in month_order]
    max_monthly  = max(d["plays"] for d in monthly_data) or 1

    # ── New artists discovered this year ─────────────────────────────────
    # First ever play for each artist across all time
    all_plays_q = (
        select(TrackPlay.artists, func.min(TrackPlay.listened_at).label("first"))
        .group_by(TrackPlay.artists)
    )
    all_first_plays = (await db.execute(all_plays_q)).fetchall()
    new_artists = set()
    for row in all_first_plays:
        if row.artists and row.first:
            first_year = row.first.strftime("%Y")
            if first_year == year:
                for a in row.artists.split(","):
                    a = a.strip()
                    if a:
                        new_artists.add(a)
    new_artist_count = len(new_artists)
    new_artists_list = [
        {"name": a, "plays": artist_counter.get(a, 0)}
        for a in sorted(new_artists, key=lambda x: artist_counter.get(x, 0), reverse=True)
    ][:10]

    # ── Songs played every month ──────────────────────────────────────────
    months_active = set(p.listened_at.strftime("%b") for p in plays)
    track_months  = defaultdict(set)
    for p in plays:
        track_months[p.track_name].add(p.listened_at.strftime("%b"))
    every_month_tracks = [
        {"name": t, "plays": track_counter[t], "art": track_art.get(t)}
        for t, months in track_months.items()
        if months >= months_active and len(months_active) >= 3
    ]
    every_month_tracks = sorted(every_month_tracks, key=lambda x: x["plays"], reverse=True)[:8]

    # ── Ghost liked songs ─────────────────────────────────────────────────
    liked_q = select(LikedSong)
    liked   = (await db.execute(liked_q)).scalars().fetchall()
    played_this_year = set(p.track_id for p in plays)
    ghost_liked = [
        s for s in liked
        if s.track_id not in played_this_year
    ][:10]

    # ── No-skip streak within the year ───────────────────────────────────
    longest_noskip = temp_noskip = 0
    for p in plays:
        if not p.was_skipped:
            temp_noskip    += 1
            longest_noskip  = max(longest_noskip, temp_noskip)
        else:
            temp_noskip = 0

    # ── Peak hour ─────────────────────────────────────────────────────────
    hour_counter = Counter(p.hour_of_day for p in plays if p.hour_of_day is not None)
    peak_hour    = hour_counter.most_common(1)[0][0] if hour_counter else None
    hour_dist    = [hour_counter.get(h, 0) for h in range(24)]

    # ── Peak day ──────────────────────────────────────────────────────────
    dow_counter = Counter(p.day_of_week for p in plays if p.day_of_week)
    peak_dow    = dow_counter.most_common(1)[0][0] if dow_counter else None

    # ── Night owl vs morning ──────────────────────────────────────────────
    night_pct   = round(sum(1 for p in plays if p.hour_of_day is not None and 0  <= p.hour_of_day < 4) / total * 100, 1) if total else 0
    morning_pct = round(sum(1 for p in plays if p.hour_of_day is not None and 5  <= p.hour_of_day < 10) / total * 100, 1) if total else 0
    weekend_pct = round(sum(1 for p in plays if p.day_of_week in ["Saturday", "Sunday"]) / total * 100, 1) if total else 0

    # ── Skip rate vs prior year ───────────────────────────────────────────
    prior_year     = str(int(year) - 1)
    prior_plays_q  = (
        select(TrackPlay.was_skipped)
        .where(func.strftime('%Y', TrackPlay.listened_at) == prior_year)
    )
    prior_plays    = (await db.execute(prior_plays_q)).fetchall()
    prior_skips    = sum(1 for p in prior_plays if p.was_skipped)
    prior_skip_rate = round(prior_skips / len(prior_plays) * 100, 1) if prior_plays else None
    skip_rate_delta = round(skip_rate - prior_skip_rate, 1) if prior_skip_rate is not None else None

    # ── Liked songs added this year ───────────────────────────────────────
    new_liked = [s for s in liked if s.added_at and s.added_at.strftime("%Y") == year]
    new_liked_count = len(new_liked)

    # ── Most skipped track ────────────────────────────────────────────────
    skip_counter = Counter(p.track_name for p in plays if p.was_skipped)
    most_skipped = skip_counter.most_common(1)[0] if skip_counter else None

    # ── Unique counts ─────────────────────────────────────────────────────
    unique_tracks  = len(set(p.track_id  for p in plays))
    unique_artists = len(set(
        a.strip()
        for p in plays if p.artists
        for a in p.artists.split(",")
        if a.strip()
    ))

    # ── Listening personality ─────────────────────────────────────────────
    if night_pct > morning_pct and night_pct > 20:
        personality = "🌙 NIGHT OWL"
    elif morning_pct > night_pct and morning_pct > 20:
        personality = "🌅 EARLY BIRD"
    elif weekend_pct > 40:
        personality = "📅 WEEKEND WARRIOR"
    else:
        personality = "⚡ ALL DAY LISTENER"

    # ── Biggest comeback (longest gap then returned) ───────────────────────
    days_with_plays = sorted(set(p.listened_at.date() for p in plays))
    longest_gap = 0
    comeback_date = None
    for i in range(1, len(days_with_plays)):
        gap = (days_with_plays[i] - days_with_plays[i-1]).days
        if gap > longest_gap:
            longest_gap   = gap
            comeback_date = days_with_plays[i].strftime("%b %d")

    return templates.TemplateResponse("year_review.html", {
        "request":          request,
        "available_years":  available_years,
        "selected_year":    year,
        "data": {
            "total":            total,
            "skips":            skips,
            "completes":        completes,
            "skip_rate":        skip_rate,
            "minutes":          minutes,
            "hours":            hours,
            "days":             days,
            "top_artists":      top_artists,
            "top_tracks":       top_tracks,
            "top_genre":        top_genre,
            "top_genres":       top_genres,
            "top_mood":         top_mood,
            "mood_breakdown":   mood_breakdown,
            "first_track":      first_track,
            "last_track":       last_track,
            "biggest_day":      biggest_day,
            "monthly_data":     monthly_data,
            "max_monthly":      max_monthly,
            "new_artist_count": new_artist_count,
            "new_artists_list": new_artists_list,
            "every_month_tracks": every_month_tracks,
            "ghost_liked":      ghost_liked,
            "longest_noskip":   longest_noskip,
            "peak_hour":        peak_hour,
            "peak_dow":         peak_dow,
            "hour_dist":        hour_dist,
            "night_pct":        night_pct,
            "morning_pct":      morning_pct,
            "weekend_pct":      weekend_pct,
            "skip_rate_delta":  skip_rate_delta,
            "prior_skip_rate":  prior_skip_rate,
            "prior_year":       prior_year,
            "new_liked_count":  new_liked_count,
            "most_skipped":     most_skipped,
            "unique_tracks":    unique_tracks,
            "unique_artists":   unique_artists,
            "personality":      personality,
            "longest_gap":      longest_gap,
            "comeback_date":    comeback_date,
        },
    })