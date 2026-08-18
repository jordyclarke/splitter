@echo off
title POD Splitter
cd /d "%~dp0"

echo ============================================================
echo  POD Batch Splitter
echo ============================================================
echo  App folder : %~dp0
echo  Output from: %~dp0POD_System\2_Output
echo.
if exist "%~dp0settings.ini" (
  echo  Using watch folder from settings.ini
) else (
  echo  No settings.ini — copy settings.ini.example and edit Kodak path
  echo  Or scan into: %~dp0POD_System\1_Input
)
echo.
echo  Keep this window open while scanning.
echo  Press Ctrl+C to stop.
echo ============================================================
echo.

POD_Splitter.exe

echo.
echo Splitter stopped.
pause
