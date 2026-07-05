"""Uvicorn entrypoint: load config and serve the middleware."""
from __future__ import annotations

import logging

import uvicorn

from middleware.app import create_app
from middleware.config import load_config
from middleware.paths import config_path


def main() -> None:
    cfg = load_config(config_path())
    logging.basicConfig(level=getattr(logging, cfg.log.level.upper(), logging.INFO))
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level=cfg.log.level)


if __name__ == "__main__":
    main()
