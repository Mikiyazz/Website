"""新闻稿（apps.news）后端行为测试。

覆盖：Word 上传入口的四层校验（.docx only）、覆盖重传、越权、主申报前置状态、
乐观锁、两级审核、退回必填意见、终审限权、提交后仍可改、已采纳锁定。
"""
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.accounts.models import User
from apps.common.errors import Conflict, DomainError, Forbidden
from apps.news.models import NewsDraft, NewsRevision
from apps.news.services import (
    get_or_create_news,
    review_news,
    save_news,
    submit_news,
)
from apps.reviews.models import ReviewAssignment
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft, submit_for_review
from apps.uploads.services import direct_upload, ensure_standard_material_requirements
from tests.conftest import seed_mandatory_materials

DOCX_MAGIC = b"PK\x03\x04"


def docx_bytes(payload: bytes = b"") -> bytes:
    """最小 ZIP/DOCX 头：魔数校验只看前 32 字节的 PK\\x03\\x04。"""
    return DOCX_MAGIC + payload + b"\x00" * 64


def upload_news_docx(student, activity, name="新闻稿.docx", payload=b""):
    reqs = ensure_standard_material_requirements(activity)
    return direct_upload(
        actor=student,
        requirement_id=reqs["news_document"].id,
        file_obj=SimpleUploadedFile(name, docx_bytes(payload)),
    )


def submitted_submission(student, activity):
    """造一份「已正式提交」的主申报，满足新闻稿提交的前置状态。"""
    draft = create_draft(actor=student, activity_id=activity.pk)
    save_draft(actor=student, submission_id=draft.pk, expected_version=0, data={"project_name": "暑期社会实践"})
    seed_mandatory_materials(student, activity)
    submission, _ = submit_for_review(actor=student, submission_id=draft.pk)
    assert submission.status == Submission.Status.PENDING_FIRST_REVIEW
    return submission


def make_reviewer(student, activity):
    reviewer = User.objects.create_user(
        username="tzs-news", password="pwd", role=User.Role.REVIEWER, class_name=student.class_name
    )
    ReviewAssignment.objects.create(reviewer=reviewer, activity=activity, class_name=student.class_name)
    return reviewer


def make_admin():
    return User.objects.create_user(username="admin-news", password="pwd", role=User.Role.ADMIN, is_staff=True)


def ready_news(student, activity, title="我院学子开展暑期实践", body="新闻稿正文说明。"):
    """已上传 Word 且填好标题备注的新闻稿草稿。"""
    submission = submitted_submission(student, activity)
    upload_news_docx(student, activity)
    news = get_or_create_news(actor=student, submission_id=submission.pk)
    return save_news(actor=student, news_id=news.pk, expected_version=news.version, title=title, body=body)


# =====================================================================
# 1. 第 6 类材料：只收 .docx，复用现有四层校验
# =====================================================================

def test_news_document_rule_is_optional_docx_only(activity):
    reqs = ensure_standard_material_requirements(activity)
    rule = reqs["news_document"]
    assert rule.required is False
    assert rule.allowed_extensions == [".docx"]
    assert rule.max_file_bytes == 20 * 1024 * 1024
    assert rule.max_count == 1


def test_news_docx_upload_passes_and_is_valid(student, activity):
    upload, asset, _ = upload_news_docx(student, activity)
    assert upload.requirement.code == "news_document"
    assert asset.status == asset.Status.VALID


def test_news_upload_rejects_pdf_disguised_as_docx(student, activity):
    reqs = ensure_standard_material_requirements(activity)
    with pytest.raises(DomainError) as excinfo:
        direct_upload(
            actor=student,
            requirement_id=reqs["news_document"].id,
            file_obj=SimpleUploadedFile("新闻稿.docx", b"%PDF-1.7\n" + b"0" * 64),
        )
    assert "魔数" in str(excinfo.value)


def test_news_upload_rejects_legacy_doc_extension(student, activity):
    reqs = ensure_standard_material_requirements(activity)
    with pytest.raises(DomainError):
        direct_upload(
            actor=student,
            requirement_id=reqs["news_document"].id,
            file_obj=SimpleUploadedFile("新闻稿.doc", docx_bytes()),
        )


