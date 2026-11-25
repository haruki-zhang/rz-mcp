"""RZ documentation MCP server: catalog, indexers, query layer, MCP wrapper."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "catalog" / "manifest.toml"
DB_PATH = ROOT / "data" / "rz.db"
