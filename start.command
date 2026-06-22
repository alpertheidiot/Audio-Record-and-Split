#!/bin/bash

# Navigate to the directory containing this script
cd "$(dirname "$0")"

echo "========================================="
echo "   Starting SilenceCut Audio Capture Hub  "
echo "========================================="

# Check if the virtual environment exists, create if missing
if [ ! -d ".venv" ]; then
    echo "Virtual environment (.venv) not found. Initializing..."
    python3 -m venv .venv
    source .venv/bin/activate
    echo "Installing required Python dependencies..."
    pip install --upgrade pip
    pip install -r requirements.txt
else
    # Activate virtual environment
    source .venv/bin/activate
fi

# Start browser opening in background after 1.5s delay
(sleep 1.5 && open http://localhost:8000) &

# Launch FastAPI / Uvicorn server
python3 -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
