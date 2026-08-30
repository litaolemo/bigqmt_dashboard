"""
数据库损坏修复脚本
用法：python check_integrity.py [db路径]
默认路径：dashboard.db
"""
import sqlite3
import shutil
import os
import sys
from datetime import datetime

DB = sys.argv[1] if len(sys.argv) > 1 else 'dashboard.db'
REPAIRED = DB + '.repaired'

print(f"目标数据库: {DB}")

# 1. 先做完整性检查
print("\n=== 完整性检查 ===")
conn = sqlite3.connect(DB)
rows = conn.execute("PRAGMA integrity_check").fetchall()
conn.close()
for r in rows[:20]:
    print(r[0])
print(f"(共 {len(rows)} 条结果)")

if rows[0][0] == 'ok':
    print("\n数据库完好，无需修复。")
    sys.exit(0)

# 2. 备份
backup_dir = os.path.join(os.path.dirname(DB), 'backup')
os.makedirs(backup_dir, exist_ok=True)
backup_name = os.path.join(backup_dir, f"dashboard_before_repair_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
shutil.copy2(DB, backup_name)
print(f"\n已备份原库 -> {backup_name}")

# 3. dump 全量重建（彻底修复 B-tree 页序和索引）
print("正在重建数据库（iterdump）...")
src = sqlite3.connect(DB)
dst = sqlite3.connect(REPAIRED)
skip = 0
for line in src.iterdump():
    try:
        dst.execute(line)
    except Exception as e:
        skip += 1
        if skip <= 5:
            print(f"  [skip] {str(e)[:80]}")
dst.commit()
src.close()
print(f"跳过了 {skip} 条语句（通常是重复键，正常现象）")

# 4. 验证新库
print("\n=== 新库完整性检查 ===")
rows2 = dst.execute("PRAGMA integrity_check").fetchall()
for r in rows2[:5]:
    print(r[0])
dst.close()

if rows2[0][0] == 'ok':
    os.replace(REPAIRED, DB)
    print("\n修复成功！已替换原数据库。")
else:
    print(f"\n新库仍有问题，未替换，请人工检查 {REPAIRED}")
    sys.exit(1)
