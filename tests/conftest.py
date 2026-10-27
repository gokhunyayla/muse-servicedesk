import os

os.environ["DEMO_MANAGER_PHONE"] = ""      # tests use the seeded manager phones, whatever .env says

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import User, session


@pytest.fixture
def desk():
    """A TestClient signed in as an admin desk user (created in the test database)."""
    from app.main import app
    with session() as db:
        db.add(User(email="admin@test", name="Desk Admin", password_hash=hash_password("secret-pass-1"), role="admin"))
    c = TestClient(app)
    r = c.post("/login", data={"email": "admin@test", "password": "secret-pass-1"}, follow_redirects=False)
    assert r.status_code == 303 and "sd_session" in c.cookies
    return c
