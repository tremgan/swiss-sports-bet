import os

from dotenv import load_dotenv
from sqlmodel import create_engine

from core.logging_config import setup_logging

# Configured here rather than in main.py because this is the lowest-level
# module every entry point imports, and the warning below needs a handler.
logger = setup_logging("db_service")

load_dotenv(override=False)  # reads .env from cwd by default

SQLMODEL_DB_URL = os.getenv("SQLMODEL_DB_URL")

if not SQLMODEL_DB_URL:
    logger.warning(
        "SQLMODEL_DB_URL is not set. Database operations will fail until it is "
        "configured."
    )

engine = create_engine(SQLMODEL_DB_URL) if SQLMODEL_DB_URL else None
