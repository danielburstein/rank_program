"""Entry point: ``rankrecovery-server``."""

from __future__ import annotations

import argparse
import logging
import os


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Rank Recovery API server")
    parser.add_argument("--host", default=os.environ.get("RANKRECOVERY_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("RANKRECOVERY_PORT", "8420")))
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--log-level", default=os.environ.get("RANKRECOVERY_LOG_LEVEL", "info"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    from rankrecovery.api.app import build_context, create_app

    ctx = build_context(args.database_url)
    app = create_app(ctx)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
