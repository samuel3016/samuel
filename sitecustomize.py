"""Load the CPS async callback handoff when Render starts from repo root."""
import importlib.util
import os

_here = os.path.dirname(__file__)
_shim_path = os.path.join(_here, "cps-claude-bridge", "sitecustomize.py")
_spec = importlib.util.spec_from_file_location("cps_async_sitecustomize", _shim_path)
if _spec and _spec.loader:
    _shim = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_shim)
