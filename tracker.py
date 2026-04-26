from datetime import datetime
from zoneinfo import ZoneInfo
from collections import Counter
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import TrackPlay, ArtistCache

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

# ── Non-music filter ──────────────────────────────────────────────────────────
NON_MUSIC_GENRES = {
    "white noise", "brown noise", "pink noise", "green noise", "blue noise",
    "nature sounds", "nature", "ambient noise", "sleep sounds", "rain sounds",
    "sleep", "meditation", "healing", "binaural", "binaural beats", "asmr",
    "spa", "yoga", "relaxation", "study sounds", "focus sounds",
    "soundscape", "field recording", "nature ambient",
}

NON_MUSIC_KEYWORDS = [
    "white noise", "brown noise", "pink noise", "green noise", "blue noise",
    "black noise", "grey noise", "violet noise", "red noise",
    "rain sounds", "rain noise", "rainfall", "thunder sounds",
    "ocean waves", "wave sounds", "ocean sounds", "beach sounds",
    "forest sounds", "nature sounds", "bird sounds", "birdsong",
    "sleep sounds", "sleep music", "sleeping sounds",
    "meditation music", "meditation sounds", "guided meditation",
    "binaural beats", "binaural",
    "asmr", "whisper",
    "fan noise", "air conditioner", "vacuum cleaner",
    "fireplace sounds", "crackling fire",
    "waterfall sounds", "stream sounds",
    "delta waves", "theta waves", "alpha waves",
    "528 hz", "432 hz", "174 hz", "396 hz", "417 hz", "639 hz", "741 hz", "852 hz",
    "solfeggio", "frequency",
    "lofi hip hop radio", "lo-fi hip hop radio",
    "study with me", "focus music",
]

# Genres implausible for electronic/hip hop artists when combined with those genres
IMPLAUSIBLE_GENRE_COMBOS = {
    "heavy metal", "death metal", "black metal", "thrash metal",
    "grindcore", "metalcore", "deathcore", "doom metal",
    "classical", "orchestra", "symphony",
    "country", "bluegrass", "gospel",
    "flamenco", "polka",
}


def is_non_music(track_name: str, genre_str: str) -> bool:
    """Return True if this track appears to be non-music content."""
    if not track_name:
        return False

    track_lower = track_name.lower().strip()
    genre_lower = (genre_str or "").lower()

    for keyword in NON_MUSIC_KEYWORDS:
        if keyword in track_lower:
            return True

    if genre_lower:
        for genre in genre_lower.split(","):
            genre = genre.strip()
            if genre in NON_MUSIC_GENRES:
                return True

    return False


def filter_implausible_genres(genres: list) -> list:
    """Remove genres that are implausible given the other genres present."""
    if not genres:
        return genres

    genres_lower = [g.lower().strip() for g in genres]

    electronic_indicators = {
        "electronic", "edm", "dubstep", "drum and bass", "dnb",
        "hip hop", "rap", "trap", "house", "techno", "bass music",
        "glitch hop", "glitch-hop", "funk", "future bass", "chillhop",
        "trance", "breaks", "breakbeat", "jungle", "garage",
    }

    is_electronic = any(
        any(ind in g for ind in electronic_indicators)
        for g in genres_lower
    )

    if is_electronic:
        filtered = [
            g for g in genres
            if g.lower().strip() not in IMPLAUSIBLE_GENRE_COMBOS
        ]
        print(f"[FILTER] is_electronic=True. Before: {genres} After: {filtered}")
        if filtered:
            return filtered

    return genres


