"""批次 2（2000 人并发不崩溃 · 后端核心 3 项）专项验收测试。

覆盖：
- 2.1 (PERF-01): SubmissionTeamMember 索引表、迁移回填、全生命周期同步、索引查询、单 SQL redundant_member_draft_ids、2000 人数据集下首页 < 100ms & SQL <= 10
- 2.2 (PERF-02): ensure_standard_material_requirements 只读优先、学生 GET / 与管理员 GET /admin-panel/ 写 SQL 严格为 0
- 2.3 (PERF-05 + PERF-06 后台): GET /api/v1/reviews/ SQL <= 8、单次审核动作 SQL <= 6（无全表扫描）、Admin 批量操作防超时 / Celery 异步任务 / 命令行脚本
"""
import importlib
import time
import uuid
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.hashers import make_password
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import call_command
from django.db import connection
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext

from apps.accounts.admin import ADMIN_SYNC_PASSWORD_BATCH_LIMIT, PlatformUserAdmin, RosterAdmin
from apps.accounts.models import StudentRoster, User
from apps.accounts.tasks import batch_initialize_student_accounts_task, batch_reset_passwords_task
from apps.activities.models import MaterialRequirement
from apps.reviews.models import ReviewRecord
from apps.reviews.services import has_first_review_permission, review_submission
from apps.submissions.models import Submission, SubmissionRevision, SubmissionTeamMember
from apps.submissions.services import (
    create_draft,
    delete_submission,
    find_team_membership_activity_ids,
    find_team_membership_submissions,
    map_activity_team_memberships_all,
    redundant_member_draft_ids,
    save_draft,
    submit_declaration,
    submit_for_review,
)
from apps.uploads.services import ensure_standard_material_requirements
from tests.conftest import seed_mandatory_materials

_WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _extract_write_sqls(captured_queries) -> list[str]:
    writes = []
    for item in captured_queries:
        sql = (item.get("sql") or "").lstrip()
        upper = sql.upper()
        if any(upper.startswith(prefix) for prefix in _WRITE_PREFIXES):
            writes.append(sql)
    return writes


def _extract_non_savepoint_sqls(captured_queries) -> list[str]:
    result = []
    for item in captured_queries:
        sql = (item.get("sql") or "").lstrip()
        upper = sql.upper()
        if upper.startswith("SAVEPOINT") or upper.startswith("RELEASE SAVEPOINT") or upper.startswith("ROLLBACK TO SAVEPOINT"):
            continue
        result.append(sql)
    return result


def _attach_messages(request):
    request.session = {}
    request._messages = FallbackStorage(request)
    return request


# =====================================================================
# 2.1 SubmissionTeamMember 生命周期同步、迁移回填与单 SQL 冗余草稿过滤
# =====================================================================

