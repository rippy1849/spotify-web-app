from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import TrackPlay, ArtistCache
from collections import Counter

TIMEZONE       = ZoneInfo("America/New_York")
SKIP_THRESHOLD = 0.80

current_track_state = {
    "track_id":          None,
    "track_name":        None,
    "artists":           None,
    "primary_artist":    None,
    "album":             None,
    "album_art_url":     None,
    "duration_ms":       None,
    "progress_ms":       None,
    "primary_genre":     None,
    "artist_id":         None,
}

_last_commit_key = {"key": None}



def derive_mood_from_genre(genre_str: str) -> str:
    if not genre_str:
        return "UNKNOWN"

    genres = [g.strip().lower() for g in genre_str.split(",") if g.strip()]

    def matches(genre: str, terms: list) -> bool:
        for term in terms:
            if genre == term:
                return True
            # Only match whole words
            import re
            if re.search(r'\b' + re.escape(term) + r'\b', genre):
                return True
        return False

    mood_map = {
        "EUPHORIC": [
            "trance", "uplifting trance", "vocal trance", "anthem trance",
            "euphoric trance", "epic trance", "orchestral trance",
            "progressive trance", "psytrance", "goa trance", "full on",
            "suomisaundi", "darkpsy", "forest psytrance", "hi-tech",
            "tech trance", "hard trance", "acid trance",
            "progressive house", "big room house", "big room",
            "electro house", "bounce", "dutch house", "melbourne bounce",
            "mainstage", "festival edm", "festival trap",
            "hands up", "eurodance", "euro house", "eurobeat",
            "italo dance", "italo disco", "hi-nrg",
            "afro house", "tribal house", "funky house",
            "soulful house", "gospel house",
        ],
        "RAVE": [
            "techno", "tech house", "industrial techno", "minimal techno",
            "dark techno", "detroit techno", "berlin techno", "acid techno",
            "hard techno", "schranz", "peak time techno", "rave techno",
            "doomcore", "raw techno", "brutal techno",
            "acid house", "chicago house",
            "minimal house", "microhouse", "click house",
            "deep minimal", "dark minimal",
            "rave", "oldschool rave", "uk rave", "acid rave",
            "breakbeat hardcore", "jungle techno",
        ],
        "INTENSE": [
            "hardstyle", "hardcore", "gabber", "terror", "frenchcore",
            "uptempo", "rawstyle", "industrial hardcore", "speedcore",
            "uk hardcore", "happy hardcore", "makina", "hard house",
            "powernoize", "aggrotech", "dark electro",
            "riddim", "deathstep", "dark dubstep", "brostep",
            "heavy bass", "tearout", "wubstep",
            "metal", "heavy metal", "death metal", "black metal",
            "metalcore", "deathcore", "nu metal", "thrash metal",
            "doom metal", "grindcore", "sludge metal", "post-metal",
            "djent", "mathcore", "brutal death metal", "symphonic metal",
            "power metal", "folk metal", "viking metal",
            "punk", "hardcore punk", "post-hardcore", "screamo",
            "skate punk", "pop punk", "anarcho punk",
            "crust punk", "noise rock", "no wave",
            "hard rock", "stoner rock", "desert rock",
            "industrial rock", "industrial metal",
        ],
        "PUMPED": [
            "drum and bass", "dnb", "neurofunk", "jump up",
            "liquid funk", "liquid dnb", "crossbreed", "darkstep",
            "techstep", "hardstep", "sambass", "drumfunk",
            "halftime", "autonomic", "jazzstep",
            "dubstep", "complextro", "post-dubstep",
            "breakbeat", "breaks", "nu-skool breaks", "progressive breaks",
            "florida breaks", "big beat", "chemical breaks",
            "trap edm", "hybrid trap", "future bounce", "riddim trap",
            "hardwave", "future rave",
            "bass house", "g-house", "ghetto house",
            "juke", "footwork", "jersey club",
            "workout", "gym", "running", "sport", "cardio",
            "electro house",
        ],
        "BROODING": [
            "hip hop", "rap", "underground hip hop",
            "boom bap", "east coast hip hop", "west coast hip hop",
            "southern hip hop", "midwest hip hop", "conscious hip hop",
            "alternative hip hop", "abstract hip hop", "instrumental hip hop",
            "jazz rap", "lo-fi hip hop", "political hip hop",
            "horrorcore", "crunk", "snap", "hyphy",
            "trap", "dark trap", "cloud rap", "mumble rap",
            "drill", "uk drill", "chicago drill", "brooklyn drill",
            "pop rap", "melodic rap", "emo rap",
            "grime", "road rap", "afroswing",
            "witch house", "occult",
            "industrial", "ebm", "futurepop",
            "power electronics", "death industrial",
            "alternative r&b", "dark r&b", "trap r&b",
        ],
        "MELANCHOLIC": [
            "chillstep", "melodic dubstep", "emotional dubstep",
            "tearjerker", "sadstep",
            "shoegaze", "dream pop", "slowcore", "sadcore",
            "bedroom pop", "indie sad", "lo-fi indie",
            "indie folk", "chamber pop",
            "post-rock", "ambient pop",
            "darkwave", "gothic rock", "gothic", "deathrock",
            "coldwave", "minimal wave", "dark pop",
            "delta blues", "soul blues", "doom blues",
            "anti-folk",
        ],
        "CHILL": [
            "future bass", "kawaii future bass", "color bass",
            "melodic bass",
            "deep house", "nu disco", "melodic house",
            "organic house", "afro melodic house", "sunset house",
            "balearic", "balearic beat", "balearic house",
            "tropical house", "slap house", "indie dance",
            "chillout", "chill", "downtempo", "down tempo",
            "trip hop", "chillhop", "lo-fi", "lofi",
            "jazzhop", "jazz hop", "nubeat",
            "synthwave", "retrowave", "outrun", "darksynth",
            "dreamwave", "space synth", "chillwave",
            "vapourwave", "vaporwave", "future funk",
            "indie electronic", "indietronica",
            "uk garage", "2step", "future garage",
            "speed garage", "bassline",
            "glitch hop", "wonky", "purple",
            "ethereal wave",
        ],
        "PEACEFUL": [
            "ambient", "dark ambient", "space ambient", "drone ambient",
            "nature ambient", "field recording", "soundscape",
            "minimal ambient", "isolationism", "lowercase",
            "drone music", "musique concrete", "acousmatic",
            "new age", "meditation", "sleep", "healing",
            "nature sounds", "spa", "yoga", "relaxation",
            "binaural", "solfeggio", "tibetan",
            "classical", "orchestral", "neo-classical", "baroque",
            "chamber music", "contemporary classical",
            "minimalism", "modern classical", "piano",
            "string quartet", "symphony",
            "acoustic", "folk", "folk music", "traditional folk",
            "celtic", "irish folk", "british folk", "appalachian",
            "americana", "bluegrass", "country folk",
            "fingerpicking", "fingerstyle", "neofolk",
            "world music", "ethnic", "native american",
            "indian classical", "flamenco",
        ],
        "NOSTALGIC": [
            "80s", "90s", "70s", "60s", "retro",
            "classic rock", "classic pop", "oldies", "vintage",
            "new wave", "post punk", "post-punk",
            "sophisti-pop", "twee pop", "jangle pop",
            "yacht rock", "soft rock", "arena rock",
            "glam rock", "glam metal", "hair metal",
            "disco", "boogie", "motown", "northern soul", "mod",
            "psychedelic rock", "psychedelic pop",
            "city pop", "shibuya-kei",
        ],
        "FOCUSED": [
            "instrumental", "study", "focus", "concentration",
            "idm", "intelligent dance music", "braindance",
            "clicks and cuts", "microsound", "glitch",
            "jazz", "jazz fusion", "nu jazz", "cool jazz",
            "bebop", "hard bop", "free jazz", "avant-garde jazz",
            "bossa nova", "samba jazz", "latin jazz",
            "math rock", "progressive rock", "prog rock",
            "ambient techno", "berlin school", "space music",
        ],
        "ROMANTIC": [
            "r&b", "soul", "neo soul", "contemporary r&b",
            "quiet storm", "smooth r&b", "slow jam",
            "bedroom r&b", "jazz vocals", "vocal jazz",
            "samba", "bolero", "tango",
            "love songs", "adult contemporary",
            "easy listening", "lounge", "exotica",
            "smooth jazz", "jazz pop",
            "chanson", "french pop", "italian pop",
            "latin pop", "latin", "salsa", "bachata",
            "reggaeton", "latin urban",
        ],
        "HAPPY": [
            "pop", "dance pop", "synth pop", "indie pop",
            "bubblegum pop", "power pop", "art pop",
            "k-pop", "j-pop", "mandopop", "c-pop",
            "teen pop", "sunshine pop", "summer",
            "happy", "feel good", "party", "dance",
            "club", "nu disco", "funk pop",
            "reggae", "ska", "rocksteady", "dancehall",
            "tropical", "island", "afropop",
        ],
        "ENERGETIC": [
            "electronic", "edm", "electro", "house",
            "alternative rock", "alternative", "indie rock",
            "rock", "garage rock", "post-punk revival",
            "new rave", "dance rock", "electrorock",
            "nu rave", "fidget house", "funk",
        ],
    }

    # Count how many genres match each mood
    mood_votes = Counter()
    for genre in genres:
        for mood, terms in mood_map.items():
            if matches(genre, terms):
                mood_votes[mood] += 1
                break  # each genre only votes once

    if not mood_votes:
        return "NEUTRAL"

    # Return the mood with the most votes
    return mood_votes.most_common(1)[0][0]



