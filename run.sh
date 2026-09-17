#!/data/data/com.termux/files/usr/bin/bash
cd "$(dirname "$0")"
export $(grep -v '^#' .env 2>/dev/null | xargs 2>/dev/null)
python main.py
