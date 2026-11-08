# 冒烟校验脚本（smoke）

对**已运行**的本地开发服务器做端到端人工校验，不参与 pytest 套件。

| 脚本 | 作用 | 前置条件 |
|---|---|---|
| `check_pages.py <activity_id>` | 登录学生账号，校验工作台卡片、状态徽章、强制改密横幅、申报向导分步页面 | 服务运行在 `127.0.0.1:8010`，且已有指定活动 |
| `check_export.py` | 以学生身份上传材料并触发导出，校验 zip 结构与去重逻辑 | 服务运行在 `127.0.0.1:8010`，依赖 Django ORM（需在 mainbody 的 .venv 下执行） |

运行方式（在 `mainbody/` 目录下）：

```powershell
.\.venv\Scripts\python.exe scripts\smoke\check_pages.py 12
```

脚本内的测试账号（`2025080901` / `2025080902`）来自 `scripts/seed_demo_data.py` 灌入的演示数据，仅限本地开发库使用。这两个脚本**不进 pytest**（依赖真实 HTTP 服务、有副作用），正式自动化覆盖见 `tests/test_step4_frontend_and_e2e.py`。
