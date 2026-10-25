# 学院社会实践材料平台 · 项目骨架

为学院约 600 余名学生提交社会实践申请表、实践成果准备的模块化项目。当前交付范围是**可继续开发的架构、模型、基础接口、并发保护示例、部署配置和文档**。正式上传、正式提交、审核、导出尚未完成。

当前将 10GB 按十进制解释为 `10,000,000,000` 字节，作为单文件硬上限。每名学生每个活动的累计额度独立配置，骨架暂设为 10GB；最终额度规则可后续调整。

## 从哪里看起

| 文档 | 用途 |
|---|---|
| [项目架构与模块说明](项目架构与模块说明.md) | 总体架构、模块职责、代码分层、修改入口、当前完成边界 |
| [文件清单](docs/文件清单.md) | 每一个交付文件的具体用途；不含虚拟环境、密钥和缓存 |
| [开发与扩展指南](docs/开发与扩展指南.md) | 如何新增字段、页面、业务规则、队列任务和外部适配器 |
| [接口与状态约定](docs/接口与状态约定.md) | 当前接口、错误格式、权限、草稿版本号和未来状态流转 |
| [运行与可靠性说明](docs/运行与可靠性说明.md) | 本地启动、并发保护、资源边界、故障恢复和部署限制 |
| [架构完整性与验证记录](docs/架构完整性与验证记录.md) | 实际检查结果，以及尚未完成的运行验证 |

## 技术组成

Python 3.12、Django 5.2 LTS、PostgreSQL、Redis、Celery、Gunicorn、Nginx。页面采用 Django 模板，前后端同域；文件存储通过独立接口接入 OSS 等对象存储。

`requirements/*.in` 表示允许的依赖版本范围，`requirements/*.txt` 是本次实际安装后生成的精确版本快照。Docker 使用 Linux，Celery 正式运行也放在 Linux 容器中。

## 在 Windows 上启动本地容器环境

先打开 Docker Desktop，确认 Linux 容器引擎能够运行。本次仅生成了配置，未启动容器，也未拉取镜像。以下命令需要在 `F:\Website\mainbody` 执行。

```powershell
Set-Location F:\Website\mainbody

# 本次已生成 .venv 和 .env；换电脑时才需要创建。
# py -3.12 -m venv .venv
# .\.venv\Scripts\python.exe -m pip install -r requirements/dev.txt
# .\.venv\Scripts\python.exe scripts/init_local_env.py

docker compose up -d db redis
docker compose build web
docker compose --profile setup run --rm migrate
docker compose run --rm web python manage.py createsuperuser
docker compose up -d web dispatcher worker-validation worker-exports nginx
```

打开 [本地登录页](http://localhost:8080/accounts/login/)、[管理后台](http://localhost:8080/admin/) 或开发环境中的 [模块总览](http://localhost:8080/dev/architecture/)。不会自动创建管理员账号，也没有默认密码。

本地 `.env` 已使用随机值生成且被 `.gitignore` 排除；请勿把它或真实学生材料提交到仓库。已有 `.env` 不会被初始化脚本覆盖。

## 检查骨架

```powershell
.\.venv\Scripts\python.exe scripts/check_structure.py
$env:TEST_SQLITE = '1'
.\.venv\Scripts\python.exe manage.py check --settings=config.settings.test
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item Env:TEST_SQLITE
```

SQLite 模式仅用于轻量检查，明确跳过真实 PostgreSQL 行锁并发和 JSON 查询检查。正常开发和部署仍使用 PostgreSQL；真实并发运行方法见 [运行说明](docs/运行与可靠性说明.md)。
