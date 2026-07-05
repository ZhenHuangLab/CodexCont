#!/usr/bin/env python3
"""Entrypoint: load config.toml and serve the middleware with uvicorn."""
from middleware.server import main

if __name__ == "__main__":
    main()
