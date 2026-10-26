"""Command-line entry point."""

from __future__ import annotations

import uvicorn


def main() -> None:
    """Run the API server."""

    uvicorn.run("subtitleforge.api:app", host="0.0.0.0", port=8600)


if __name__ == "__main__":
    main()
