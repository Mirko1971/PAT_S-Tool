@echo off
echo.
echo  ============================================
echo   PAT_S - Physio-Akademie Tool Schulter
echo  ============================================
echo.

cd /d "%~dp0"

:: Alte Server-Prozesse auf Port 5000 beenden
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000 "') do (
    taskkill /PID %%a /F >nul 2>&1
)

echo  Server startet ... Browser oeffnet sich.
echo  Zum Beenden: Strg+C druecken
echo.

timeout /t 1 /nobreak >nul
start "" http://localhost:5000
py server.py
pause