# =====================================================================
# 2. 越权与前置状态
# =====================================================================

def test_cannot_open_other_students_news(student, other_student, activity):
    submission = submitted_submission(student, activity)
    news = get_or_create_news(actor=student, submission_id=submission.pk)
    save_news(actor=student, news_id=news.pk, expected_version=0, title="标题", body="正文")

    with pytest.raises(NewsDraft.DoesNotExist):
        save_news(actor=other_student, news_id=news.pk, expected_version=1, title="劫持", body="劫持")


def test_submit_requires_submitted_submission(student, activity):
    """主申报还在草稿状态时，不能提交新闻稿。"""
    draft = create_draft(actor=student, activity_id=activity.pk)
    upload_news_docx(student, activity)
    news = get_or_create_news(actor=student, submission_id=draft.pk)
    save_news(actor=student, news_id=news.pk, expected_version=0, title="标题", body="正文")

    with pytest.raises(Conflict):
        submit_news(actor=student, news_id=news.pk)


def test_submit_requires_docx_attachment(student, activity):
    submission = submitted_submission(student, activity)
    news = get_or_create_news(actor=student, submission_id=submission.pk)
    save_news(actor=student, news_id=news.pk, expected_version=0, title="标题", body="正文")

    with pytest.raises(DomainError) as excinfo:
        submit_news(actor=student, news_id=news.pk)
    assert "docx" in str(excinfo.value).lower()


# =====================================================================
# 3. 乐观锁与提交幂等
# =====================================================================

def test_stale_version_is_rejected(student, activity):
    submission = submitted_submission(student, activity)
    news = get_or_create_news(actor=student, submission_id=submission.pk)
    save_news(actor=student, news_id=news.pk, expected_version=0, title="标题", body="正文")

    with pytest.raises(Conflict):
        save_news(actor=student, news_id=news.pk, expected_version=0, title="旧版本", body="旧版本")


def test_submit_is_idempotent_by_request_key(student, activity):
    news = ready_news(student, activity)
    key = uuid.uuid4()
    first, rev1 = submit_news(actor=student, news_id=news.pk, request_key=key)
    assert first.status == NewsDraft.Status.SUBMITTED
    assert NewsRevision.objects.filter(news=first).count() == 1

    again, rev2 = submit_news(actor=student, news_id=first.pk, request_key=key)
    assert rev2.pk == rev1.pk
    assert NewsRevision.objects.filter(news=first).count() == 1


# =====================================================================
# 4. 两级审核
# =====================================================================

def test_first_review_moves_to_reviewing(student, activity):
    news = ready_news(student, activity)
    news, _ = submit_news(actor=student, news_id=news.pk)
    reviewer = make_reviewer(student, activity)

    reviewed = review_news(actor=reviewer, news_id=news.pk, decision="approve", comment="团支部初审通过")
    assert reviewed.status == NewsDraft.Status.REVIEWING
    assert reviewed.reviewed_version == reviewed.version


def test_reviewer_cannot_finalize(student, activity):
    news = ready_news(student, activity)
    news, _ = submit_news(actor=student, news_id=news.pk)
    reviewer = make_reviewer(student, activity)
    review_news(actor=reviewer, news_id=news.pk, decision="approve", comment="初审通过")

    with pytest.raises(Forbidden):
        review_news(actor=reviewer, news_id=news.pk, decision="approve", comment="想直接终审")


def test_return_requires_comment(student, activity):
    news = ready_news(student, activity)
    news, _ = submit_news(actor=student, news_id=news.pk)
    reviewer = make_reviewer(student, activity)

    with pytest.raises(DomainError) as excinfo:
        review_news(actor=reviewer, news_id=news.pk, decision="return", comment="   ")
    assert "意见" in str(excinfo.value)


