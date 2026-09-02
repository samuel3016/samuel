"""Load the CPS async callback handoff when Render starts from repo root."""
import os
import sys

bridge_dir = os.path.join(os.path.dirname(__file__), "cps-claude-bridge")
if bridge_dir not in sys.path:
    sys.path.insert(0, bridge_dir)

# Importing the existing shim installs the HTTPServer patch before app.py starts.
import sitecustomize as _bridge_sitecustomize
