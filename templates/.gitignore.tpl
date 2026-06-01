# Python
venv/
.venv/
__pycache__/
*.pyc
*.pyo
.pytest_cache/
.mypy_cache/

# Node
node_modules/
dist/
*.log

# Secrets
.env
.env.*
!.env.example

# Editor
.vscode/
.idea/

# OS
.DS_Store
Thumbs.db

# Claude-Ops: память проекта (.claude-ops/memory/) коммитится — НЕ игнорировать.
# Приватное (ключи, секреты) — только в .claude-ops/secrets/.
.claude-ops/secrets/