def test_final_approval_by_admin_locks_news(student, activity):
    news = ready_news(student, activity)
    news, _ = submit_news(actor=student, news_id=news.pk)
    reviewer = make_reviewer(student, activity)
    admin = make_admin()

    review_news(actor=reviewer, news_id=news.pk, decision="approve", comment="初审通过")
    approved = review_news(actor=admin, news_id=news.pk, decision="approve", comment="团委终审通过")
    assert approved.status == NewsDraft.Status.APPROVED

    # 已采纳后锁定：既不能改，也不能重新提交。
    with pytest.raises(Forbidden):
        save_news(actor=student, news_id=news.pk, expected_version=approved.version, title="改标题", body="改正文")
    with pytest.raises(Forbidden):
        submit_news(actor=student, news_id=news.pk)


# =====================================================================
# 5. 「提交后仍能改」：submitted / reviewing 都允许重提并生成新版本
# =====================================================================

def test_resubmit_after_submitted_bumps_version(student, activity):
    news = ready_news(student, activity)
    news, first_rev = submit_news(actor=student, news_id=news.pk)
    version_after_first = news.version

    edited = save_news(
        actor=student, news_id=news.pk, expected_version=version_after_first, title="修订标题", body="修订正文"
    )
    resubmitted, second_rev = submit_news(actor=student, news_id=edited.pk)

    assert resubmitted.status == NewsDraft.Status.SUBMITTED
    assert second_rev.number > first_rev.number
    assert NewsRevision.objects.filter(news=resubmitted).count() == 2
    assert second_rev.snapshot["title"] == "修订标题"


def test_resubmit_after_first_review_returns_to_submitted(student, activity):
    news = ready_news(student, activity)
    news, _ = submit_news(actor=student, news_id=news.pk)
    reviewer = make_reviewer(student, activity)
    reviewing = review_news(actor=reviewer, news_id=news.pk, decision="approve", comment="初审通过")
    assert reviewing.status == NewsDraft.Status.REVIEWING

    edited = save_news(
        actor=student, news_id=news.pk, expected_version=reviewing.version, title="再改一次", body="再改一次"
    )
    resubmitted, _ = submit_news(actor=student, news_id=edited.pk)
    # 重提回到待初审，审核人按 reviewed_version 判断审的是旧版。
    assert resubmitted.status == NewsDraft.Status.SUBMITTED
    assert resubmitted.version > reviewing.version


def test_returned_news_can_be_edited_and_resubmitted(student, activity):
    news = ready_news(student, activity)
    news, _ = submit_news(actor=student, news_id=news.pk)
    reviewer = make_reviewer(student, activity)
    returned = review_news(actor=reviewer, news_id=news.pk, decision="return", comment="请补充实践照片说明")
    assert returned.status == NewsDraft.Status.RETURNED
    assert returned.returned_comment == "请补充实践照片说明"

    edited = save_news(
        actor=student, news_id=news.pk, expected_version=returned.version, title="已补充", body="已补充说明"
    )
    resubmitted, _ = submit_news(actor=student, news_id=edited.pk)
    assert resubmitted.status == NewsDraft.Status.SUBMITTED


# =====================================================================
# 6. 重复上传覆盖（max_count=1）
# =====================================================================

def test_reupload_replaces_previous_docx(student, activity):
    first_upload, _, _ = upload_news_docx(student, activity, name="新闻稿.docx")
    second_upload, _, replaced = upload_news_docx(student, activity, name="新闻稿-修订.docx", payload=b"v2")

    assert str(first_upload.pk) in {str(item) for item in replaced}
    assert second_upload.pk != first_upload.pk


# =====================================================================
# 7. 页面与接口：学生端 /news/、独立审核页 /reviews/news/、/api/v1/news/
# =====================================================================