def update_state(track: dict):
    current_track_state.update({
        "track_id":         track["track_id"],
        "track_name":       track["track_name"],
        "artists":          track["artists"],
        "primary_artist":   track["artists"].split(",")[0].strip() if track.get("artists") else None,
        "album":            track["album"],
        "album_art_url":    track["album_art_url"],
        "duration_ms":      track["duration_ms"],
        "progress_ms":      track["progress_ms"],
        "primary_genre":    track.get("primary_genre"),
        "artist_id":        track.get("artist_id"),
    })


def is_new_track(track: dict) -> bool:
    return current_track_state["track_id"] != track["track_id"]


def was_skipped(progress_ms: int, duration_ms: int) -> bool:
    if not progress_ms or not duration_ms:
        return False
    return (progress_ms / duration_ms) < SKIP_THRESHOLD


async def get_or_fetch_genre(artist_name: str, artist_id: str,
                              db: AsyncSession, access_token: str,
                              track_name: str = None) -> str | None:
    import httpx
    import urllib.parse
    import os
    import re

    LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
    DISCOGS_TOKEN  = os.getenv("DISCOGS_TOKEN")

    cache_q = select(ArtistCache).where(
        ArtistCache.artist_name == artist_name,
        ArtistCache.track_name  == track_name,
    )
    cached = (await db.execute(cache_q)).scalar_one_or_none()

    if cached:
        genres = cached.genres or ""
        print(f"[GENRE] Cache hit for {artist_name} - {track_name}: {genres}")
        return genres if genres else None

    def is_valid_genre(genre: str) -> bool:
        if not genre:
            return False
        genre_lower = genre.lower().strip()
        if len(genre_lower) < 2 or len(genre_lower) > 50:
            return False
        if any(c in genre_lower for c in ['http', 'www', '<', '>', '{', '}', '/', '\\', '=', '@']):
            return False
        if genre_lower.strip().isnumeric():
            return False
        if re.search(r'[<>"\']', genre_lower):
            return False
        if not re.match(r'^[\w\s\-&\.\(\)\']+$', genre_lower):
            return False
        return True

    def dedup_genres(genres: list) -> list:
        seen    = set()
        deduped = []
        for g in genres:
            key = g.lower().strip()
            if key not in seen:
                seen.add(key)
                deduped.append(g)
        return deduped

    async def cache_and_return(genres: list, label: str) -> str | None:
        deduped   = dedup_genres(genres)
        genre_str = ", ".join(deduped)
        print(f"[GENRE] {label} for {artist_name} - {track_name}: {genre_str}")
        entry = ArtistCache(
            artist_name = artist_name,
            track_name  = track_name,
            genres      = genre_str,
        )
        db.add(entry)
        await db.commit()
        return genre_str if genre_str else None

    all_genres = []

    # ── 1. Last.fm track-level ────────────────────────────────────────────────
    if LASTFM_API_KEY and track_name:
        try:
            encoded_artist = urllib.parse.quote(artist_name)
            encoded_track  = urllib.parse.quote(track_name)
            async with httpx.AsyncClient(timeout=8.0) as client:
                lfm_track_resp = await client.get(
                    f"https://ws.audioscrobbler.com/2.0/?method=track.gettoptags"
                    f"&artist={encoded_artist}&track={encoded_track}"
                    f"&api_key={LASTFM_API_KEY}&format=json"
                )
            if lfm_track_resp.status_code == 200:
                tags        = lfm_track_resp.json().get("toptags", {}).get("tag", [])
                strong_tags = [t["name"] for t in tags if t.get("count", 0) >= 10]
                valid_tags  = [t for t in strong_tags if is_valid_genre(t)]
                if valid_tags:
                    all_genres.extend(valid_tags[:8])
                    print(f"[GENRE] Last.fm track tags for {track_name}: {valid_tags[:8]}")
        except Exception as e:
            print(f"[GENRE] Last.fm track error: {e}")

    # ── 2. Discogs track-level ────────────────────────────────────────────────
    if DISCOGS_TOKEN and track_name:
        try:
            encoded_track  = urllib.parse.quote(track_name)
            encoded_artist = urllib.parse.quote(artist_name)
            headers        = {
                "User-Agent":    "RaveFM/1.0",
                "Authorization": f"Discogs token={DISCOGS_TOKEN}",
            }
            async with httpx.AsyncClient(timeout=8.0) as client:
                track_search_resp = await client.get(
                    f"https://api.discogs.com/database/search"
                    f"?q={encoded_track}&artist={encoded_artist}&type=release",
                    headers=headers,
                )
            if track_search_resp.status_code == 200:
                results = track_search_resp.json().get("results", [])
                if results:
                    release_id = results[0].get("id")
                    if release_id:
                        async with httpx.AsyncClient(timeout=8.0) as client:
                            release_resp = await client.get(
                                f"https://api.discogs.com/releases/{release_id}",
                                headers=headers,
                            )
                        if release_resp.status_code == 200:
                            release_data = release_resp.json()
                            styles       = release_data.get("styles", []) + release_data.get("genres", [])
                            valid_styles = [s for s in styles if is_valid_genre(s)]
                            if valid_styles:
                                all_genres.extend(valid_styles)
                                print(f"[GENRE] Discogs track styles for {track_name}: {valid_styles}")
        except Exception as e:
            print(f"[GENRE] Discogs track error: {e}")

    # ── Early exit if track-specific genres found ─────────────────────────────
    if len(all_genres) >= 1:
        return await cache_and_return(all_genres, "Track-specific genres found")

    print(f"[GENRE] No track-specific genres, falling back to artist level")

    # ── 3. Last.fm artist-level ───────────────────────────────────────────────
    if LASTFM_API_KEY:
        try:
            encoded_name = urllib.parse.quote(artist_name)
            async with httpx.AsyncClient(timeout=8.0) as client:
                lfm_response = await client.get(
                    f"https://ws.audioscrobbler.com/2.0/?method=artist.gettoptags"
                    f"&artist={encoded_name}&api_key={LASTFM_API_KEY}&format=json"
                )
            if lfm_response.status_code == 200:
                tags        = lfm_response.json().get("toptags", {}).get("tag", [])
                strong_tags = [t["name"] for t in tags if t.get("count", 0) >= 10]
                valid_tags  = [t for t in strong_tags if is_valid_genre(t)]
                all_genres.extend(valid_tags[:8])
                print(f"[GENRE] Last.fm artist tags for {artist_name}: {valid_tags[:8]}")
        except Exception as e:
            print(f"[GENRE] Last.fm artist error: {e}")

    # ── 4. Discogs artist-level ───────────────────────────────────────────────
    if DISCOGS_TOKEN:
        try:
            encoded_name = urllib.parse.quote(artist_name)
            headers      = {
                "User-Agent":    "RaveFM/1.0",
                "Authorization": f"Discogs token={DISCOGS_TOKEN}",
            }
            async with httpx.AsyncClient(timeout=8.0) as client:
                search_resp = await client.get(
                    f"https://api.discogs.com/database/search?q={encoded_name}&type=artist",
                    headers=headers,
                )
            if search_resp.status_code == 200:
                results           = search_resp.json().get("results", [])
                discogs_artist_id = None
                for r in results[:3]:
                    if r.get("title", "").lower() == artist_name.lower():
                        discogs_artist_id = r["id"]
                        break
                if not discogs_artist_id and results:
                    discogs_artist_id = results[0]["id"]

                if discogs_artist_id:
                    async with httpx.AsyncClient(timeout=8.0) as client:
                        releases_resp = await client.get(
                            f"https://api.discogs.com/artists/{discogs_artist_id}/releases"
                            f"?per_page=10&sort=year&sort_order=desc",
                            headers=headers,
                        )
                    if releases_resp.status_code == 200:
                        releases   = releases_resp.json().get("releases", [])
                        master_id  = None
                        release_id = None
                        for r in releases:
                            if r.get("type") == "master" and r.get("role") == "Main":
                                master_id = r["id"]
                                break
                        if not master_id:
                            for r in releases:
                                if r.get("role") == "Main" and r.get("type") == "release":
                                    release_id = r["id"]
                                    break

                        styles = []
                        if master_id:
                            async with httpx.AsyncClient(timeout=8.0) as client:
                                master_resp = await client.get(
                                    f"https://api.discogs.com/masters/{master_id}",
                                    headers=headers,
                                )
                            if master_resp.status_code == 200:
                                master_data = master_resp.json()
                                styles      = master_data.get("styles", []) + master_data.get("genres", [])
                        elif release_id:
                            async with httpx.AsyncClient(timeout=8.0) as client:
                                release_resp = await client.get(
                                    f"https://api.discogs.com/releases/{release_id}",
                                    headers=headers,
                                )
                            if release_resp.status_code == 200:
                                release_data = release_resp.json()
                                styles       = release_data.get("styles", []) + release_data.get("genres", [])

                        valid_styles = [s for s in styles if is_valid_genre(s)]
                        all_genres.extend(valid_styles)
                        print(f"[GENRE] Discogs artist for {artist_name}: {valid_styles}")
        except Exception as e:
            print(f"[GENRE] Discogs artist error: {e}")

    # ── 5. MusicBrainz ───────────────────────────────────────────────────────
    try:
        encoded_name = urllib.parse.quote(artist_name)
        async with httpx.AsyncClient(timeout=8.0) as client:
            mb_response = await client.get(
                f"https://musicbrainz.org/ws/2/artist/?query=artist:{encoded_name}&fmt=json&limit=1",
                headers={"User-Agent": "RaveFM/1.0 (contact@ravefm.app)"},
            )
        if mb_response.status_code == 200:
            artists = mb_response.json().get("artists", [])
            if artists:
                tags    = artists[0].get("tags", [])
                tags    = sorted(tags, key=lambda x: x.get("count", 0), reverse=True)
                genres  = [t["name"] for t in tags if is_valid_genre(t["name"])][:8]
                all_genres.extend(genres)
                print(f"[GENRE] MusicBrainz for {artist_name}: {genres}")
    except Exception as e:
        print(f"[GENRE] MusicBrainz error: {e}")

    if all_genres:
        return await cache_and_return(all_genres, "Final merged genres")

    entry = ArtistCache(
        artist_name = artist_name,
        track_name  = track_name,
        genres      = "",
    )
    db.add(entry)
    await db.commit()
    print(f"[GENRE] No genres found for {artist_name} - {track_name}")
    return None


