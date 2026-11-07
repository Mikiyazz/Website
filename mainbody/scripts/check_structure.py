"""Verify the scaffold contract and optionally refresh the Chinese per-file index.

Uses only the standard library; never imports app settings or reads local secrets.
"""
import argparse
import ast
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
INDEX = "docs/文件清单.md"
EXCLUDED_DIRS = {".venv", ".git", "__pycache__", ".pytest_cache", "staticfiles", ".runtime", "media", "backups"}
MODULES = {
    "common": ("通用基础", "models.py admin.py api.py errors.py health.py logging.py middleware.py"),
    "accounts": ("账号与名单", "models.py admin.py permissions.py services.py views.py urls.py"),
    "activities": ("活动与材料规则", "models.py admin.py selectors.py views.py urls.py"),
    "submissions": ("草稿与提交版本", "models.py admin.py services.py views.py urls.py"),
    "uploads": ("上传与额度", "models.py admin.py policies.py services.py views.py urls.py"),
    "reviews": ("教师审核", "models.py admin.py selectors.py services.py views.py urls.py"),
    "validation": ("文件检查", "models.py admin.py rules.py"),
    "news": ("可选新闻稿", "models.py admin.py"),
    "exports": ("后台导出", "models.py admin.py services.py views.py urls.py"),
    "audit": ("业务审计", "models.py admin.py services.py"),
    "jobs": ("持久任务", "models.py admin.py registry.py services.py dispatcher.py tasks.py"),
}
PURPOSES = {
    ".gitignore": "排除密钥、依赖目录、缓存、学生文件和备份，避免进入 Git。",
    ".dockerignore": "排除本机密钥、依赖和运行数据，避免复制进镜像。",
    ".env.example": "可复制的环境配置模板，不包含真实凭证。",
    "README.md": "项目入口说明、文档导航和快速启动命令。",
    "项目架构与模块说明.md": "架构图、模块职责、数据关系和后续修改入口。",
    "manage.py": "Django 管理命令入口，执行迁移、建管理员、运行本地服务。",
    "pyproject.toml": "Python 版本要求、项目元信息和 pytest 配置。",
    "Dockerfile": "构建应用镜像、安装依赖、收集静态文件，以非 root 用户运行。",
    "compose.yaml": "组合数据库、Redis、Web、调度器、Worker、迁移和 Nginx 服务。",
    "compose.dev.yaml": "本机 Python 开发用覆盖配置，仅向回环地址开放数据库端口。",
    "config/__init__.py": "导出 Celery 应用，使 Django 和命令行加载一致。",
    "config/celery.py": "初始化 Celery、加载队列配置并自动发现任务。",
    "config/urls.py": "注册健康、登录、注册、改密、各角色工作台和业务模块 URL。",
    "config/wsgi.py": "Gunicorn 加载 Django 的 WSGI 入口。",
    "config/settings/base.py": "共享设置：数据库、缓存、认证、中间件、日志、额度和队列限制。",
    "config/settings/development.py": "开发环境配置、调试和静态文件设置。",
    "config/settings/production.py": "生产密钥/域名校验、HTTPS、防护 Cookie 和代理信任配置。",
    "config/settings/test.py": "隔离测试配置；显式选择 SQLite 轻量模式或 PostgreSQL。",
    "deploy/gunicorn.conf.py": "Web 进程/线程数量、超时、回收和日志配置。",
    "deploy/nginx.conf": "本地 HTTP 反向代理、350MB 请求体上限、超时和限速配置。",
    "integrations/storage/base.py": "定义分片存储协议及分页结果、对象信息等数据类型。",
    "integrations/storage/__init__.py": "选择存储适配器；尚未接入时明确拒绝上传。",
    "integrations/identity/__init__.py": "学校认证、学生激活外部接口的扩展位置。",
    "templates/base.html": "公共页面结构、首次改密顶部提醒横幅、头部和退出表单。",
    "templates/registration/login.html": "Django 账号登录页面、初始密码说明与自主注册入口。",
    "templates/registration/register.html": "学生自主注册页面（学号、姓名、年级、专业、班级、手机号与密码）。",
    "templates/registration/change_password.html": "用户修改密码与解除首次改密提醒页面。",
    "templates/dashboard.html": "全角色前端工作台（学生申报与拖拽上传进度条、初审/终审与管理员面板）。",
    "templates/review_detail.html": "初审与终审独立详情页（项目信息、团队成员、附件预览/下载与审核操作）。",
    "templates/architecture.html": "仅开发环境开放的模块总览。",
    "static/css/app.css": "公共颜色、排版、卡片、拖拽上传进度条、状态徽章和响应式样式。",
    "requirements/base.in": "应用运行依赖的版本范围。",
    "requirements/dev.in": "运行依赖和开发检查依赖的版本范围。",
    "requirements/base.txt": "运行依赖的精确版本快照，供 Docker 和部署安装。",
    "requirements/dev.txt": "包含测试依赖的精确版本快照。",
    "scripts/init_local_env.py": "生成随机本地密钥与 .env，拒绝覆盖已有配置。",
    "scripts/lock_dependencies.py": "从已安装依赖树生成 Windows/Linux 版本快照。",
    "scripts/check_health.py": "容器健康探测，使用配置中允许的 Host 访问 live。",
    "scripts/check_structure.py": "核对必要文件、语法、模块注册、迁移和文档链接；生成本清单。",
    "scripts/seed_demo_data.py": "快速填充初始测试账号、管理员、实践活动与模拟申报数据。",
    "docs/开发与扩展指南.md": "新增模型、接口、存储适配器、检查规则和队列任务的开发约定。",
    "docs/接口与状态约定.md": "当前路由、请求响应、权限、错误和状态机说明。",
    "docs/运行与可靠性说明.md": "启动方式、资源限制、故障处理、生产边界与检查命令。",
    "docs/架构完整性与验证记录.md": "实际执行的检查结果和未验证内容。",
    INDEX: "所有交付源文件的用途索引，由结构检查脚本生成。",
    "tests/conftest.py": "创建虚构用户、活动和材料规则的测试夹具。",
    "tests/test_drafts_and_access.py": "验证草稿冲突、权限隔离、CSRF、未实现接口和页面响应。",
    "tests/test_upload_quota.py": "验证大小、累计额度、幂等请求、取消和同时任务数量限制。",
    "tests/test_outbox.py": "验证事务回滚、重复消费、队列故障和有限重试。",
    "tests/test_postgresql_concurrency.py": "真实 PostgreSQL 并发与 JSON 查询检查，SQLite 模式明确跳过。",
    "tests/test_phase1_business_core.py": "验证第一阶段账号注册、原草稿覆盖与两级审核状态机。",
    "tests/test_step1_security_and_rules.py": "验证账号扩充字段、默认后6位密码、改密闭环、补交白名单与跨项目防重。",
    "tests/test_step2_streaming_uploads.py": "验证流式上传、文件名与32字节魔数双重硬校验、物理覆盖删除与在线预览。",
    "tests/test_step3_exports_and_zip.py": "验证全院6列Excel汇总表导出、非法文件名清洗与按班级流式打包ZIP。",
    "tests/test_step4_frontend_and_e2e.py": "验证全角色前端界面、拖拽上传进度条、打回提示、审核预览与部署配置。",
    "apps/jobs/management/commands/dispatch_outbox.py": "持续发布数据库中的到期任务，支持单次执行检查。",
    "apps/jobs/management/commands/enqueue_probe.py": "登记一个无学生数据的任务，用于检查队列链路。",
    "apps/jobs/management/commands/retry_job.py": "修复故障后，将一条失败任务恢复到待投递状态。",
    "apps/common/models.py": "UUID 和时间戳抽象父类，本身不创建数据库表。",
    "apps/common/admin.py": "业务记录的只读管理后台基类。",
    "apps/common/api.py": "会话认证、JSON 解析、统一业务错误和繁忙响应。",
    "apps/common/errors.py": "业务异常及相应 HTTP 错误码。",
    "apps/common/health.py": "进程存活及数据库/Redis 就绪接口。",
    "apps/common/logging.py": "JSON 日志格式，不输出异常详情中的潜在凭证。",
    "apps/common/middleware.py": "生成请求编号，记录路由、状态和耗时。",
    "apps/accounts/permissions.py": "有效学生、学院管理员等身份判断。",
    "apps/uploads/policies.py": "上传文件元数据校验与分片数量计算。",
    "apps/validation/rules.py": "规则检查协议、结果类型与规则注册位置。",
    "apps/jobs/registry.py": "明确允许的任务类型、目标队列及处理器边界。",
    "apps/jobs/dispatcher.py": "领取任务、短租约、事务外发布、退避和重投。",
    "apps/jobs/tasks.py": "幂等消费持久任务并提交完成或失败状态。",
}
FILE_ROLES = {
    "models.py": "定义该模块的数据库模型、关系、索引和约束。",
    "admin.py": "注册管理后台；业务状态记录按只读方式查看。",
    "selectors.py": "按业务权限查询数据，封装读取范围。",
    "services.py": "业务写入与状态变化入口，集中校验和事务边界。",
    "views.py": "接收 HTTP 请求，调用服务并返回响应或 JSON。",
    "urls.py": "分配模块内 HTTP 地址。",
}


