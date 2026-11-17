---
name: pref-pip-windows
description: Prefer python -m pip inside .venv on Windows when uv is unavailable
metadata:
  node_type: memory
  type: user
---

Always use `.venv\Scripts\python.exe -m pip` instead of `uv` on this Windows machine.
