import uvicorn

from app.config import settings
from app.db import init_db

if __name__ == "__main__":
    init_db()
    from app.scheduler import start_background
    start_background()
    import os
    uvicorn.run("app.main:app", host=os.getenv("HOST", "0.0.0.0"), port=settings.port)
