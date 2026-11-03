"""测试前确保 MES 演示数据库存在（数据库由脚本生成，不进仓库）。"""

import subprocess
import sys
from pathlib import Path

MES_SERVER_DIR = Path(__file__).parent / "domains" / "mes" / "mcp_server"

if not (MES_SERVER_DIR / "data" / "mes_demo.db").exists():
    subprocess.run([sys.executable, str(MES_SERVER_DIR / "seed_demo_db.py")], check=True)
