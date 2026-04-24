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
from collections import defaultdict

from database import init_db, AsyncSessionLocal
from models import TrackPlay, ArtistCache
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

SCOPES = "user-read-currently-playing user-read-playback-state user-read-recently-played user-top-read"

token_store     = {}
poller_running  = False


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

                            # Fetch audio features for new track
                            try:
                                async with httpx.AsyncClient() as client:
                                    af_resp = await client.get(
                                        f"https://api.spotify.com/v1/audio-features/{item['id']}",
                                        headers={"Authorization": f"Bearer {token_store['access_token']}"},
                                    )
                                print(f"[POLLER] Audio features status: {af_resp.status_code}")
                                if af_resp.status_code == 200:
                                    af = af_resp.json()
                                    current_track_state["valence"]          = af.get("valence")
                                    current_track_state["energy"]           = af.get("energy")
                                    current_track_state["danceability"]     = af.get("danceability")
                                    current_track_state["tempo"]            = af.get("tempo")
                                    current_track_state["acousticness"]     = af.get("acousticness")
                                    current_track_state["instrumentalness"] = af.get("instrumentalness")
                                    print(f"[POLLER] Features stored: valence={af.get('valence')} energy={af.get('energy')}")
                            except Exception as e:
                                print(f"[POLLER] Audio features error: {e}")

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
    if not poller_running:
        poller_running = True
        asyncio.create_task(poll_spotify(app))
        print("[POLLER] Started")
    else:
        print("[POLLER] Already running, skipping")
    yield


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")


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
    return RedirectResponse("/")


@app.get("/logout")
async def logout():
    token_store.clear()
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

    total_plays    = await db.scalar(select(func.count()).select_from(TrackPlay)) or 0
    total_skips    = await db.scalar(select(func.count()).select_from(TrackPlay).where(TrackPlay.was_skipped == True)) or 0
    total_complete = total_plays - total_skips
    skip_rate      = round((total_skips / total_plays * 100), 1) if total_plays else 0
    total_ms       = await db.scalar(select(func.sum(TrackPlay.progress_ms)).select_from(TrackPlay)) or 0
    total_minutes  = round(total_ms / 60000, 1)
    total_hours    = round(total_minutes / 60, 1)
    unique_tracks  = await db.scalar(select(func.count(func.distinct(TrackPlay.track_id))).select_from(TrackPlay)) or 0
    unique_artists = await db.scalar(select(func.count(func.distinct(TrackPlay.artists))).select_from(TrackPlay)) or 0

    avg_skip_pct_row = await db.scalar(
        select(func.avg(TrackPlay.progress_pct))
        .where(TrackPlay.was_skipped == True)
    )
    avg_skip_pct = round(avg_skip_pct_row or 0, 1)

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

    all_plays_q    = select(TrackPlay.was_skipped).order_by(TrackPlay.listened_at.asc())
    all_skips_col  = (await db.execute(all_plays_q)).fetchall()
    longest_streak = current_streak = 0
    for row in all_skips_col:
        if not row.was_skipped:
            current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            current_streak = 0

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

    most_played_artists_q = (
        select(TrackPlay.artists, func.count().label("plays"))
        .group_by(TrackPlay.artists)
        .order_by(func.count().desc())
        .limit(10)
    )
    most_played_artists = (await db.execute(most_played_artists_q)).fetchall()

    most_skipped_artists_q = (
        select(TrackPlay.artists, func.count().label("skips"))
        .where(TrackPlay.was_skipped == True)
        .group_by(TrackPlay.artists)
        .order_by(func.count().desc())
        .limit(10)
    )
    most_skipped_artists = (await db.execute(most_skipped_artists_q)).fetchall()

    artist_skip_rate_q = (
        select(
            TrackPlay.artists,
            func.count().label("plays"),
            func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)).label("skips"),
            (func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)) * 100.0 / func.count()).label("skip_rate")
        )
        .group_by(TrackPlay.artists)
        .having(func.count() >= 5)
        .order_by((func.sum(case((TrackPlay.was_skipped == True, 1), else_=0)) * 100.0 / func.count()).asc())
        .limit(10)
    )
    best_artists = (await db.execute(artist_skip_rate_q)).fetchall()

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

    dow_order  = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    dow_q      = select(TrackPlay.day_of_week, func.count().label("cnt")).group_by(TrackPlay.day_of_week)
    dow_rows   = (await db.execute(dow_q)).fetchall()
    dow_map    = {r.day_of_week: r.cnt for r in dow_rows}
    dow_values = [dow_map.get(d, 0) for d in dow_order]

    month_order  = ["January","February","March","April","May","June","July","August","September","October","November","December"]
    month_q      = select(TrackPlay.month, func.count().label("cnt")).group_by(TrackPlay.month)
    month_rows   = (await db.execute(month_q)).fetchall()
    month_map    = {r.month: r.cnt for r in month_rows}
    month_values = [month_map.get(m, 0) for m in month_order]

    week_ago = datetime.now(TIMEZONE) - timedelta(days=7)

    top_track_week_q = (
        select(TrackPlay.track_name, TrackPlay.artists, func.count().label("plays"))
        .where(TrackPlay.listened_at >= week_ago)
        .group_by(TrackPlay.track_id)
        .order_by(func.count().desc())
        .limit(5)
    )
    top_tracks_week = (await db.execute(top_track_week_q)).fetchall()

    top_artist_week_q = (
        select(TrackPlay.artists, func.count().label("plays"))
        .where(TrackPlay.listened_at >= week_ago)
        .group_by(TrackPlay.artists)
        .order_by(func.count().desc())
        .limit(5)
    )
    top_artists_week = (await db.execute(top_artist_week_q)).fetchall()

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

    artist_counts = {}
    for p in today_plays:
        artist_counts[p.artists] = artist_counts.get(p.artists, 0) + 1
    top_artist_today = max(artist_counts.items(), key=lambda x: x[1]) if artist_counts else None

    first_track          = today_plays[0] if today_plays else None
    last_track           = today_plays[-1] if today_plays else None
    unique_tracks_today  = len(set(p.track_id for p in today_plays))
    unique_artists_today = len(set(p.artists for p in today_plays))

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

    best_artist_streak = {"artist": None, "count": 0}
    temp_artist        = {"artist": None, "count": 0}
    for play in all_plays:
        if play.artists == temp_artist["artist"]:
            temp_artist["count"] += 1
        else:
            if temp_artist["count"] > best_artist_streak["count"]:
                best_artist_streak = dict(temp_artist)
            temp_artist = {"artist": play.artists, "count": 1}
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


