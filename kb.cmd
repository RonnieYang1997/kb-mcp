@echo off
rem kb-mcp CLI wrapper: runnable from any directory (switches to repo root, uses repo venv).
rem Usage: kb.cmd doctor | kb.cmd index --fts-only | kb.cmd search "keywords" | kb.cmd stats
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kb %*
exit /b %ERRORLEVEL%