@pytest.mark.django_db
def test_team_member_index_lifecycle_and_backfill(activity, student, other_student):
    member_c = User.objects.create_user(
        username="20250903",
        password="password123",
        real_name="陈三",
        class_name="01班",
        grade="2025级",
        major="信息安全",
        must_change_password=False,
    )
    ensure_standard_material_requirements(activity)

    # 1) 创建空白草稿不应产生团队成员索引行
    sub = create_draft(actor=student, activity_id=activity.id)
    assert SubmissionTeamMember.objects.filter(submission=sub).count() == 0

    # 2) 保存团队草稿（含队长本人 + 2 名队员）-> 仅队员入索引，排除队长本人
    sub = save_draft(
        actor=student,
        submission_id=sub.id,
        expected_version=sub.version,
        data={
            "project_name": "矿大信安实践项目",
            "category": "社会调查",
            "declaration_type": "team",
            "team_members": [
                {"student_number": student.username, "real_name": student.real_name or "队长甲", "class_name": student.class_name or "01班"},
                {"student_number": other_student.username, "real_name": other_student.real_name or "队员乙", "class_name": other_student.class_name or "02班"},
                {"student_number": member_c.username, "real_name": member_c.real_name, "class_name": member_c.class_name},
            ],
        },
    )
    indexed_sns = set(
        SubmissionTeamMember.objects.filter(submission=sub).values_list("student_number", flat=True)
    )
    assert indexed_sns == {other_student.username, member_c.username}
    assert activity.id in find_team_membership_activity_ids(other_student.username)
    assert [s.id for s in find_team_membership_submissions(activity=activity, student_number=other_student.username)] == [sub.id]
    assert set(map_activity_team_memberships_all(activity.id).keys()) == {other_student.username, member_c.username}

    # 3) 修改队员列表（移除 member_c，保留 other_student）-> 索引差量同步
    sub = save_draft(
        actor=student,
        submission_id=sub.id,
        expected_version=sub.version,
        data={
            "project_name": "矿大信安实践项目",
            "category": "社会调查",
            "declaration_type": "team",
            "team_members": [
                {"student_number": other_student.username, "real_name": "更新姓名", "class_name": "02班"},
            ],
        },
    )
    rows = list(SubmissionTeamMember.objects.filter(submission=sub))
    assert len(rows) == 1
    assert rows[0].student_number == other_student.username
    assert rows[0].real_name == "更新姓名"
    assert rows[0].class_name == "02班"
    assert find_team_membership_activity_ids(member_c.username) == set()

    # 4) 提交申报 & 提交审核均保持索引同步
    seed_mandatory_materials(student, activity)
    sub = submit_declaration(actor=student, submission_id=sub.id)
    assert SubmissionTeamMember.objects.filter(submission=sub, student_number=other_student.username).exists()
    sub, _ = submit_for_review(actor=student, submission_id=sub.id)
    assert SubmissionTeamMember.objects.filter(submission=sub, student_number=other_student.username).exists()

    # 5) 模拟历史数据回填迁移 0007
    SubmissionTeamMember.objects.filter(submission=sub).delete()
    assert SubmissionTeamMember.objects.filter(submission=sub).count() == 0
    migration_mod = importlib.import_module("apps.submissions.migrations.0007_backfill_submission_team_members")
    migration_mod.backfill_submission_team_members(django_apps, None)
    assert set(
        SubmissionTeamMember.objects.filter(submission=sub).values_list("student_number", flat=True)
    ) == {other_student.username}

    # 6) 团队转个人 -> 索引行清空；删除申报 -> 索引行级联清理
    sub2 = create_draft(actor=student, activity_id=activity.id, force_new=True)
    sub2 = save_draft(
        actor=student,
        submission_id=sub2.id,
        expected_version=sub2.version,
        data={
            "project_name": "待删除团队项目",
            "category": "社会调查",
            "declaration_type": "team",
            "team_members": [
                {"student_number": member_c.username, "real_name": member_c.real_name, "class_name": member_c.class_name},
            ],
        },
    )
    assert SubmissionTeamMember.objects.filter(submission=sub2, student_number=member_c.username).exists()
    delete_submission(actor=student, submission_id=sub2.id)
    assert not SubmissionTeamMember.objects.filter(submission_id=sub2.id).exists()