def source_files():
    files = []
    for folder, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        for name in names:
            if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
                continue
            if name.endswith((".pyc", ".log", ".sqlite3")):
                continue
            files.append((Path(folder) / name).relative_to(ROOT).as_posix())
    return sorted(files)


def purpose(path):
    if path in PURPOSES:
        return PURPOSES[path]
    parts = path.split("/")
    if path.endswith("/__init__.py"):
        if len(parts) >= 3 and parts[0] == "apps" and parts[1] in MODULES:
            return f"声明 {MODULES[parts[1]][0]} 的 Python 包或迁移/命令子包。"
        return "声明 Python 包，供模块导入和框架发现。"
    if len(parts) >= 3 and parts[0] == "apps" and parts[1] in MODULES:
        label = MODULES[parts[1]][0]
        if "migrations" in parts and parts[-1].endswith(".py"):
            return f"{label}的数据库迁移：创建或变更表、关系、索引和约束。"
        if parts[-1] in FILE_ROLES:
            return f"{label}：{FILE_ROLES[parts[-1]]}"
    raise ValueError(f"Missing purpose description: {path}")


def _doc_exists_in_workspace(rel_path: str) -> bool:
    if (ROOT / rel_path).exists():
        return True
    parent_docs = ROOT.parent / "docs"
    if (ROOT.parent / rel_path).exists():
        return True
    if (parent_docs / Path(rel_path).name).exists():
        return True
    return False


