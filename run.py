"""Start the Smart Campus Multi-Agent System."""
import uvicorn

from campus.config import settings

if __name__ == "__main__":
    uvicorn.run("campus.main:app", host=settings.host, port=settings.port, reload=False)