@pytest.mark.django_db
def test_redundant_member_draft_ids_single_sql_and_accuracy(activity, student, other_student):
    solo_student = User.objects.create_user(
        username="20250904",
        password="password123",
        real_name="独立生",
        class_name="01班",
        grade="2025级",
        major="信息安全",
        must_change_password=False,
    )
    ensure_standard_material_requirements(activity)

    # 队长有有效团队申报（含 other_student）+ 一条自己的空白草稿（应判定冗余）
    team_sub = create_draft(actor=student, activity_id=activity.id, force_new=True)
    save_draft(
        actor=student,
        submission_id=team_sub.id,
        expected_version=team_sub.version,
        data={
            "project_name": "团队实践",
            "category": "社会调查",
            "declaration_type": "team",
            "team_members": [
                {"student_number": other_student.username, "real_name": other_student.real_name or "队员乙", "class_name": other_student.class_name or "02班"},
            ],
        },
    )
    leader_empty_draft = create_draft(actor=student, activity_id=activity.id, force_new=True)

    # 队员有一条无上传的空白草稿（应判定冗余）
    member_empty_draft = create_draft(actor=other_student, activity_id=activity.id, force_new=True)

    # 独立生仅有一条空白草稿（不应判定冗余）
    solo_empty_draft = create_draft(actor=solo_student, activity_id=activity.id, force_new=True)

    with CaptureQueriesContext(connection) as ctx:
        redundant = set(redundant_member_draft_ids([activity.id]))

    assert len(ctx) == 1, f"redundant_member_draft_ids 应只发 1 条 SQL，实际 {len(ctx)} 条"
    assert redundant == {leader_empty_draft.id, member_empty_draft.id}
    assert solo_empty_draft.id not in redundant


# =====================================================================
# 2.1 + 2.2: 2000 学生数据集首页耗时 (<100ms)、SQL (<=10)、0 写 SQL
# =====================================================================

@pytest.mark.django_db
def test_2000_students_homepage_sql_budget_latency_and_zero_writes(client, activity):
    ensure_standard_material_requirements(activity)
    pw_hash = make_password("123456")

    # 批量造 2000 名学生（避免 2000 次 PBKDF2，复用预计算哈希）
    users = [
        User(
            username=f"2500{i:04d}",
            password=pw_hash,
            real_name=f"学生{i:04d}",
            class_name=f"{(i % 10) + 1:02d}班",
            grade="2025级",
            major="信息安全",
            role=User.Role.STUDENT,
            must_change_password=False,
            is_active=True,
        )
        for i in range(2000)
    ]
    User.objects.bulk_create(users, batch_size=500)
    created_users = list(User.objects.filter(username__startswith="2500").order_by("username"))
    assert len(created_users) == 2000

    # 批量造 1200 份申报（前 800 份为团队申报、每份 2 名队员，后 400 份为个人申报）并写入索引表
    submissions = []
    team_member_rows = []
    for idx in range(1200):
        leader = created_users[idx]
        sub_id = uuid.uuid4()
        if idx < 800:
            m1 = created_users[(idx + 800) % 2000]
            m2 = created_users[(idx + 1200) % 2000]
            draft_data = {
                "project_name": f"社会实践项目{idx:04d}",
                "category": "社会调查",
                "declaration_type": "team",
                "team_members": [
                    {"student_number": m1.username, "real_name": m1.real_name, "class_name": m1.class_name},
                    {"student_number": m2.username, "real_name": m2.real_name, "class_name": m2.class_name},
                ],
            }
            team_member_rows.append(
                SubmissionTeamMember(
                    submission_id=sub_id,
                    activity_id=activity.id,
                    student_number=m1.username,
                    real_name=m1.real_name,
                    class_name=m1.class_name,
                )
            )
            team_member_rows.append(
                SubmissionTeamMember(
                    submission_id=sub_id,
                    activity_id=activity.id,
                    student_number=m2.username,
                    real_name=m2.real_name,
                    class_name=m2.class_name,
                )
            )
        else:
            draft_data = {
                "project_name": f"个人实践项目{idx:04d}",
                "category": "红色寻访",
                "declaration_type": "individual",
                "team_members": [],
            }
        submissions.append(
            Submission(
                id=sub_id,
                owner=leader,
                activity=activity,
                status=Submission.Status.PENDING_FIRST_REVIEW,
                draft_data=draft_data,
                declaration_submitted=True,
                version=1,
            )
        )
    Submission.objects.bulk_create(submissions, batch_size=500)
    SubmissionTeamMember.objects.bulk_create(team_member_rows, batch_size=500)

    # 3 类代表性学生：
    # - created_users[1999]: 无本人申报、但作为 idx=799 的队员
    # - created_users[10]: 本人已申报队长
    # - created_users[1500]: 团队队员
    # 另建 1 名完全无申报也无团队的纯新生
    freshman = User.objects.create_user(
        username="25999999",
        password="password123",
        real_name="纯新生",
        class_name="01班",
        grade="2025级",
        major="信息安全",
        must_change_password=False,
    )

    # 先预热一次模板编译
    client.force_login(freshman)
    assert client.get("/").status_code == 200
    assert not Submission.objects.filter(owner=freshman, activity=activity).exists()

    for label, target_user in (
        ("freshman", freshman),
        ("leader", created_users[10]),
        ("member", created_users[1999]),
    ):
        client.force_login(target_user)
        with CaptureQueriesContext(connection) as ctx:
            t0 = time.perf_counter()
            resp = client.get("/")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert resp.status_code == 200
        write_sqls = _extract_write_sqls(ctx)
        assert write_sqls == [], f"{label} 访问 GET / 产生了写 SQL: {write_sqls}"
        assert len(ctx) <= 10, f"{label} 访问 GET / 执行了 {len(ctx)} 条 SQL（上限 10）"
        assert elapsed_ms < 100.0, f"{label} 访问 GET / 耗时 {elapsed_ms:.2f} ms（上限 100 ms）"

    # 纯新生多次刷新 GET / 绝不会自动创建空白草稿
    assert not Submission.objects.filter(owner=freshman, activity=activity).exists()

    # 管理员访问 GET /admin-panel/ 与 GET / 写 SQL 严格为 0
    admin_user = User.objects.create_user(
        username="admin_perf_check",
        password="password123",
        real_name="学院管理员",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )
    client.force_login(admin_user)
    with CaptureQueriesContext(connection) as admin_ctx:
        admin_resp = client.get("/admin-panel/")
    assert admin_resp.status_code == 200
    assert _extract_write_sqls(admin_ctx) == [], f"GET /admin-panel/ 产生了写 SQL: {_extract_write_sqls(admin_ctx)}"


