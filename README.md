# 🎵 RAVE FM

A self-hosted Spotify listening tracker and analytics dashboard with real-time mood detection, artist graphs, and deep listening statistics.

![Python](https://img.shields.io/badge/Python-3.11+-ff00ff?style=flat-square)
![FastAPI](https://img.shields.io/badge/FastAPI-latest-00ffff?style=flat-square)
![SQLite](https://img.shields.io/badge/SQLite-aiosqlite-ffff00?style=flat-square)

---

## ✨ Features

- **Real-time tracking** — polls Spotify every 5 seconds and logs every track play
- **Skip detection** — knows when you bailed and at what point in the song
- **Auto mood tagging** — classifies every play into moods like EUPHORIC, CHILL, BROODING, and more
- **Genre detection** — pulls genre data from Last.fm, Discogs, and MusicBrainz with intelligent caching
- **Artist Graph** — interactive Three.js force graph of artist collaborations with dynamic node sizing
- **Liked Songs Graph** — same graph but built from your Spotify library
- **Year in Review** — full Spotify Wrapped-style recap for any year in your history
- **Time Machine** — travel back to any date and see exactly what you listened to
- **Compare** — head-to-head artist stats with degrees of separation
- **Top Items** — Spotify's affinity ranking vs your own tracked data
- **Following** — who you follow vs who you actually listen to
- **Rave Mode** — immersive full-screen visualiser that reacts to your current mood and album art colours
- **Mosaic** — wall of album art sorted by play count
- **Vibe** — auto mood tracking with genre and hour breakdowns
- **Sessions, Streaks, Evolution, Calendar, Clock** — and much more

---

## 🛠 Tech Stack

- **Backend** — Python, FastAPI, SQLAlchemy (async), SQLite
- **Frontend** — Jinja2 templates, Three.js, D3.js, Chart.js
- **APIs** — Spotify, Last.fm, Discogs, MusicBrainz

---

## 🚀 Setup

### 1. Clone the repository

    git clone https://github.com/rippy1849/spotify-web-app.git
    cd rave-fm

### 2. Create a virtual environment

    python -m venv venv

    # Windows
    venv\Scripts\activate

    # Mac/Linux
    source venv/bin/activate

### 3. Install dependencies

    pip install fastapi uvicorn sqlalchemy aiosqlite httpx python-dotenv jinja2

### 4. Get your API keys

#### 🟢 Spotify

1. Go to https://developer.spotify.com/dashboard and log in
2. Click **Create App**
3. Fill in the app name and description — anything works
4. Set the **Redirect URI** to exactly: `http://127.0.0.1:8000/auth-redirect`
5. Check **Web API** under APIs used
6. Click **Save**
7. On your app dashboard click **Settings**
8. Copy your **Client ID** and **Client Secret**

> ⚠️ The Redirect URI must match exactly — including no trailing slash. If it does not match you will get an auth error.

#### 🟡 Last.fm

1. Go to https://www.last.fm/api/account/create and log in or create a free account
2. Fill in the application name and description — anything works
3. Submit the form
4. Copy your **API Key** from the confirmation page

Last.fm is used for track and artist genre tag lookups. The free tier is sufficient — no payment required.

#### 🔵 Discogs

1. Go to https://www.discogs.com/settings/developers and log in or create a free account
2. Scroll down to **Generate new token**
3. Click the button and copy the generated **Personal Access Token**

Discogs is used as a secondary genre source when Last.fm does not return strong enough tags. The free tier is sufficient.

---

### 5. Create your .env file

Create a file called `.env` in the root of the project with the following contents:

    SPOTIFY_CLIENT_ID=your_spotify_client_id_here
    SPOTIFY_CLIENT_SECRET=your_spotify_client_secret_here
    LASTFM_API_KEY=your_lastfm_api_key_here
    DISCOGS_TOKEN=your_discogs_token_here

> ⚠️ Never commit this file. Add `.env` to your `.gitignore`.

---

### 6. Run the app

    uvicorn main:app --reload --port 8000

Open your browser and go to http://127.0.0.1:8000

Click **LOGIN WITH SPOTIFY** and authorise the app. Once connected, play something on Spotify and the tracker will start recording automatically. The poller starts as a background task on server launch and polls every 5 seconds.

---

## 📁 Project Structure

    rave-fm/
    ├── main.py                  # FastAPI routes and all endpoints
    ├── tracker.py               # Spotify polling, genre detection, mood logic
    ├── models.py                # SQLAlchemy database models
    ├── database.py              # Database engine and session setup
    ├── .env                     # Your API keys — never commit this
    ├── token_store.json         # Auto-generated OAuth token cache
    ├── spotify_history.db       # SQLite database — auto-created on first run
    └── templates/               # Jinja2 HTML templates
        ├── index.html
        ├── now_playing.html
        ├── stats.html
        ├── artist_graph.html
        ├── artist_dive.html
        ├── artist_search.html
        ├── liked_songs.html
        ├── compare.html
        ├── top_items.html
        ├── following.html
        ├── rave.html
        ├── vibe.html
        ├── time_machine.html
        ├── year_review.html
        ├── streaks.html
        ├── sessions.html
        ├── evolution.html
        ├── mosaic.html
        ├── calendar.html
        ├── clock.html
        ├── skip_heatmap.html
        ├── recap.html
        ├── search.html
        ├── recently_played.html
        ├── queue.html
        ├── avoiding.html
        └── debug_llm.html

---

## 🗄 Database

The app uses SQLite and creates `spotify_history.db` automatically on first run. Four tables are used:

| Table | Description |
|---|---|
| `track_plays` | Every track play with mood, genre, skip data, progress percentage, and timestamps |
| `artist_cache` | Cached genre tag and artist image lookups with 7-day freshness |
| `artist_spotify_ids` | Stored Spotify artist IDs for fast image lookups |
| `liked_songs` | Your Spotify liked songs library synced via /sync-liked-songs |

---

## 🔑 Spotify Scopes

The app requests the following Spotify OAuth scopes:

| Scope | Used For |
|---|---|
| `user-read-currently-playing` | Real-time now playing polling |
| `user-read-playback-state` | Playback progress and state |
| `user-read-recently-played` | Recently played tracks page |
| `user-top-read` | Top Items page — Spotify affinity ranking |
| `user-follow-read` | Following page — who you follow |

---

## 🎵 How Mood Detection Works

Every track committed to the database is automatically assigned a mood based on its genre tags. The mood is derived by matching genre strings against a detailed mood map with over 200 genre terms across 13 mood categories:

| Mood | Example Genres |
|---|---|
| EUPHORIC | trance, progressive house, big room, eurodance |
| RAVE | techno, acid house, dark minimal, berlin techno |
| INTENSE | hardstyle, metal, hardcore punk, riddim |
| PUMPED | drum and bass, dubstep, breakbeat, trap edm |
| BROODING | hip hop, grime, dark trap, witch house |
| MELANCHOLIC | chillstep, shoegaze, darkwave, bedroom pop |
| CHILL | deep house, synthwave, lo-fi, trip hop, chillout |
| PEACEFUL | ambient, classical, folk, new age, acoustic |
| NOSTALGIC | 80s, classic rock, disco, yacht rock, city pop |
| FOCUSED | jazz, idm, math rock, progressive rock |
| ROMANTIC | r&b, soul, bossa nova, smooth jazz |
| HAPPY | pop, k-pop, reggae, dancehall, funk pop |
| ENERGETIC | electronic, alternative rock, indie rock, funk |

---

## 🔍 How Genre Detection Works

Genre detection uses a five-source pipeline with caching:

1. **Last.fm track tags** — most specific, checked first
2. **Discogs track lookup** — cross-referenced with release styles
3. **Last.fm artist tags** — fallback if no track-level tags found
4. **Discogs artist lookup** — scored candidate matching to find the correct artist
5. **MusicBrainz** — final fallback with confidence scoring

Results are cached in `ArtistCache` for 7 days to avoid repeat API calls. An implausible genre filter removes contradictory tags — for example, Heavy Metal is stripped from an artist whose other tags indicate electronic or hip hop.

---

## 📊 Loyalty Score Formula

The loyalty score shown on artist pages and in Compare is calculated as:

    loyalty = (completion_rate × 0.40)
            + (min(plays, 100) / 100 × 30)
            + (min(longest_streak, 30) / 30 × 20)
            + (min(active_days, 50) / 50 × 10)

- **40%** — how often you finish their songs
- **30%** — total play count, capped at 100
- **20%** — longest consecutive day streak, capped at 30
- **10%** — total active days, capped at 50

A score of 100 means you listen constantly, never skip, and have long streaks.


# 📊 RAVE FM — Features & Pages

---

## 🏠 Home (`/`)

The magazine-style dashboard hub. Shows live now playing with album art, animated canvas backgrounds for each feature card, and quick access to all pages. Displays connection status and a secondary grid of smaller feature links.

---

## ▶ Now Playing (`/now-playing`)

Live view of your current Spotify track.

- Album art with animated rainbow border
- Track name, artist, album
- Playing / paused status
- Real-time progress bar
- Direct link to open in Spotify
- Auto-refreshes every 10 seconds

---

## ◈ Stats (`/stats`)

Full listening statistics dashboard.

**Totals**
- Total plays, total skips, skip rate
- Total hours listened
- Unique tracks, unique artists
- Average skip point percentage
- Longest no-skip streak
- Busiest day with play count

**Charts**
- Plays by hour of day
- Skip rate % by hour of day
- Plays by day of week
- Plays by month

**This Week**
- Top 5 tracks of the last 7 days
- Top 5 artists of the last 7 days

**Tracks**
- Top 10 most played tracks
- Top 10 most skipped tracks
- Tracks you always complete (3+ plays, 0 skips)

**Artists**
- Top 10 most played artists
- Top 10 most skipped artists
- Top 10 most loved artists (lowest skip rate, min 5 plays)

**Albums**
- Top 10 most played albums
- Top 10 most skipped albums

**Recent History**
- Last 20 plays with track, artist, album, progress %, status, and timestamp

---

## 🎤 Artist Deep Dive (`/artist/{name}`)

Full profile page for any artist in your history.

**Hero Stats**
- Artist photo, genres, total plays, hours listened, skip rate, current streak, dominant mood

**Core Stats**
- Total plays, hours, skip rate, completion rate
- Unique tracks, unique albums, active days, plays per day

**Loyalty Scores**
- Loyalty score (0-100) with formula breakdown
- Consistency % (active days ÷ days since first play)
- Longest consecutive day streak
- Visual score bars for completion rate, loyalty, consistency, avg progress

**Charts**
- Monthly plays + skip rate overlay chart
- Plays by hour of day

**Listening Personality**
- Peak hour, peak day
- Night owl %, morning %, weekend %
- Binge days (5+ plays), comebacks (7+ day gaps), longest gap, rediscoveries

**Time of Day Breakdown**
- Morning / afternoon / evening / night split

**Mood Breakdown**
- Bar chart of all moods with counts

**Tracks**
- Top 10 most played tracks with skip rates
- Top 5 always completed tracks (never skipped, 3+ plays)
- Top 5 most skipped tracks

**Timeline**
- First play date, last play date, days in history
- Busiest single day, top month, top album
- Longest complete streak, longest skip streak

**Skip Analysis**
- Avg progress %, avg skip point %, plays per week

**Listened Alongside**
- Artists most often played in the same 30-minute window

---

## 🔍 Artist Search (`/artist-search`)

Searchable grid of every artist in your history sorted by play count. Click any artist to go to their deep dive page.

---

## ⚡ Compare (`/compare`)

Head-to-head comparison of any two artists.

**Artist Headers**
- Photos, play counts, hours

**Degrees of Separation**
- Number of collaboration hops between the two artists
- Connection path with shared track names at each step
- Direct connection songs shown as pills
- ∞ shown if no connection found

**Loyalty & Consistency Score Cards**
- Side-by-side loyalty scores and consistency percentages

**Core Stats Bars**
- 14 metrics compared side by side with animated bars and winner indicators:
  total plays, hours, skip rate, completion rate, unique tracks, unique albums, avg progress, active days, plays per day, longest streak, binge days, best day plays, comebacks, days in history

**Listening Personality**
- Night owl %, morning %, weekend %, mood variety, genre variety
- Peak hour, peak day, dominant mood, top genre, top album, first played, last played, busiest day
- Avg skip point, top track, most skipped track

**Charts**
- Monthly play trend (line chart, both artists)
- Listening by hour (bar chart, both artists)

---

## 🕸 Artist Graph (`/artist-graph`)

Interactive Three.js force-directed graph of artist collaborations from your play history.

**Nodes**
- Each artist is a node sized by play count (logarithmic infinite scaling)
- Artist photos loaded from Spotify as node textures
- Nodes sized proportionally — more plays = bigger node
- Cluster-colour coded by connected component

**Edges**
- Straight lines connecting artists who appear on the same track together
- Hover a line to see the shared track name(s) in a tooltip

**Interaction**
- Left drag to pan, scroll to zoom
- Click a node to focus — dims all unconnected nodes and links
- Hover a node to see play count and connection count
- Search bar with autofill to find and focus any artist
- Dimmed nodes stay dim on hover (no false highlighting)

**Controls**
- ↺ RESET — resets camera and clears focus
- ❄ FREEZE / ▶ UNFREEZE — stops or resumes the force simulation
- ⚡ SPREAD / 🔗 TIGHTEN — adjusts charge force
- 🎨 COLORS — toggles cluster colour mode
- ⬡ DYN SIZE — toggles dynamic logarithmic node sizing
- 🌐 3D MODE — switches to full 3D orbit mode with d3-force-3d

---

## ❤️ Liked Songs (`/liked-songs`)

Statistics about your Spotify liked songs library.

**Sync**
- Sync button fetches your full Spotify library, upserts all songs, and removes any that have been unliked

**Core Stats**
- Total liked songs, total listen time, unique artists, unique albums
- Average song length, adds per month, library age in days

**Insights**
- % actually played (via this app), % never played since tracking started
- Longest adding streak (consecutive days)
- Peak add hour, peak add day of week

**Charts**
- Songs added per month (bar chart)
- Adds by hour of day (bar chart)
- Adds by day of week (bar chart)

**Top Artists & Albums**
- Top liked artists with bar visualisation and links to artist deep dive
- Top liked albums with artist and count

**Liked But Often Skipped**
- Liked songs with the highest skip rate from your play history

**Recently Added**
- Last 20 songs added to your library with dates

**First & Latest Liked**
- Full track cards for the oldest and newest liked songs

**Degrees of Separation**
- Search two artists from your liked songs library
- Shows how many collaboration hops connect them
- Connection path with artist pills, arrows, and shared track names
- Direct connections show song pill badges

---

## 🕸 Liked Songs Graph (`/liked-songs-graph`)

Same interactive Three.js graph as the artist graph but built entirely from your liked songs library instead of play history. Node size represents number of liked songs by that artist. All the same controls and interactions apply.

---

## 🎵 Vibe (`/vibe`)

Auto mood tracking dashboard.

- Current vibe (dominant mood from last 10 plays)
- Mood distribution doughnut chart
- Top genres bar chart
- Dominant mood by hour — 24-cell grid showing your most common mood at each hour
- Dominant mood by day of week — table
- Genre to typical mood map — table
- Recent tracks with auto mood tag, genre, and timestamp

---

## 🌐 Rave Mode (`/rave`)

Immersive full-screen music visualiser.

- Album art displayed with glow pulse animation
- Track name, artist, dominant mood badge
- Real-time progress bar that ticks forward between Spotify polls
- **13 visual themes** each with unique canvas animations:
  EUPHORIC (plasma), RAVE (tunnel vortex), INTENSE (lightning + fire), PUMPED (equalizer bars), CHILL (lava lamp orbs), BROODING (fractal geometry), PEACEFUL (aurora + snowflakes), MELANCHOLIC (rain + ripples), HAPPY (disco + rainbow rings), ROMANTIC (plasma + hearts), NOSTALGIC (VHS tunnel), FOCUSED (matrix + geometry), ENERGETIC (Lissajous + spirals)
- AUTO mode automatically picks theme based on current mood
- 🎨 AUTO COLOR — samples album art colours and applies them to the current visual theme
- Theme selector and nav fade in on mouse movement
- Track updates live every 5 seconds without page reload

---

## 🖼 Mosaic (`/mosaic`)

Full-screen wall of album art from your listening history.

- Grid of every album you've listened to, sorted by play count
- Cell size auto-calculates to fill the screen perfectly
- Hover a cell to see album name, artist, play count
- Click a cell for a detail popup with plays, skips, skip rate, avg listen %
- Hot/warm glow borders for heavily played albums
- Search bar to filter by album or artist name

---

## 📅 Calendar (`/calendar`)

365-day activity heatmap.

- Full year grid coloured by play intensity
- Summary: active days, total plays, total minutes, best single day
- Click any day to see that day's stats

---

## 🕐 Listening Clock (`/clock`)

24-hour radial clock visualisation.

- Each hour segment sized by play count
- Colour-coded from green (low skip rate) to pink (high skip rate)
- Hover any segment for plays and skip rate tooltip

---

## ⏰ Time Machine (`/time-machine/{date}`)

Travel back to any date in your listening history.

**Date Picker**
- Calendar picker showing only dates with data
- Earliest and latest dates shown

**Day Stats**
- Tracks played, skip rate, minutes, unique artists, unique tracks
- Skips, peak hour, comparison vs your average day

**Highlights**
- Dominant vibe, top genre, top artist, top track
- First and last track of the day with album art and times

**Full Timeline**
- Every play of that day in chronological order with mood pill and skip/complete badge

---

## 🔥 Streaks (`/streaks`)

Streak and consistency tracking.

- Current day streak (consecutive days listening)
- Longest day streak (all-time record)
- Current no-skip streak (tracks in a row without skipping)
- Longest no-skip streak (all-time record)
- Artist loyalty streak (most consecutive plays of one artist)
- Activity rate % (active days ÷ total days tracked)
- Bar chart of plays per day for the last 30 days

---

## ⚡ Sessions (`/sessions`)

Listening session detection and stats.

**Session Summary**
- Total sessions detected, average duration, average tracks per session, longest session

**Top Sessions**
- Sessions ranked by length, showing:
  date, time range, duration, track count, skip count, skip rate, dominant mood, top artist, top genre, first and last track, album art

Sessions are detected by gaps of 30+ minutes between plays.

---

## 🧬 Evolution (`/evolution`)

How your taste has shifted over time.

- Line chart of top 5 artists' play counts month by month
- Month-by-month grid cards showing:
  top track of that month, top 5 artists with play counts

---

## 🎯 Skip Heatmap (`/skip-heatmap`)

Where in songs you tend to bail.

**Stats**
- Total skips, early skips (&lt;20%), mid skips (20-50%), late skips (&gt;50%)

**Chart**
- Bar chart of skip counts by 5% progress buckets, colour-coded from purple (early) to red (late)

**Top Skipped Track Per Zone**
- The most-skipped track in each progress bucket

---

## 👻 Avoiding (`/avoiding`)

Tracks you keep starting but never finish.

**Always Skipped**
- Tracks with 5+ plays and 100% skip rate

**Almost Avoiding**
- Tracks with 3+ plays and average progress under 30%

---

## 📋 Daily Recap (`/recap`)

Summary of everything you listened to today.

- Tracks played, skipped, skip rate, minutes listened, unique tracks, unique artists, peak hour
- Top track of the day, top artist of the day
- First and last track with album art and times
- Full chronological play log with mood and skip/complete badges

---

## 🔍 Search (`/search`)

Live search across your full play history.

- Searches track name, artist, and album simultaneously
- Results update as you type with 200ms debounce
- Shows album art, track info, timestamp, and played/skipped badge

---

## ⏮ Recently Played (`/recently-played`)

Last 50 tracks from the Spotify recently played API.

- Album art, track name, artist, album, timestamp, duration
- Direct links to open each track in Spotify

---

## ⚡ Queue (`/queue`)

Your current Spotify queue.

- Now playing card with album art and live badge
- Up to 15 upcoming tracks with album art, artist, duration
- Direct Spotify links
- Auto-refreshes every 15 seconds

---

## 🏆 Top Items (`/top-items`)

Spotify's affinity ranking vs your own tracked data.

**Time Ranges**
- Last 4 weeks, last 6 months, all time

**Top Artists**
- Spotify's rank 1-20 with photo, genres
- Your own tracked rank shown as a badge — green if match, yellow if you rank them higher, pink if lower, grey if untracked

**Top Tracks**
- Spotify's rank 1-20 with album art, artist
- Cross-referenced with your own play count ranking

Explains the difference between Spotify's affinity algorithm and raw play counts.

---

## 👥 Following (`/following`)

Who you follow on Spotify vs who you actually listen to.

**Summary**
- Total following, loyal follows count, ghost follows count, unrecognized fans count

**Loyal Follows**
- Artists you follow AND have 10+ tracked plays — sorted by play count with photos

**Casual Follows**
- Artists you follow and have played but fewer than 10 times

**Ghost Follows**
- Artists you follow but have never played since tracking started — shown dimmed

**Unrecognized Fans**
- Artists you play constantly (10+ plays) but haven't followed on Spotify yet

---

## 🎊 Year in Review (`/review/{year}`)

Full Spotify Wrapped-style annual recap for any year in your history.

**Year Selector**
- Buttons for every year with data in your history

**Hero**
- Large animated year display with listening personality label (Night Owl, Early Bird, Weekend Warrior, All Day Listener)

**Big Numbers**
- Tracks played, hours listened, unique artists, new artists discovered, unique tracks, days with music, skip rate vs prior year, new liked songs added

**Top Mood & Genre**
- Dominant vibe and top genre displayed as hero cards

**Top 5 Artists**
- Photos, names, play counts with links to artist deep dive

**Top 5 Tracks**
- Album art, names, play counts

**Month by Month**
- Animated bar chart showing plays per month across all 12 months

**First & Last Track**
- Full track cards for the first and last song of the year with date and time

**Listening Stats**
- Biggest single day, longest no-skip streak, peak hour, peak day
- Night owl %, morning %, weekend %, most skipped track
- Biggest comeback (longest gap + return date)

**Plays by Hour Chart**
- Bar chart of all plays across the 24 hours of the day for that year

**Mood Breakdown**
- Bar chart of mood distribution for the year

**Top Genres**
- Genre pills sized by frequency

**New Artists Discovered**
- Artists played for the very first time that year, sorted by play count

**Always on Repeat**
- Tracks played every active month of the year

**Ghost Liked Songs**
- Songs in your library you never got around to playing that year

---

## 🛠 Debug Endpoints

| Endpoint | Purpose |
|---|---|
| `/debug-artist-ids` | Which artists are missing Spotify IDs |
| `/debug-fetch-genre/{artist}` | Test Last.fm genre lookup |
| `/debug-fetch-discogs/{artist}` | Test Discogs genre lookup |
| `/debug-discogs-artist/{artist}` | Raw Discogs artist search results |
| `/debug-filter-test` | Test the implausible genre filter |
| `/cleanup-bad-genres` | Re-run genre filter over all stored plays |
| `/clear-genre-cache` | Wipe genre cache to force re-fetching |

---

## ⚠️ Notes

- The app must be running and you must be logged in for tracking to work — the poller starts automatically on server launch
- Genre detection can be slow on first run for a large history as it queries Last.fm, Discogs, and MusicBrainz — results are cached after the first fetch
- The Spotify audio-features endpoint is no longer available to new developer apps — mood detection uses genre tags instead
- OAuth tokens are persisted to `token_store.json` so you do not need to log in again after restarting the server
- This app is intended for personal/local use only and is not designed to be exposed publicly
- Non-music content such as white noise, meditation, binaural beats, and ASMR is automatically filtered from your play history

---

## 📄 License

MIT