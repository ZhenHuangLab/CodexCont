@echo off
rem One-click entrypoint for CodexCont's installer/service-manager CLI.
rem
rem   codexcont.bat              interactive menu
rem   codexcont.bat install      guided setup (deps + config.toml)
rem   codexcont.bat start        start the proxy in the background
rem   codexcont.bat logs -f      follow the log
rem   codexcont.bat stop         stop it
rem
rem Prefers `uv`; falls back to a plain `python -m middleware.cli` (Method B
rem in INSTALL-GUIDE-AGENT/AGENT.md) when uv isn't installed.
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if %ERRORLEVEL%==0 (
    uv run python -m middleware.cli %*
) else (
    python -m middleware.cli %*
)
