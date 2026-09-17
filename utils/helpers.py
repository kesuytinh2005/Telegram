import os
import re

def safe_filename(name: str, default="file"):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name or "")
    name = name.strip().strip(".")
    return name or default

def ensure_dir(path):
    os.makedirs(path, exist_ok=True)
    return path
