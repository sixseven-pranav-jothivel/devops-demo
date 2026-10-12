"""`python -m app`: run the app with a single uvicorn worker on `$HOST:$PORT`."""

import uvicorn

from app.config import get_settings
from app.main import app


def main() -> None:
    """Serve on the configured address. One worker only: the repository is in-process memory."""
    settings = get_settings()
    uvicorn.run(app, host=settings.host, port=settings.port, workers=1, log_level="info")


if __name__ == "__main__":
    main()
