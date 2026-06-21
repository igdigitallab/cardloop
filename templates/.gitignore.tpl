# Secrets and local config
.env
.env.*
!.env.example
.claude-ops/secrets/

# Python
__pycache__/
*.py[cod]
*.pyo
*.pyd
.Python
venv/
env/
.venv/
.pytest_cache/
.mypy_cache/
*.egg-info/
.eggs/
dist/
build/
*.so

# Node
node_modules/
dist/
.next/
.nuxt/
out/
.cache/

# Logs and data
*.log
data/

# OS
.DS_Store
Thumbs.db
*.swp
*~

# Editor
.idea/
.vscode/
*.sublime-project
*.sublime-workspace

# Claude-Ops: project memory (.claude-ops/memory/) IS committed — do NOT ignore.
# Private data (keys, secrets) → .claude-ops/secrets/ only (gitignored above).