def test_news_list_page_renders_student_entries(client, student, activity):
    news = ready_news(student, activity, title="列表页标题")
    client.force_login(student)
    resp = client.get("/news/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "我的新闻稿" in html
    assert "列表页标题" in html
    assert f"/news/edit/?activity_id={activity.pk}" in html
    assert news.status == NewsDraft.Status.DRAFT


def test_news_edit_page_renders_docx_uploader(client, student, activity):
    submitted_submission(student, activity)
    client.force_login(student)
    resp = client.get(f"/news/edit/?activity_id={activity.pk}")
    assert resp.status_code == 200
    html = resp.content.decode()
    # FilePond 拖拽区：input 原样输出，由底部脚本包裹成带上传按钮的容器。
    assert 'class="filepond-zone"' in html
    assert 'id="news-docx-input"' in html
    assert 'id="news-material-json"' in html
    assert "vendor/filepond/filepond.min.js" in html
    assert "vendor/filepond/plugins/filepond-plugin-file-validate-size.min.js" in html
    assert "server:" in html and "process:" in html
    assert 'id="btn-news-submit"' in html


def test_news_edit_page_hides_uploader_when_approved(client, student, activity):
    news = ready_news(student, activity)
    news, _ = submit_news(actor=student, news_id=news.pk)
    reviewer = make_reviewer(student, activity)
    admin = make_admin()
    review_news(actor=reviewer, news_id=news.pk, decision="approve", comment="初审通过")
    review_news(actor=admin, news_id=news.pk, decision="approve", comment="终审通过")

    client.force_login(student)
    resp = client.get(f"/news/edit/?activity_id={activity.pk}")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'id="pond-news_document"' not in html
    assert "已锁定" in html


def test_news_submit_page_form_end_to_end(client, student, activity):
    news = ready_news(student, activity, title="表单提交标题")
    client.force_login(student)
    resp = client.post(f"/news/submissions/{news.pk}/submit/")
    assert resp.status_code == 302
    news.refresh_from_db()
    assert news.status == NewsDraft.Status.SUBMITTED


def test_review_page_only_lists_reviewable_news(client, student, other_student, activity):
    mine = ready_news(student, activity, title="本班新闻稿")
    submit_news(actor=student, news_id=mine.pk)

    client.force_login(make_reviewer(student, activity))
    resp = client.get("/reviews/news/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "本班新闻稿" in html
    assert "初审通过" in html


def test_student_cannot_open_review_page(client, student, activity):
    client.force_login(student)
    resp = client.get("/reviews/news/")
    assert resp.status_code == 403


def test_review_action_page_requires_permission(client, student, activity):
    news = ready_news(student, activity)
    news, _ = submit_news(actor=student, news_id=news.pk)
    client.force_login(student)
    resp = client.post(f"/reviews/news/{news.pk}/action/", {"decision": "approve", "comment": "自己给自己过"})
    assert resp.status_code == 400


def test_api_collection_and_detail_roundtrip(client, student, activity):
    submission = submitted_submission(student, activity)
    client.force_login(student)

    created = client.post(
        "/api/v1/news/",
        data={"submission_id": str(submission.pk)},
        content_type="application/json",
    )
    assert created.status_code == 200
    payload = created.json()
    assert payload["status"] == NewsDraft.Status.DRAFT

    patched = client.patch(
        f"/api/v1/news/{payload['id']}/",
        data={"expected_version": payload["version"], "title": "接口标题", "body": "接口正文"},
        content_type="application/json",
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "接口标题"

    listed = client.get("/api/v1/news/")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == payload["id"]


def test_api_rejects_other_students_news(client, student, other_student, activity):
    news = ready_news(student, activity)
    client.force_login(other_student)
    resp = client.get(f"/api/v1/news/{news.pk}/")
    assert resp.status_code == 404


# =====================================================================
# 8. 导出：ZIP 内含新闻稿，Excel 第 7 列反映新闻稿状态
# =====================================================================

def test_excel_export_has_news_status_column(client, student, activity):
    import io

    import openpyxl

    from apps.accounts.models import User

    ready_news(student, activity)  # 该学生有新闻稿草稿
    admin = User.objects.create_user(username="admin-excel", password="pwd", role=User.Role.ADMIN, is_superuser=True)

    client.force_login(admin)
    resp = client.get(f"/api/v1/exports/activities/{activity.pk}/excel/")
    assert resp.status_code == 200

    rows = list(openpyxl.load_workbook(io.BytesIO(resp.content)).active.iter_rows(values_only=True))
    assert rows[0] == ("学号", "姓名", "班级", "项目名", "类别", "审核状态", "新闻稿状态")
    # 有新闻稿的学生写状态文案，没有新闻稿的行写「未提交」。
    assert rows[1][0] == student.username
    assert rows[1][6] == "草稿"


def test_class_zip_contains_news_docx(client, student, activity):
    import zipfile

    from apps.accounts.models import User

    news = ready_news(student, activity, title="实践新闻稿")
    submit_news(actor=student, news_id=news.pk)
    admin = User.objects.create_user(username="admin-zip", password="pwd", role=User.Role.ADMIN, is_superuser=True)

    client.force_login(admin)
    resp = client.get(
        f"/api/v1/exports/activities/{activity.pk}/zip/",
        {"class_name": student.class_name},
    )
    assert resp.status_code == 200

    with zipfile.ZipFile(io_bytes(resp)) as archive:
        names = archive.namelist()
    news_entries = [name for name in names if name.endswith("实践新闻稿.docx")]
    assert news_entries, names
    assert any("/news_document/" not in name for name in names)


def test_async_export_entries_include_news_docx(student, activity):
    from apps.exports.services import build_export_entries

    news = ready_news(student, activity, title="异步导出新闻稿")
    submit_news(actor=student, news_id=news.pk)

    entries = build_export_entries(activity=activity, allowed_classes=None)
    arcnames = [entry["arcname"] for entry in entries]
    assert any(name.endswith("/新闻稿/新闻稿.docx") for name in arcnames), arcnames


def io_bytes(response):
    """把测试客户端的响应体包成 BytesIO，供 zipfile 读取。"""
    import io

    return io.BytesIO(response.content)


# =====================================================================
# 9. 新闻稿附件：可查看、可删除（提交后也能删，已采纳则隐藏删除按钮）
# =====================================================================

def test_news_document_delete_api(client, student, activity):
    from apps.news.services import current_document

    news = ready_news(student, activity)
    upload = current_document(news.submission)
    assert upload is not None

    client.force_login(student)
    resp = client.post(f"/api/v1/news/{news.pk}/document/")
    assert resp.status_code == 200

    news.refresh_from_db()
    assert current_document(news.submission) is None
    upload.refresh_from_db()
    assert upload.status == upload.Status.ABORTED


def test_news_document_delete_after_submit_allowed(client, student, activity):
    """主申报已提交（pending_first_review）时，新闻稿文件仍应允许学生自己删除重传。"""
    from apps.news.services import current_document

    news = ready_news(student, activity)
    news, _ = submit_news(actor=student, news_id=news.pk)

    client.force_login(student)
    resp = client.post(f"/api/v1/news/{news.pk}/document/")
    assert resp.status_code == 200
    assert current_document(news.submission) is None


def test_news_document_delete_rejects_other_student(client, student, other_student, activity):
    news = ready_news(student, activity)
    client.force_login(other_student)
    resp = client.post(f"/api/v1/news/{news.pk}/document/")
    assert resp.status_code == 404


def test_news_list_page_exposes_view_and_delete(client, student, activity):
    ready_news(student, activity, title="可查看可删除")
    client.force_login(student)
    resp = client.get("/news/")
    html = resp.content.decode()
    assert "js-delete-news-doc" in html
    assert "/document/" in html
    assert ">打开<" in html
    assert ">下载<" in html


def test_news_edit_page_exposes_delete_button(client, student, activity):
    news = ready_news(student, activity)
    client.force_login(student)
    resp = client.get(f"/news/edit/?activity_id={activity.pk}")
    html = resp.content.decode()
    assert 'id="btn-delete-news-doc"' in html
    assert f"/api/v1/news/{news.pk}/document/" in html


def test_dashboard_shows_news_entry(client, student, activity):
    client.force_login(student)
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "新闻稿（选交）" in html
    assert 'href="/news/"' in html


