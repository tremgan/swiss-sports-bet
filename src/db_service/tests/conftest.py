import pytest
from fastapi.testclient import TestClient
from main import app, get_session
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

DATABASE_URL = "sqlite:///:memory:"

# StaticPool keeps every connection pointed at the same in-memory database, so
# the app under test and the test itself see the same rows.
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@pytest.fixture(name="session")
def session_fixture():
    # The schema is Alembic-managed in production; unit tests build it directly
    # rather than replaying migrations.
    SQLModel.metadata.drop_all(engine)  # avoid leaking state between tests
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(name="client")
def client_fixture(session: Session):
    def get_test_session():
        yield session

    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
