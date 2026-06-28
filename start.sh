#!/bin/bash
# start.sh - PocketFM Bot with Auto Restart on Crash

# ============================================
# CONFIGURATION
# ============================================
LOG_FILE="bot.log"

# ============================================
# FUNCTIONS
# ============================================
log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

# ============================================
# MAIN LOOP
# ============================================
log_message "=========================================="
log_message "BOT STARTED"
log_message "=========================================="

# ============================================
# AUTO-INSTALL DEPENDENCIES
# ============================================
log_message "Checking system dependencies..."

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo &> /dev/null; then
        SUDO="sudo"
    else
        log_message "You may need root privileges to install packages."
    fi
fi

if ! command -v ffmpeg &> /dev/null; then
    log_message "FFmpeg not found! Auto-installing..."
    if [ -x "$(command -v apt-get)" ]; then
        $SUDO apt-get update && $SUDO apt-get install -y ffmpeg
    elif [ -x "$(command -v yum)" ]; then
        $SUDO yum install -y epel-release && $SUDO yum install -y ffmpeg
    fi
fi

if ! [ -d "venv" ]; then
    log_message "Virtual environment not found! Setting up..."
    if ! command -v python3 &> /dev/null; then
        log_message "Python3 is not installed. Auto-installing..."
        if [ -x "$(command -v apt-get)" ]; then
            $SUDO apt-get update && $SUDO apt-get install -y python3 python3-venv python3-pip
        fi
    fi
    python3 -m venv venv
    venv/bin/pip install --upgrade pip
    venv/bin/pip install -r requirements.txt
fi

while true; do
    log_message ""
    log_message "Starting bot session at $(date)"
    
    # Run bot directly (no timeout - bot runs until stopped or crash)
    venv/bin/python3 bot.py >> "$LOG_FILE" 2>&1
    
    EXIT_CODE=$?
    
    if [ $EXIT_CODE -eq 0 ]; then
        log_message "Bot completed normally"
    else
        log_message "Bot crashed with exit code: $EXIT_CODE"
    fi
    
    log_message "Restarting bot in 5 seconds..."
    sleep 5
done
