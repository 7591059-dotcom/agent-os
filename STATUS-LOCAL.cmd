@echo off
setlocal DisableDelayedExpansion
call "%~dp0scripts\windows-local.cmd" status
set "AGENTOS_EXIT=%ERRORLEVEL%"
pause
exit /b %AGENTOS_EXIT%
