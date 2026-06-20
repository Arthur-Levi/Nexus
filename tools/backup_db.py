"""
Backup manual e imediato do nexus_world.db — roda a MESMA lógica usada pela
task periódica em main.py (nunca duplica essa lógica em dois lugares).

Uso: python3 tools/backup_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import backup_db_once  # noqa: E402

if __name__ == "__main__":
    path = backup_db_once()
    if path:
        print(f"Backup criado em: {path}")
    else:
        print("Nada para fazer backup — nexus_world.db ainda não existe.")