def check(files):
    errors = []
    required = set(PURPOSES) | {"apps/__init__.py", "integrations/__init__.py", "tests/__init__.py", "config/settings/__init__.py"}
    for module, (_, names) in MODULES.items():
        required |= {f"apps/{module}/{name}" for name in ("__init__.py " + names).split()}
        if module != "common":
            required |= {f"apps/{module}/migrations/__init__.py", f"apps/{module}/migrations/0001_initial.py"}
    for path in sorted(required - set(files)):
        if path.endswith(".md") and _doc_exists_in_workspace(path):
            continue
        errors.append(f"Missing required file: {path}")
    for path in files:
        try:
            purpose(path)
        except ValueError as exc:
            errors.append(str(exc))
        if path.endswith(".py"):
            try:
                ast.parse((ROOT / path).read_text(encoding="utf-8-sig"), filename=path)
            except SyntaxError as exc:
                errors.append(f"Python syntax: {path}:{exc.lineno}: {exc.msg}")
        if path.endswith(".md"):
            text = (ROOT / path).read_text(encoding="utf-8")
            for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                if link.startswith(("http:", "https:", "#", "mailto:", "/")):
                    continue
                local = unquote(link.split("#", 1)[0]).strip("<>")
                if local and not ((ROOT / path).parent / local).exists():
                    errors.append(f"Broken documentation link: {path} -> {link}")
    tree = ast.parse((ROOT / "config/settings/base.py").read_text(encoding="utf-8"))
    installed = next(ast.literal_eval(node.value) for node in tree.body
                     if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "INSTALLED_APPS" for t in node.targets))
    for module in MODULES:
        if f"apps.{module}" not in installed:
            errors.append(f"Django app not registered: {module}")
    return errors


def write_index(files):
    lines = ["# 项目文件清单", "", "由 `scripts/check_structure.py --write-index` 根据实际源文件生成。", "",
             "不包含 `.env`、`.venv`、缓存、收集后的静态资源或学生材料。每个链接对应实际文件。", "",
             f"本清单覆盖 **{len(files)} 个文件**。", "", "| 文件 | 用途 |", "|---|---|"]
    lines += [f"| [{path}](../{path}) | {purpose(path)} |" for path in files]
    target_path = ROOT / INDEX if (ROOT / "docs").exists() else (ROOT.parent / INDEX)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-index", action="store_true")
    args = parser.parse_args()
    files = source_files()
    if args.write_index:
        write_index(files)
    errors = check(source_files())
    if errors:
        print("\n".join(errors))
        sys.exit(1)
    print(f"PASS: {len(source_files())} source/documentation files; {len(MODULES)} registered modules; Python syntax and local documentation links valid.")