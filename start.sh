#!/usr/bin/env bash
# SENTRIX — one-command launcher (macOS / Linux)
set -e
cd "$(dirname "$0")"
echo ""
echo "  S E N T R I X  |  starting all services..."
echo "  ------------------------------------------------"
PY=${PYTHON:-python3}
[ -d venv ] && source venv/bin/activate 2>/dev/null || true

echo "[1/3] API      -> http://localhost:8000/docs"
uvicorn api.main:app --reload --port 8000 & API_PID=$!
sleep 2
echo "[2/3] Agent    -> Perceive - Reason - Predict - Act"
$PY -m agent.main & AGENT_PID=$!
sleep 1
echo "[3/3] Dashboard-> http://localhost:3000"
( cd dashboard && npm start ) & DASH_PID=$!

trap "kill $API_PID $AGENT_PID $DASH_PID 2>/dev/null" INT TERM
echo ""
echo "  All services launched. Ctrl+C to stop."
wait