async def commit_previous_track(db: AsyncSession):
    state = current_track_state
    if state["track_id"] is None:
        return
    if state["progress_ms"] is None or state["duration_ms"] is None:
        print("[TRACKER] Skipping commit — progress or duration is None")
        return
    if state["duration_ms"] == 0:
        print("[TRACKER] Skipping commit — duration is 0")
        return

    commit_key = f"{state['track_id']}_{state['progress_ms']}"
    if _last_commit_key["key"] == commit_key:
        print(f"[TRACKER] Skipping duplicate commit for {state['track_name']}")
        return
    _last_commit_key["key"] = commit_key

    now            = datetime.now(TIMEZONE)
    progress_pct   = round(state["progress_ms"] / state["duration_ms"] * 100, 2)
    skipped        = was_skipped(state["progress_ms"], state["duration_ms"])
    primary_artist = state["artists"].split(",")[0].strip() if state["artists"] else None

    auto_mood = derive_mood_from_genre(state.get("primary_genre"))

    play = TrackPlay(
        track_id         = state["track_id"],
        track_name       = state["track_name"],
        artists          = state["artists"],
        primary_artist   = primary_artist,
        album            = state["album"],
        album_art_url    = state["album_art_url"],
        duration_ms      = state["duration_ms"],
        progress_ms      = state["progress_ms"],
        was_skipped      = skipped,
        listened_at      = now,
        progress_pct     = progress_pct,
        hour_of_day      = now.hour,
        day_of_week      = now.strftime("%A"),
        month            = now.strftime("%B"),
        auto_mood        = auto_mood,
        primary_genre    = state.get("primary_genre"),
        artist_spotify_id = state.get("artist_id"),
    )

    db.add(play)
    await db.commit()
    print(f"[TRACKER] Committed: {state['track_name']} | skipped: {skipped} | mood: {auto_mood} | genre: {state.get('primary_genre')}")