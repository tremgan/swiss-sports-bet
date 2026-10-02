import os

from core.logging_config import setup_logging
from sqlmodel import create_engine

# Configured here rather than in main.py because this is the lowest-level
# module every entry point imports, and the warning below needs a handler.
logger = setup_logging("db_service")

# Read from the real environment only: repository secrets in Actions, the
# command line locally. See "There is no `.env`, on purpose" in AGENTS.md
# before reintroducing load_dotenv() here.
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    logger.warning(
        "DATABASE_URL is not set. Database operations will fail until it is configured."
    )

engine = create_engine(DATABASE_URL) if DATABASE_URL else None
