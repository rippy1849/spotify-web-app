from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy import text
DATABASE_URL = "sqlite+aiosqlite:///./spotify_history.db"

engine = create_async_engine(DATABASE_URL, echo=False)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

class Base(DeclarativeBase):
    pass

async def init_db():
    async with engine.begin() as conn:
        from models import TrackPlay, ArtistCache, ArtistSpotifyID, LikedSong
        await conn.run_sync(Base.metadata.create_all)
        # Add local_image column if it doesn't exist yet
        try:
            await conn.execute(text("ALTER TABLE artist_cache ADD COLUMN local_image TEXT"))
        except Exception:
            pass  # Column already exists