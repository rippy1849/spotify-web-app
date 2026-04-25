from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float
from sqlalchemy.sql import func
from database import Base


class TrackPlay(Base):
    __tablename__ = "track_plays"

    id               = Column(Integer, primary_key=True, index=True)
    track_id         = Column(String, nullable=False)
    track_name       = Column(String, nullable=False)
    artists          = Column(String, nullable=False)
    primary_artist   = Column(String, nullable=True)
    album            = Column(String, nullable=False)
    album_art_url    = Column(String, nullable=True)
    duration_ms      = Column(Integer, nullable=False)
    progress_ms      = Column(Integer, nullable=False)
    was_skipped      = Column(Boolean, default=False)
    listened_at      = Column(DateTime, server_default=func.now())
    progress_pct     = Column(Float, nullable=False)
    hour_of_day      = Column(Integer, nullable=True)
    day_of_week      = Column(String, nullable=True)
    month            = Column(String, nullable=True)
    auto_mood        = Column(String, nullable=True)
    primary_genre    = Column(String, nullable=True)
    artist_spotify_id = Column(String, nullable=True)


class ArtistCache(Base):
    __tablename__ = "artist_cache"

    id          = Column(Integer, primary_key=True, index=True)
    artist_name = Column(String, nullable=False)
    track_name  = Column(String, nullable=True)
    genres      = Column(String, nullable=True)
    image_url   = Column(String, nullable=True)
    fetched_at  = Column(DateTime, server_default=func.now())

class ArtistSpotifyID(Base):
    __tablename__ = "artist_spotify_ids"

    id          = Column(Integer, primary_key=True, index=True)
    artist_name = Column(String, nullable=False, unique=True)
    spotify_id  = Column(String, nullable=False)