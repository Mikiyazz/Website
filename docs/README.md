# 学院社会实践材料平台

面向学院约 600 名学生，收集社会实践申报表、实践报告、现场照片与宣讲视频，按班级完成初审/终审并导出汇总。**阶段 0~4 已收口**：注册登录、四步申报、直传与分片上传、两级审核、补交白名单、Excel/ZIP 导出与异步导出任务均可跑通；测试与压测脚本齐全。

单文件硬上限 `10,000,000,000` 字节（`MAX_FILE_BYTES`，模型约束里同样写死，只改 `.env` 突破不了）。每名学生每个活动的默认额度 **12 GiB = 12,884,901,888 字节**：5 个 2000MB 打包件 + 约 2.4GB 零散材料。该默认值只对新活动生效。

## 文档导航

| 文档 | 用途 |
|---|---|
| [项目架构与模块说明](项目架构与模块说明.md) | 总体架构、模块职责、分片接口、状态机、导出链路与修改入口 |
| [文件清单](文件清单.md) | 每个交付文件的具体用途；不含虚拟环境、密钥和缓存 |
| [开发与扩展指南](开发与扩展指南.md) | 新增字段、页面、业务规则、队列任务和外部适配器 |
| [接口与状态约定](接口与状态约定.md) | 接口、错误格式、权限、草稿版本号与状态流转 |
| [运行与可靠性说明](运行与可靠性说明.md) | 启动、并发保护、资源边界、故障恢复与部署限制 |
| [架构完整性与验证记录](架构完整性与验证记录.md) | 实际检查结果与尚未验证的内容 |
| [压测报告](压测报告.md) | 50 并发登录/直传与 300MB 分片视频的实测数据、局限与优化方向 |

## 技术组成

Python 3.12、Django 5.2 LTS、PostgreSQL、Redis、Celery、Gunicorn、Nginx；页面用 Django 模板，FilePond 与 Tabler 本地托管（不走 CDN）。`requirements/*.in` 是版本范围，`requirements/*.txt` 是精确快照。

## 从零启动（Windows 本机，SQLite 快速体验）

```powershell
Set-Location E:\Website\mainbody

# 1) 虚拟环境与依赖（已存在可跳过）
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements\dev.txt

# 2) 生成 .env（随机密钥，不覆盖已有文件）
.\.venv\Scripts\python.exe scripts\init_local_env.py

# 3) 没有 PostgreSQL/Redis 时用 SQLite 开发模式；CELERY 在开发环境是 eager
$env:DEV_SQLITE = '1'

# 4) 建表、造演示数据、起服务
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe scripts\seed_demo_data.py
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8080
```

打开 [登录页](http://127.0.0.1:8080/accounts/login/)、[管理后台](http://127.0.0.1:8080/admin/) 或 [模块总览](http://127.0.0.1:8080/dev/architecture/)。演示账号由 `seed_demo_data.py` 打印；学生初始密码规则是**学号后 6 位**，首次登录会看到强制改密横幅。

`development.py` 的回退逻辑：`DEV_SQLITE=1` 或本机连不上 PostgreSQL 时自动用 SQLite；可用 `DEV_SQLITE_PATH` 指定数据库副本（压测用），并已开启 `transaction_mode=IMMEDIATE` + 20 秒忙等待，避免并发写瞬间报 `database is locked`。

## 启动完整容器环境（PostgreSQL + Redis + Celery）

```powershell
Set-Location E:\Website\mainbody
docker compose up -d db redis
docker compose build web
docker compose --profile setup run --rm migrate
docker compose run --rm web python manage.py createsuperuser
docker compose up -d web dispatcher worker-validation worker-exports nginx
```

入口是 [http://localhost:8080](http://localhost:8080/)。`.env` 存随机密钥且被 `.gitignore` 排除，不要提交，也不要提交真实学生材料。

## 检查与测试

```powershell
Set-Location E:\Website\mainbody

# 结构与文档链接检查（新增文件必须补 PURPOSES，否则报 Missing purpose description）
.\.venv\Scripts\python.exe scripts\check_structure.py

# 配置检查 + 全量测试（SQLite 轻量模式）
$env:TEST_SQLITE = '1'
.\.venv\Scripts\python.exe manage.py check --settings=config.settings.test
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:TEST_SQLITE
```

SQLite 模式会跳过标了 `postgresql` 的用例（`select_for_update` 行锁、真实并发提交）。这些必须在 docker compose 起的 PostgreSQL 上复跑：

```powershell
# 先 docker compose up -d db，并让 settings 连到该库
Remove-Item Env:TEST_SQLITE
.\.venv\Scripts\python.exe -m pytest -q -m postgresql
```

## 压测（50 并发 + 300MB 分片视频）

压测要用**独立数据库副本**和**独立端口**，避免和日常开发服务器抢文件：

```powershell
Set-Location E:\Website\mainbody
$env:DEV_SQLITE = '1'
$env:DEV_SQLITE_PATH = 'db_stress.sqlite3'
$env:UPLOAD_FFPROBE_BIN = '__disabled__'   # 随机字节不是真实视频，让 ffprobe 降级
.\.venv\Scripts\python.exe manage.py migrate --settings=config.settings.development
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8010 --noreload

# 另开一个终端
.\.venv\Scripts\python.exe scripts\stress_test.py --base-url http://127.0.0.1:8010
```

脚本自动创建 50 个压测账号（99000001 起）与一个开放活动，依次跑：并发登录/建草稿/直传 -> 串行基准 -> 300MB（60×5MB 随机字节）分片上传。结果打印 P50/P95 并写入 [压测报告](压测报告.md)。`--skip-video` 跳过 300MB 阶段，`--keep-video` 保留产物。

验收线：50 并发小附件直传 **P95 < 2000ms**。SQLite 副本只能验证链路，600 人真实规模请在 PostgreSQL 下用同一脚本复跑（改 `--settings` 并去掉 `DEV_SQLITE`）。
