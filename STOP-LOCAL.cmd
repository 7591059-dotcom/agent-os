@echo off
setlocal DisableDelayedExpansion
call "%~dp0scripts\windows-local.cmd" stop
set "AGENTOS_EXIT=%ERRORLEVEL%"
pause
exit /b %AGENTOS_EXIT%