# =====================================================================
# 2.2 ensure_standard_material_requirements 只读优先
# =====================================================================

@pytest.mark.django_db
def test_ensure_standard_material_requirements_readonly_when_present(activity):
    # 首次调用补齐 5 条规则
    reqs = ensure_standard_material_requirements(activity)
    assert len(reqs) == 5

    # 再次调用：仅 1 条 SELECT，0 条写 SQL
    with CaptureQueriesContext(connection) as ctx:
        reqs_again = ensure_standard_material_requirements(activity)
    assert len(reqs_again) == 5
    assert _extract_write_sqls(ctx) == []
    assert len(ctx) == 1

    # 删掉其中 1 条后再调用：仅补建缺失的 1 条，不覆盖另外 4 条
    MaterialRequirement.objects.filter(activity=activity, code="video").delete()
    with CaptureQueriesContext(connection) as ctx_fill:
        reqs_refilled = ensure_standard_material_requirements(activity)
    assert len(reqs_refilled) == 5
    fill_writes = _extract_write_sqls(ctx_fill)
    assert len(fill_writes) == 1
    assert fill_writes[0].upper().startswith("INSERT")

    # 显式 sync_existing=True 时可刷新规则字段
    MaterialRequirement.objects.filter(activity=activity, code="video").update(title="旧名称")
    reqs_synced = ensure_standard_material_requirements(activity, sync_existing=True)
    assert reqs_synced["video"].title == "宣讲视频"


# =====================================================================
# 2.3 审核列表 N+1 消除（SQL <= 8）& 单次审核动作 SQL <= 6（无全表扫描）
# =====================================================================