# ── Mood derivation ───────────────────────────────────────────────────────────
def derive_mood_from_genre(genre_str: str) -> str:
    """Derive mood from a comma-separated string of genre tags."""
    if not genre_str:
        return "UNKNOWN"

    genres = [g.strip().lower() for g in genre_str.split(",") if g.strip()]

    def matches(genre: str, terms: list) -> bool:
        import re
        for term in terms:
            if genre == term:
                return True
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

    mood_votes = Counter()
    for genre in genres:
        for mood, terms in mood_map.items():
            if matches(genre, terms):
                mood_votes[mood] += 1
                break

    if not mood_votes:
        return "NEUTRAL"

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
    print(f"[GENRE] ========== Fetching genre for: {artist_name} / {track_name} ==========")
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
        deduped   = filter_implausible_genres(deduped)
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
                            valid_styles = filter_implausible_genres(valid_styles)
                            if valid_styles:
                                all_genres.extend(valid_styles)
                                print(f"[GENRE] Discogs track styles for {track_name}: {valid_styles}")
        except Exception as e:
            print(f"[GENRE] Discogs track error: {e}")

    # ── Early exit if track-specific genres found ─────────────────────────────
    if len(all_genres) >= 1:
        print(f"[GENRE] All genres before cache (track-specific): {all_genres}")
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
                results = search_resp.json().get("results", [])

                # Collect all candidates whose base name matches
                candidates = []
                for r in results[:10]:
                    title = r.get("title", "").strip()
                    base  = title.split("(")[0].strip().lower()
                    if base == artist_name.lower():
                        candidates.append(r)

                # Score each candidate
                best_id    = None
                best_score = -1

                known_genres_lower = set(g.lower() for g in all_genres)

                electronic_keywords = [
                    "electronic", "funk", "jazz", "hip hop", "saxophone",
                    "producer", "edm", "bass", "musician", "dj", "beat",
                    "remix", "synthesizer", "keyboard", "turntable",
                    "drum", "sample", "mix", "dance", "club",
                ]

                for candidate in candidates:
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            profile_resp = await client.get(
                                f"https://api.discogs.com/artists/{candidate['id']}",
                                headers=headers,
                            )
                        if profile_resp.status_code != 200:
                            continue

                        profile      = profile_resp.json()
                        profile_text = (profile.get("profile") or "").lower()
                        score        = 0

                        for kw in electronic_keywords:
                            if kw in profile_text:
                                score += 1

                        if candidate["title"].lower() == artist_name.lower():
                            score += 5

                        if candidate.get("thumb"):
                            score += 2

                        async with httpx.AsyncClient(timeout=5.0) as client:
                            releases_check = await client.get(
                                f"https://api.discogs.com/artists/{candidate['id']}/releases"
                                f"?per_page=5&sort=year&sort_order=desc",
                                headers=headers,
                            )
                        if releases_check.status_code == 200:
                            rel_data = releases_check.json().get("releases", [])
                            for rel in rel_data:
                                if rel.get("role") == "Main":
                                    score += 3
                                    break

                        print(f"[GENRE] Discogs candidate: {candidate['title']} (id:{candidate['id']}) score:{score}")

                        if score > best_score:
                            best_score = score
                            best_id    = candidate["id"]

                    except Exception as e:
                        print(f"[GENRE] Discogs candidate check error: {e}")
                        continue

                if best_id and best_score >= 3:
                    async with httpx.AsyncClient(timeout=8.0) as client:
                        releases_resp = await client.get(
                            f"https://api.discogs.com/artists/{best_id}/releases"
                            f"?per_page=20&sort=year&sort_order=desc",
                            headers=headers,
                        )
                    if releases_resp.status_code == 200:
                        releases      = releases_resp.json().get("releases", [])
                        main_releases = [r for r in releases if r.get("role") == "Main"]
                        master_id     = None
                        release_id    = None

                        for r in main_releases:
                            if r.get("type") == "master":
                                master_id = r["id"]
                                break
                        if not master_id:
                            for r in main_releases:
                                if r.get("type") == "release":
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
                        valid_styles = filter_implausible_genres(valid_styles)
                        if valid_styles:
                            all_genres.extend(valid_styles)
                            print(f"[GENRE] Discogs artist for {artist_name} (id:{best_id} score:{best_score}): {valid_styles}")
                else:
                    print(f"[GENRE] Discogs no confident match for {artist_name} (best score: {best_score}) — skipping")

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
                mb_artist      = artists[0]
                mb_name        = mb_artist.get("name", "").strip()
                mb_score       = mb_artist.get("score", 0)
                name_matches   = mb_name.lower() == artist_name.lower()
                high_confidence = mb_score >= 90

                if not name_matches:
                    print(f"[MB] Name mismatch: searching '{artist_name}' got '{mb_name}' — skipping")
                elif not high_confidence:
                    print(f"[MB] Low confidence {mb_score} for '{mb_name}' — skipping")
                else:
                    print(f"[MB] Found artist: {mb_name} (score:{mb_score})")
                    tags    = mb_artist.get("tags", [])
                    tags    = sorted(tags, key=lambda x: x.get("count", 0), reverse=True)
                    print(f"[MB] Raw tags: {[(t['name'], t.get('count',0)) for t in tags[:10]]}")
                    genres  = [t["name"] for t in tags if is_valid_genre(t["name"])][:8]

                    # Filter using combined context so electronic artists
                    # don't get implausible tags from wrong matches
                    combined   = list(set(all_genres + genres))
                    filtered   = filter_implausible_genres(combined)
                    new_genres = [
                        g for g in genres
                        if g.lower().strip() in {f.lower().strip() for f in filtered}
                    ]
                    print(f"[MB] After filter: {new_genres}")
                    all_genres.extend(new_genres)
    except Exception as e:
        print(f"[GENRE] MusicBrainz error: {e}")

    if all_genres:
        print(f"[GENRE] Final all_genres before cache: {all_genres}")
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

    # ── Filter out non-music content ──────────────────────────────────────────
    if is_non_music(state.get("track_name", ""), state.get("primary_genre", "")):
        print(f"[TRACKER] Skipping non-music track: {state.get('track_name')}")
        return

    now            = datetime.now(TIMEZONE)
    progress_pct   = round(state["progress_ms"] / state["duration_ms"] * 100, 2)
    primary_artist = state["artists"].split(",")[0].strip() if state["artists"] else None

    # ── Round up near-complete plays to 100% ──────────────────────────────────
    if progress_pct >= 95:
        progress_pct = 100.0
        progress_ms  = state["duration_ms"]
    else:
        progress_ms  = state["progress_ms"]

    skipped   = was_skipped(progress_ms, state["duration_ms"])
    auto_mood = derive_mood_from_genre(state.get("primary_genre"))

    play = TrackPlay(
        track_id          = state["track_id"],
        track_name        = state["track_name"],
        artists           = state["artists"],
        primary_artist    = primary_artist,
        album             = state["album"],
        album_art_url     = state["album_art_url"],
        duration_ms       = state["duration_ms"],
        progress_ms       = progress_ms,
        was_skipped       = skipped,
        listened_at       = now,
        progress_pct      = progress_pct,
        hour_of_day       = now.hour,
        day_of_week       = now.strftime("%A"),
        month             = now.strftime("%B"),
        auto_mood         = auto_mood,
        primary_genre     = state.get("primary_genre"),
        artist_spotify_id = state.get("artist_id"),
    )

    db.add(play)
    await db.commit()
    print(f"[TRACKER] Committed: {state['track_name']} | skipped: {skipped} | mood: {auto_mood} | genre: {state.get('primary_genre')}")