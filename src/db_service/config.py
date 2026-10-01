import os
from pathlib import Path

from core.logging_config import setup_logging
from dotenv import load_dotenv
from sqlmodel import create_engine

# Configured here rather than in main.py because this is the lowest-level
# module every entry point imports, and the warning below needs a handler.
logger = setup_logging("db_service")

# Pinned to this directory rather than left to find_dotenv(), which walks *up*
# from the working directory: a .env at the repo root was being picked up by
# anything started anywhere inside the repo, so a test run or a stray uvicorn
# got production credentials it never asked for. An explicit path makes the
# database a property of this service, not of where a process was launched.
load_dotenv(Path(__file__).parent / ".env", override=False)

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    logger.warning(
        "DATABASE_URL is not set. Database operations will fail until it is configured."
    )

engine = create_engine(DATABASE_URL) if DATABASE_URL else None
