import uvicorn

from app.config import settings
from app.db import init_db

if __name__ == "__main__":
    init_db()
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port)