@pytest.mark.django_db
def test_reviews_api_eliminates_n_plus_one_and_review_action_sql_budget(client, activity, monkeypatch):
    ensure_standard_material_requirements(activity)
    admin_user = User.objects.create_user(
        username="admin_rev_perf",
        password="password123",
        real_name="终审老师",
        role=User.Role.ADMIN,
        is_staff=True,
        must_change_password=False,
    )
    reviewer = User.objects.create_user(
        username="rev_class01",
        password="password123",
        real_name="一班团支书",
        role=User.Role.REVIEWER,
        class_name="01班",
        grade="2025级",
        major="信息安全",
        must_change_password=False,
    )
    pw_hash = make_password("123456")

    # 构造 100 名学生（前 60 名在 01班，后 40 名在 02班），每人 1 条待初审申报 + 1 条版本 + 1 条历史审核记录
    students = [
        User(
            username=f"2501{i:04d}",
            password=pw_hash,
            real_name=f"同学{i:03d}",
            class_name="01班" if i < 60 else "02班",
            grade="2025级",
            major="信息安全",
            role=User.Role.STUDENT,
            must_change_password=False,
            is_active=True,
        )
        for i in range(100)
    ]
    User.objects.bulk_create(students)
    created_students = list(User.objects.filter(username__startswith="2501").order_by("username"))

    subs = []
    revs = []
    records = []
    for idx, stu in enumerate(created_students):
        sub_id = uuid.uuid4()
        rev_id = uuid.uuid4()
        subs.append(
            Submission(
                id=sub_id,
                owner=stu,
                activity=activity,
                status=Submission.Status.PENDING_FIRST_REVIEW,
                draft_data={"project_name": f"审核项目{idx:03d}", "category": "社会调查", "declaration_type": "individual", "team_members": []},
                declaration_submitted=True,
                version=1,
            )
        )
        revs.append(
            SubmissionRevision(
                id=rev_id,
                submission_id=sub_id,
                number=1,
                snapshot={"project_name": f"审核项目{idx:03d}"},
                request_key=uuid.uuid4(),
            )
        )
        records.append(
            ReviewRecord(
                submission_id=sub_id,
                revision_id=rev_id,
                reviewer=reviewer,
                stage=ReviewRecord.Stage.FIRST,
                decision=ReviewRecord.Decision.RETURNED,
                comment=f"历史意见{idx}",
                request_key=uuid.uuid4(),
            )
        )
    Submission.objects.bulk_create(subs)
    SubmissionRevision.objects.bulk_create(revs)
    ReviewRecord.objects.bulk_create(records)

    # 1) 管理员拉取 GET /api/v1/reviews/（批次 3 接入分页，每页默认 50 条，total=100）：SQL 必须 <= 8 条
    client.force_login(admin_user)
    with CaptureQueriesContext(connection) as list_ctx:
        resp = client.get(f"/api/v1/reviews/?activity_id={activity.id}")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 100
    assert len(payload["items"]) == 50
    assert payload["has_next"] is True
    assert all(len(item["review_records"]) == 1 for item in payload["items"])
    assert len(list_ctx) <= 8, f"GET /api/v1/reviews/ 执行了 {len(list_ctx)} 条 SQL（上限 8）"

    # 2) 团支书拉取本班列表（total=60，每页默认 50 条）：同样 <= 8 条
    client.force_login(reviewer)
    with CaptureQueriesContext(connection) as rev_list_ctx:
        rev_resp = client.get(f"/api/v1/reviews/?activity_id={activity.id}")
    assert rev_resp.status_code == 200
    rev_payload = rev_resp.json()
    assert rev_payload["total"] == 60
    assert len(rev_payload["items"]) == 50
    assert len(rev_list_ctx) <= 8

    # 3) 审核动作绝不能调用 reviewable_submissions 扫全表，且单次动作 SQL <= 6 条
    def _boom(*args, **kwargs):
        raise AssertionError("审核动作不应调用 reviewable_submissions 扫描全表！")

    monkeypatch.setattr("apps.reviews.selectors.reviewable_submissions", _boom)

    target_in_class = Submission.objects.select_related("owner").get(pk=subs[0].id)
    target_other_class = Submission.objects.select_related("owner").get(pk=subs[70].id)
    assert has_first_review_permission(reviewer, activity.id, target_in_class) is True
    assert has_first_review_permission(reviewer, activity.id, target_other_class) is False
    assert has_first_review_permission(admin_user, activity.id, target_other_class) is True

    with CaptureQueriesContext(connection) as first_ctx:
        rec1, updated1 = review_submission(
            actor=reviewer,
            activity_id=activity.id,
            submission_id=target_in_class.id,
            decision=ReviewRecord.Decision.APPROVED,
            comment="初审通过",
        )
    assert updated1.status == Submission.Status.PENDING_FINAL_REVIEW
    assert rec1.stage == ReviewRecord.Stage.FIRST
    first_sqls = _extract_non_savepoint_sqls(first_ctx)
    assert len(first_sqls) <= 6, f"团支书初审执行了 {len(first_sqls)} 条 SQL（上限 6）：{first_sqls}"

    with CaptureQueriesContext(connection) as final_ctx:
        rec2, updated2 = review_submission(
            actor=admin_user,
            activity_id=activity.id,
            submission_id=target_in_class.id,
            decision=ReviewRecord.Decision.APPROVED,
            comment="终审通过",
        )
    assert updated2.status == Submission.Status.FINAL_APPROVED
    assert rec2.stage == ReviewRecord.Stage.FINAL
    final_sqls = _extract_non_savepoint_sqls(final_ctx)
    assert len(final_sqls) <= 6, f"管理员终审执行了 {len(final_sqls)} 条 SQL（上限 6）：{final_sqls}"


