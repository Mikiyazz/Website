# 学院社会实践材料平台

高校学院社会实践（暑期"三下乡"类）材料申报与评审系统。Django + Celery 单体应用，学生端提交材料、教师端评审打分与导出。

## 目录速览

```
Website/
├── mainbody/              # Django 项目根（含 .venv、manage.py）
│   ├── apps/              # 业务域，一个域一个 app（accounts/submissions/uploads/reviews/exports/jobs/audit/...）
│   ├── config/            # 项目配置（settings/ 分 base/development/test/production）
│   ├── integrations/      # 外部系统适配（identity 统一认证、storage 对象存储）
│   ├── deploy/            # gunicorn 与 nginx 配置
│   ├── requirements/      # pip-tools：*.in 手写，*.txt 编译产物
│   ├── scripts/           # 运维与工具脚本（smoke/ 下为一次性冒烟校验）
│   ├── static/            # 源静态资源（vendor/ 为第三方库）
│   ├── staticfiles/       # collectstatic 产物，不入库
│   ├── media/             # 用户上传目录，不入库
│   ├── templates/         # 全局模板（app 内模板就近放置）
│   └── tests/             # pytest 用例与 fixtures
├── docs/                  # 全部文档与示意图（images/ 放截图）
├── run_dev.ps1 / .bat     # 一键起本地开发服务器（127.0.0.1:8080）
└── seed_data.bat          # 重置/填充演示账号与模拟数据
```

## 快速开始

```powershell
cd mainbody
python -m venv .venv
.\.venv\Scripts\pip install -r requirements\dev.txt
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py createsuperuser

cd ..
.\run_dev.ps1          # 或双击 run_dev.bat
```

访问 http://127.0.0.1:8080/ 。容器化启动见 `mainbody/compose.yaml`（`docker compose up -d`）。

## 测试

```powershell
cd mainbody
.\.venv\Scripts\python -m pytest
```

## 文档

| 文档 | 内容 |
|---|---|
| [docs/README.md](docs/README.md) | 文档索引 |
| [项目架构与模块说明](docs/项目架构与模块说明.md) | 分层、域边界、依赖方向 |
| [接口与状态约定](docs/接口与状态约定.md) | URL 契约、状态机、错误码 |
| [开发与扩展指南](docs/开发与扩展指南.md) | 新增 app / 字段 / 页面的步骤 |
| [运行与可靠性说明](docs/运行与可靠性说明.md) | 环境、任务队列、失败恢复 |
| [架构完整性与验证记录](docs/架构完整性与验证记录.md) | 结构检查项与结论 |
| [压测报告](docs/压测报告.md) | 并发与容量实测 |
| [文件清单](docs/文件清单.md) | 逐文件职责表 |
| [需求问答记录](docs/需求问答记录.txt) | 业务规则的原始确认记录 |
