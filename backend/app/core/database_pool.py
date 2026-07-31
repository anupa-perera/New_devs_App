import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
import logging
from ..config import settings

logger = logging.getLogger(__name__)

class DatabasePool:
    def __init__(self):
        self.engine = None
        self.session_factory = None
        
    async def initialize(self):
        """Initialize database connection pool against the application database."""
        # Idempotent: the pool is a process-wide singleton created once at startup and
        # reused for every request. Re-initializing would leak engines/connections.
        if self.session_factory is not None:
            return

        # settings.database_url is a sync-style DSN; SQLAlchemy's async engine needs
        # the asyncpg driver, so rewrite only the scheme. Let any failure propagate —
        # a database we can't reach must surface loudly, never fall back to fake data.
        database_url = settings.database_url.replace(
            "postgresql://", "postgresql+asyncpg://", 1
        )

        # async engines select AsyncAdaptedQueuePool automatically; passing the sync
        # QueuePool is invalid here.
        self.engine = create_async_engine(
            database_url,
            pool_size=20,  # Number of connections to maintain
            max_overflow=30,  # Additional connections when needed
            pool_pre_ping=True,  # Validate connections
            pool_recycle=3600,  # Recycle connections every hour
            echo=False,  # Set to True for SQL debugging
        )

        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )

        logger.info("✅ Database connection pool initialized")
    
    async def close(self):
        """Close database connections"""
        if self.engine:
            await self.engine.dispose()
    
    async def get_session(self) -> AsyncSession:
        """Get database session from pool"""
        if not self.session_factory:
            raise Exception("Database pool not initialized")
        return self.session_factory()

# Global database pool instance
db_pool = DatabasePool()

async def get_db_session() -> AsyncSession:
    """Dependency to get database session"""
    async with db_pool.session_factory() as session:
        yield session
