@echo off
rem kb-mcp 命令行包装器：任意目录可用（自动切到仓库根，用仓库内的虚拟环境）
rem 用法： kb.cmd doctor | kb.cmd index --fts-only | kb.cmd search "关键词" | kb.cmd stats
setlocal
cd /d "%~dp0"
".venv\Scripts\python.exe" -m kb %*
exit /b %ERRORLEVEL%