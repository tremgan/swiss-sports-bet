import os

from core.logging_config import setup_logging
from dotenv import load_dotenv
from sqlmodel import create_engine

# Configured here rather than in main.py because this is the lowest-level
# module every entry point imports, and the warning below needs a handler.
logger = setup_logging("db_service")

load_dotenv(override=False)  # reads .env from cwd by default

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    logger.warning(
        "DATABASE_URL is not set. Database operations will fail until it is configured."
    )

engine = create_engine(DATABASE_URL) if DATABASE_URL else None