@app.get("/rave", response_class=HTMLResponse)
async def rave_mode(request: Request):
    if "access_token" not in token_store:
        return RedirectResponse("/")

    async with httpx.AsyncClient() as client:
        np_response = await client.get(
            SPOTIFY_NOW_PLAYING_URL,
            headers={"Authorization": f"Bearer {token_store['access_token']}"},
        )

    if np_response.status_code == 401:
        refreshed = await refresh_access_token()
        if refreshed:
            return RedirectResponse("/rave")
        token_store.clear()
        return RedirectResponse("/")

    track        = None
    bpm          = 120
    energy       = 0.5
    valence      = 0.5
    danceability = 0.5

    if np_response.status_code == 200:
        data = np_response.json()
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

            async with httpx.AsyncClient() as client:
                af_response = await client.get(
                    f"https://api.spotify.com/v1/audio-features/{item['id']}",
                    headers={"Authorization": f"Bearer {token_store['access_token']}"},
                )
            if af_response.status_code == 200:
                af           = af_response.json()
                bpm          = af.get("tempo", 120)
                energy       = af.get("energy", 0.5)
                valence      = af.get("valence", 0.5)
                danceability = af.get("danceability", 0.5)

    return templates.TemplateResponse("rave.html", {
        "request":      request,
        "track":        track,
        "bpm":          bpm,
        "energy":       energy,
        "valence":      valence,
        "danceability": danceability,
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

    evolution = {}
    for month in month_names:
        top_q = (
            select(TrackPlay.artists, func.count().label("plays"))
            .where(TrackPlay.month == month)
            .group_by(TrackPlay.artists)
            .order_by(func.count().desc())
            .limit(5)
        )
        top_artists      = (await db.execute(top_q)).fetchall()
        evolution[month] = [{"artist": r.artists, "plays": r.plays} for r in top_artists]

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
                "artists": result.artists,
                "plays":   result.plays,
            }

    all_artists_q = (
        select(TrackPlay.artists, func.count().label("plays"))
        .group_by(TrackPlay.artists)
        .order_by(func.count().desc())
        .limit(5)
    )
    top_5_artists = [(r.artists) for r in (await db.execute(all_artists_q)).fetchall()]

    chart_datasets = []
    neon_colors    = ['#ff00ff', '#00ffff', '#ffff00', '#00ff88', '#ff00aa']
    for i, artist in enumerate(top_5_artists):
        data_points = []
        for month in month_names:
            plays_q = (
                select(func.count())
                .where(TrackPlay.month == month)
                .where(TrackPlay.artists == artist)
            )
            count = await db.scalar(plays_q) or 0
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

    # ── Mood by hour of day ───────────────────────────────────────────────────
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

    # ── Genre distribution — split comma separated genres ─────────────────────
    all_genre_plays_q = (
        select(TrackPlay.primary_genre)
        .where(TrackPlay.primary_genre != None)
    )
    all_genre_rows = (await db.execute(all_genre_plays_q)).fetchall()

    from collections import Counter
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
    genre_mood_rows = (await db.execute(genre_mood_plays_q)).fetchall()

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

    # ── Recent tracks with mood ───────────────────────────────────────────────
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
        from collections import Counter
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

@app.get("/debug")
async def debug(db: AsyncSession = Depends(get_db)):
    recent_q = select(TrackPlay).order_by(TrackPlay.listened_at.desc()).limit(10)
    recent   = (await db.execute(recent_q)).scalars().fetchall()
    result   = []
    for t in recent:
        result.append({
            "track":        t.track_name,
            "auto_mood":    t.auto_mood,
            "valence":      t.valence,
            "energy":       t.energy,
            "genre":        t.primary_genre,
            "listened_at":  str(t.listened_at),
            "progress_pct": t.progress_pct,
        })
    return result


@app.get("/debug-artist/{artist_id}")
async def debug_artist(artist_id: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.spotify.com/v1/artists/{artist_id}",
            headers={"Authorization": f"Bearer {token_store['access_token']}"},
        )
    return response.json()


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
                "name":      item["name"],
                "artists":   ", ".join(a["name"] for a in item["artists"]),
                "album_art": item["album"]["images"][0]["url"] if item["album"]["images"] else None,
            }
    return {}


@app.get("/debug-related-artist/{artist_id}")
async def debug_related(artist_id: str):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"https://api.spotify.com/v1/artists/{artist_id}/related-artists",
            headers={"Authorization": f"Bearer {token_store['access_token']}"},
        )
    return response.json()

@app.get("/debug-everynoise/{artist_name}")
async def debug_everynoise(artist_name: str):
    import httpx
    import urllib.parse
    encoded = urllib.parse.quote(artist_name)
    async with httpx.AsyncClient(timeout=8.0) as client:
        response = await client.get(
            f"https://everynoise.com/lookup.cgi?who={encoded}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
        )
    return {"status": response.status_code, "body": response.text[:2000]}