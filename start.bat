@echo off
echo.
echo  S E N T R I X  ^|  Starting all services...
echo  -----------------------------------------------
echo.

echo [1/3] Starting FastAPI backend on http://localhost:8000
start "SENTRIX API" cmd /k "cd /d %~dp0 && venv\Scripts\uvicorn api.main:app --reload --port 8000"

timeout /t 2 /nobreak >nul

echo [2/3] Starting agent loop (Perceive-Reason-Act)
start "SENTRIX Agent" cmd /k "cd /d %~dp0 && venv\Scripts\python -m agent.main"

timeout /t 2 /nobreak >nul

echo [3/3] Starting React dashboard on http://localhost:3000
start "SENTRIX Dashboard" cmd /k "cd /d %~dp0\dashboard && npm start"

echo.
echo  All services launching in separate windows.
echo  Dashboard: http://localhost:3000
echo  API docs:  http://localhost:8000/docs
echo  AI Engine: http://localhost:8000/api/ml-status
echo.
pause
