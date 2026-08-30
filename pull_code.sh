#!/bin/bash

# =================================================================
# Git 自动拉取与部署脚本
# =================================================================

# 设置仓库目录（默认为脚本所在目录）
REPO_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$REPO_DIR"

# 检查是否是 git 仓库
if [ ! -d ".git" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 错误: 当前目录不是一个 Git 仓库。"
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始检查更新..."

# 获取当前分支名称
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)

# 获取当前本地提交哈希
LOCAL_HASH=$(git rev-parse HEAD)

# 尝试抓取远程更新
echo "正在抓取远程仓库..."
git fetch --all --quiet

# 确定远程分支哈希
REMOTE_HASH=""
CANDIDATES=("origin/$CURRENT_BRANCH" "origin/main" "origin/master")

for REF in "${CANDIDATES[@]}"; do
    HASH=$(git rev-parse "$REF" 2>/dev/null)
    if [ -n "$HASH" ]; then
        REMOTE_HASH="$HASH"
        echo "使用远程引用 $REF (哈希: ${REMOTE_HASH:0:7})"
        break
    fi
done

if [ -z "$REMOTE_HASH" ]; then
    echo "错误: 无法获取远程哈希，跳过自动更新。"
    exit 1
fi

# 检查是否有更新
if [ "$LOCAL_HASH" != "$REMOTE_HASH" ]; then
    echo "发现更新: 本地(${LOCAL_HASH:0:7}) -> 远程(${REMOTE_HASH:0:7})"
    
    # 清理 git 锁文件
    if [ -f ".git/index.lock" ]; then
        echo "清理 git 锁文件..."
        rm -f .git/index.lock
    fi
    
    # 强制更新本地代码
    echo "正在更新本地代码..."
    git reset --hard "$REMOTE_HASH"
    
    # 安装依赖
    if [ -f "requirements.txt" ]; then
        echo "正在更新 Python 依赖..."
        # 尝试使用国内源
        pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || \
        pip install -r requirements.txt
    fi
    
    echo "更新完成。"
    
    # 重启服务逻辑 (根据实际部署环境修改)
    # 如果是在后台运行 app.py，可以尝试重启它
    echo "提示: 请手动或通过进程管理器(如 pm2/systemd) 重启 app.py 以应用更改。"
    # pkill -f "python app.py" && nohup python app.py > app.log 2>&1 &
else
    echo "代码已是最新，无须更新。"
    
    # 检查 requirements.txt 是否被修改过 (可选)
    # if [ requirements.txt -nt .last_req_check ]; then
    #     pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    #     touch .last_req_check
    # fi
fi
