#!/usr/bin/env bash
# 矿大服务器每日定时备份脚本（建议通过 cron 每日凌晨 03:00 执行）：
#   0 3 * * * /opt/practice-platform/mainbody/deploy/backup_daily.sh >> /var/log/practice-backup.log 2>&1
#
# 保护目标：
# 1. PostgreSQL 热备（pg_dump -Fc）+ pg_restore --list 完整性校验，滚动保留 14 天；
# 2. 学生材料目录（media_data）增量硬链接快照（rsync --link-dest），排除临时分片与可再生的导出包；
# 3. 备份前核查磁盘剩余空间（低于 10GB 立即告警退出）。

set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DATA_ROOT="${DATA_ROOT:-/data/practice-platform}"
MEDIA_DIR="${MEDIA_DIR:-${DATA_ROOT}/media}"
BACKUP_ROOT="${BACKUP_ROOT:-${DATA_ROOT}/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
MIN_FREE_KB="${MIN_FREE_KB:-10485760}"  # 10 GiB

DATE_TAG="$(date +%Y%m%d_%H%M%S)"
DB_BACKUP_DIR="${BACKUP_ROOT}/postgres"
MEDIA_BACKUP_DIR="${BACKUP_ROOT}/media"

mkdir -p "${DB_BACKUP_DIR}" "${MEDIA_BACKUP_DIR}"

# 1. 磁盘空间核查
FREE_KB="$(df -Pk "${BACKUP_ROOT}" | awk 'NR==2 {print $4}')"
if [[ "${FREE_KB}" -lt "${MIN_FREE_KB}" ]]; then
    echo "[ERROR] $(date -Iseconds) 备份目录 ${BACKUP_ROOT} 剩余空间不足（${FREE_KB} KB < ${MIN_FREE_KB} KB），中止本次备份。" >&2
    exit 1
fi

# 2. 加载环境变量（获取 POSTGRES_DB / POSTGRES_USER）
if [[ -f "${APP_DIR}/.env" ]]; then
    # shellcheck disable=SC1091
    set -a
    source "${APP_DIR}/.env"
    set +a
fi
PG_DB="${POSTGRES_DB:-practice}"
PG_USER="${POSTGRES_USER:-practice}"

# 3. PostgreSQL 自定义压缩格式热备 + 归档清单自检
DB_DUMP_FILE="${DB_BACKUP_DIR}/${PG_DB}_${DATE_TAG}.dump"
echo "[INFO] $(date -Iseconds) 开始备份 PostgreSQL 数据库 ${PG_DB} -> ${DB_DUMP_FILE}"
docker compose -f "${APP_DIR}/compose.yaml" -f "${APP_DIR}/compose.prod.yaml" \
    exec -T db pg_dump -U "${PG_USER}" -d "${PG_DB}" -Fc --no-owner > "${DB_DUMP_FILE}.tmp"

docker compose -f "${APP_DIR}/compose.yaml" -f "${APP_DIR}/compose.prod.yaml" \
    exec -T db pg_restore --list < "${DB_DUMP_FILE}.tmp" >/dev/null

mv "${DB_DUMP_FILE}.tmp" "${DB_DUMP_FILE}"
echo "[INFO] $(date -Iseconds) PostgreSQL 备份校验通过：$(du -h "${DB_DUMP_FILE}" | awk '{print $1}')"

# 4. 学生材料目录增量硬链接快照（跳过 tmp/ 未完成分片与 exports/ 可再生归档包）
if [[ -d "${MEDIA_DIR}" ]]; then
    TARGET_SNAP="${MEDIA_BACKUP_DIR}/snap_${DATE_TAG}"
    LATEST_LINK="${MEDIA_BACKUP_DIR}/latest"
    RSYNC_ARGS=(-a --delete --exclude='tmp/' --exclude='exports/')
    if [[ -d "${LATEST_LINK}" ]]; then
        RSYNC_ARGS+=(--link-dest="${LATEST_LINK}")
    fi
    echo "[INFO] $(date -Iseconds) 开始增量快照学生材料 ${MEDIA_DIR} -> ${TARGET_SNAP}"
    rsync "${RSYNC_ARGS[@]}" "${MEDIA_DIR}/" "${TARGET_SNAP}/"
    ln -sfn "${TARGET_SNAP}" "${LATEST_LINK}"
    echo "[INFO] $(date -Iseconds) 学生材料快照完成。"
else
    echo "[WARN] $(date -Iseconds) 未找到宿主机媒体目录 ${MEDIA_DIR}，跳过文件快照。"
fi

# 5. 滚动清理超过 KEEP_DAYS 天的历史备份
find "${DB_BACKUP_DIR}" -type f -name "*.dump" -mtime "+${KEEP_DAYS}" -delete
find "${MEDIA_BACKUP_DIR}" -maxdepth 1 -type d -name "snap_*" -mtime "+${KEEP_DAYS}" -exec rm -rf {} +

echo "[INFO] $(date -Iseconds) 每日备份全部完成（保留最近 ${KEEP_DAYS} 天）。"