# =====================================================================
# 2.3 Admin 批量初始化/重置密码防超时保护、Celery 任务与命令行脚本
# =====================================================================

@pytest.mark.django_db
def test_admin_batch_actions_guard_celery_and_cli_commands(settings, monkeypatch):
    rf = RequestFactory()
    site = AdminSite()
    user_admin = PlatformUserAdmin(User, site)
    roster_admin = RosterAdmin(StudentRoster, site)

    admin_user = User.objects.create_superuser(username="root_batch", email="root@example.com", password="password123")

    # 准备 55 条花名册记录（超过同步阈值 50）
    rosters = [
        StudentRoster(
            student_number=f"2502{i:04d}",
            real_name=f"花名册{i:03d}",
            class_name="01班",
            grade="2025级",
            major="信息安全",
            phone=f"1380013{i:04d}",
            active=True,
        )
        for i in range(ADMIN_SYNC_PASSWORD_BATCH_LIMIT + 5)
    ]
    StudentRoster.objects.bulk_create(rosters)
    roster_qs = StudentRoster.objects.filter(student_number__startswith="2502")

    # 1) 测试环境（CELERY_TASK_ALWAYS_EAGER=True）超阈值：阻止同步跑 55 次 PBKDF2，返回告警
    settings.CELERY_TASK_ALWAYS_EAGER = True
    req_warn = _attach_messages(rf.post("/admin/"))
    req_warn.user = admin_user
    roster_admin.initialize_student_accounts(req_warn, roster_qs)
    assert User.objects.filter(username__startswith="2502").count() == 0
    warn_texts = [str(m) for m in req_warn._messages]
    assert any("超过后台同步安全上限" in msg for msg in warn_texts)

    # 2) 启用异步队列时超阈值：投递 Celery .delay() 任务而不阻塞
    settings.CELERY_TASK_ALWAYS_EAGER = False
    settings.CELERY_BROKER_URL = "redis://localhost:6379/0"
    dispatched = {}

    def _fake_delay(ids, actor_id):
        dispatched["ids"] = ids
        dispatched["actor_id"] = actor_id
        return SimpleNamespace(id="celery-task-init-001")

    monkeypatch.setattr(
        "apps.accounts.admin.batch_initialize_student_accounts_task.delay",
        _fake_delay,
    )
    req_async = _attach_messages(rf.post("/admin/"))
    req_async.user = admin_user
    roster_admin.initialize_student_accounts(req_async, roster_qs)
    assert len(dispatched["ids"]) == ADMIN_SYNC_PASSWORD_BATCH_LIMIT + 5
    assert dispatched["actor_id"] == admin_user.pk
    assert any("celery-task-init-001" in str(m) for m in req_async._messages)

    # 3) 小批量（<= 50，这里取 2 人）直接同步执行
    small_qs = StudentRoster.objects.filter(student_number__in=["25020000", "25020001"])
    req_sync = _attach_messages(rf.post("/admin/"))
    req_sync.user = admin_user
    roster_admin.initialize_student_accounts(req_sync, small_qs)
    assert User.objects.filter(username__in=["25020000", "25020001"]).count() == 2

    # 4) 直接执行 Celery 任务函数（小集合验证逻辑闭环）
    task_target_ids = [
        str(rid)
        for rid in StudentRoster.objects.filter(student_number__in=["25020002", "25020003"]).values_list("pk", flat=True)
    ]
    init_res = batch_initialize_student_accounts_task(task_target_ids, actor_id=admin_user.pk, batch_size=1)
    assert init_res["created"] == 2
    assert User.objects.get(username="25020002").check_password("020002") is True

    # 5) Admin 重置密码超阈值保护 + Celery 重置任务验证
    pw_hash = make_password("CustomPass#1")
    extra_users = [
        User(
            username=f"2503{i:04d}",
            password=pw_hash,
            real_name=f"待重置{i:03d}",
            class_name="03班",
            grade="2025级",
            major="信息安全",
            role=User.Role.STUDENT,
            must_change_password=False,
        )
        for i in range(ADMIN_SYNC_PASSWORD_BATCH_LIMIT + 5)
    ]
    User.objects.bulk_create(extra_users)
    reset_qs = User.objects.filter(username__startswith="2503")

    reset_dispatched = {}

    def _fake_reset_delay(ids, actor_id):
        reset_dispatched["ids"] = ids
        return SimpleNamespace(id="celery-task-reset-001")

    monkeypatch.setattr("apps.accounts.admin.batch_reset_passwords_task.delay", _fake_reset_delay)
    req_reset = _attach_messages(rf.post("/admin/"))
    req_reset.user = admin_user
    user_admin.reset_password_to_last_six(req_reset, reset_qs)
    assert len(reset_dispatched["ids"]) == ADMIN_SYNC_PASSWORD_BATCH_LIMIT + 5
    assert any("celery-task-reset-001" in str(m) for m in req_reset._messages)

    two_user_ids = list(User.objects.filter(username__in=["25030000", "25030001"]).values_list("pk", flat=True))
    reset_res = batch_reset_passwords_task(two_user_ids, actor_id=admin_user.pk, batch_size=1)
    assert reset_res["reset"] == 2
    u0 = User.objects.get(username="25030000")
    assert u0.check_password("030000") is True
    assert u0.must_change_password is True

    # 6) 命令行脚本 batch_init_students & batch_reset_passwords 验证
    StudentRoster.objects.filter(student_number__startswith="2502").exclude(
        student_number__in=["25020000", "25020001", "25020002", "25020003", "25020004"]
    ).delete()
    call_command("batch_init_students", "--all-roster", "--batch-size", "2")
    assert User.objects.filter(username="25020004").exists()

    call_command("batch_reset_passwords", "--student-numbers", "25030002,25030003", "--batch-size", "1")
    u2 = User.objects.get(username="25030002")
    assert u2.check_password("030002") is True
    assert u2.must_change_password is True
