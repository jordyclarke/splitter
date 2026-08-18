@echo off
title POD Splitter
cd /d "%~dp0"

echo ============================================================
echo  POD Batch Splitter
echo ============================================================
echo  App folder : %~dp0
echo  Scan into  : %~dp0POD_System\1_Input
echo  Output from: %~dp0POD_System\2_Output
echo.
echo  Keep this window open while scanning.
echo  Press Ctrl+C to stop.
echo ============================================================
echo.

POD_Splitter.exe

echo.
echo Splitter stopped.
pause
