"""Shared bounds for reading project context."""

IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".idea", ".pytest_cache"}
IGNORED_FILE_NAMES = {".env", ".env.local", ".env.production", "secrets.yaml", "secrets.yml", "credentials.yaml", "credentials.yml"}
READABLE_SUFFIXES = {".md", ".py", ".toml", ".txt", ".yaml", ".yml"}
MAX_FILES = 60
MAX_DIRECTORIES = 2_000
MAX_DEPTH = 12
MAX_FILE_CHARS = 12_000
MAX_TOTAL_CHARS = 90_000

