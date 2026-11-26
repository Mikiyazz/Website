"""新闻稿页面与 API 入口。

页面（学生）
------------
GET  /news/                            我的新闻稿列表
GET  /news/edit/                       编辑页（可带 ?activity_id= 预选活动）
POST /news/submissions/<uuid>/save/    保存草稿（页面表单）
POST /news/submissions/<uuid>/submit/  提交 / 重新提交

审核（独立页，不改动 reviews/workbench）
--------------------------------------
GET  /reviews/news/                    团支书初审队列 + 管理员终审队列
POST /reviews/news/<uuid>/action/      审核动作

根路由前缀见 config/urls.py：
  /api/v1/news/  -> apps.news.urls
  /news/ + /reviews/news/ -> apps.news.page_urls
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import is_college_admin, require_student
from apps.activities.selectors import eligible_activities
from apps.audit.decorators import audit_rejections
from apps.common.api import endpoint, json_body
from apps.common.errors import DomainError
from apps.news.models import NewsDraft
from apps.news.selectors import (
    my_news_queryset,
    news_belongs_to_reviewer,
    review_queue_activities,
    review_queue_queryset,
)
from apps.news.services import (
    current_document,
    get_or_create_news,
    review_news,
    save_news,
    serialize_news,
    submit_news,
)
from apps.submissions.models import Submission
from apps.uploads.services import delete_uploaded_file, ensure_standard_material_requirements


# =====================================================================
# JSON API：/api/v1/news/
# =====================================================================

@endpoint("GET", "POST")
@audit_rejections("news.list.rejected")
def api_collection(request):
    require_student(request.user)
    if request.method == "GET":
        items = my_news_queryset(request.user).order_by("-updated_at")[:100]
        return JsonResponse({"items": [serialize_news(item) for item in items], "limit": 100})

    data = json_body(request)
    submission_id = data.get("submission_id")
    if not submission_id:
        raise DomainError("缺少必要参数：submission_id。")
    news = get_or_create_news(actor=request.user, submission_id=submission_id)
    return JsonResponse(serialize_news(news), status=200)


@endpoint("GET", "PATCH")
@audit_rejections("news.detail.rejected")
def api_detail(request, news_id):
    require_student(request.user)
    if request.method == "GET":
        news = my_news_queryset(request.user).get(pk=news_id)
        return JsonResponse(serialize_news(news))

    data = json_body(request)
    news = save_news(
        actor=request.user,
        news_id=news_id,
        expected_version=data.get("expected_version"),
        title=data.get("title"),
        body=data.get("body"),
    )
    return JsonResponse(serialize_news(news))


@endpoint("POST")
@audit_rejections("news.submit.rejected")
def api_submit(request, news_id):
    require_student(request.user)
    data = {}
    if request.body:
        try:
            data = json_body(request)
        except DomainError:
            data = {}
    news, revision = submit_news(
        actor=request.user,
        news_id=news_id,
        request_key=data.get("request_key"),
    )
    payload = serialize_news(news)
    payload["revision"] = {"number": revision.number, "id": str(revision.pk)}
    return JsonResponse(payload)


@endpoint("POST", "DELETE")
@audit_rejections("news.document.delete.rejected")
def api_delete_document(request, news_id):
    """删除本人新闻稿已上传的 Word 文件（提交后仍可删除重传）。"""
    require_student(request.user)
    news = my_news_queryset(request.user).get(pk=news_id)
    document = current_document(news.submission)
    if document is None:
        raise DomainError("当前没有可删除的新闻稿文件。")
    delete_uploaded_file(actor=request.user, upload_id=document.pk)
    news.refresh_from_db()
    return JsonResponse({"deleted_upload_id": str(document.pk), "news": serialize_news(news)})


# =====================================================================
# 学生页面：/news/
# =====================================================================

@login_required
@require_GET
def news_list_view(request):
    require_student(request.user)
    items = list(my_news_queryset(request.user).order_by("-updated_at"))
    rows = []
    for news in items:
        payload = serialize_news(news)
        payload["edit_url"] = reverse("news-edit-page") + f"?activity_id={news.submission.activity_id}"
        rows.append(payload)
    context = {
        "rows": rows,
        "edit_url": reverse("news-edit-page"),
        "workbench_url": reverse("dashboard"),
    }
    return render(request, "news/list.html", context)


@login_required
@require_GET
def news_edit_view(request):
    require_student(request.user)
    activities = list(eligible_activities(request.user).order_by("-created_at")[:50])
    activity_id = str(request.GET.get("activity_id") or "").strip()
    if not activity_id and activities:
        activity_id = str(activities[0].pk)

    news = None
    submission = None
    error_message = ""
    if activity_id:
        submission = Submission.objects.filter(owner=request.user, activity_id=activity_id).first()
        if submission is not None:
            try:
                news = get_or_create_news(actor=request.user, submission_id=submission.pk)
            except DomainError as exc:
                error_message = str(exc)

    document = current_document(submission) if submission is not None else None
    requirement_id = ""
    if submission is not None:
        requirement_id = str(ensure_standard_material_requirements(submission.activity)["news_document"].pk)
    context = {
        "activities": activities,
        "activity_id": activity_id,
        "submission": submission,
        "news": news,
        "payload_json": json.dumps(serialize_news(news) if news else None, ensure_ascii=False),
        "document": document,
        "document_view_url": f"/api/v1/uploads/{document.pk}/file/?inline=1" if document else "",
        "document_download_url": f"/api/v1/uploads/{document.pk}/file/" if document else "",
        "document_delete_url": f"/api/v1/news/{news.pk}/document/" if (document and news) else "",
        "requirement_id": requirement_id,
        # 供编辑页的 FilePond 拖拽区复用：与申报向导 Step 3 的 card config 同一字段口径。
        "news_material_json": {
            "code": "news_document",
            "title": "新闻稿",
            "required": False,
            "allowed_extensions": [".docx"],
            "max_file_mb": 20,
            "max_count": 1,
            "requirement_id": requirement_id,
        },
        "error_message": error_message,
        "save_url": reverse("news-save-page", args=[news.pk]) if news else "",
        "submit_url": reverse("news-submit-page", args=[news.pk]) if news else "",
        # 复用现有直传通道：新闻稿只是第 6 类标准材料（news_document，.docx only）。
        "direct_upload_url": "/api/v1/uploads/direct-upload/",
        "material_type": "news_document",
        "list_url": reverse("news-list-page"),
        "workbench_url": reverse("dashboard"),
    }
    return render(request, "news/edit.html", context)


@login_required
@require_POST
@audit_rejections("news.save.rejected")
def news_save_page_view(request, news_id):
    require_student(request.user)
    try:
        news = save_news(
            actor=request.user,
            news_id=news_id,
            expected_version=request.POST.get("expected_version"),
            title=request.POST.get("title"),
            body=request.POST.get("body"),
        )
        messages.success(request, "新闻稿草稿已保存。")
        return redirect(reverse("news-edit-page") + f"?activity_id={news.submission.activity_id}")
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))


@login_required
@require_POST
@audit_rejections("news.submit.rejected")
def news_submit_page_view(request, news_id):
    require_student(request.user)
    try:
        news, _ = submit_news(actor=request.user, news_id=news_id)
        messages.success(request, f"新闻稿已提交（第 {news.version} 版），等待团支书初审。")
        return redirect(reverse("news-edit-page") + f"?activity_id={news.submission.activity_id}")
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))


# =====================================================================
# 独立审核页：/reviews/news/
# =====================================================================

@login_required
@require_GET
def news_review_view(request):
    if not (is_college_admin(request.user) or news_belongs_to_reviewer_role(request.user)):
        from django.http import HttpResponseForbidden

        return HttpResponseForbidden("仅审核老师或学院管理员可访问新闻稿审核。")

    activity_id = str(request.GET.get("activity_id") or "").strip()
    class_name = str(request.GET.get("class_name") or "").strip()
    status = str(request.GET.get("status") or "").strip()

    queue = review_queue_queryset(
        request.user, activity_id=activity_id, class_name=class_name, status=status
    )[:200]
    rows = []
    for news in queue:
        payload = serialize_news(news)
        payload["student_name"] = news.submission.owner.real_name or news.submission.owner.username
        payload["student_number"] = news.submission.owner.username
        payload["class_name"] = news.submission.owner.class_name
        payload["activity_title"] = news.submission.activity.title
        payload["submission_status"] = news.submission.get_status_display()
        payload["stale_review"] = (
            news.reviewed_version is not None and news.reviewed_version < news.version
        )
        rows.append(payload)

    context = {
        "rows": rows,
        "activities": review_queue_activities(request.user),
        "activity_id": activity_id,
        "class_name": class_name,
        "status": status,
        "status_options": [
            (NewsDraft.Status.SUBMITTED, "待初审"),
            (NewsDraft.Status.REVIEWING, "待终审"),
            (NewsDraft.Status.APPROVED, "已采纳"),
            (NewsDraft.Status.RETURNED, "退回修改"),
        ],
        "is_admin": is_college_admin(request.user),
        "action_url_name": "news-review-action",
        "workbench_url": reverse("dashboard"),
    }
    return render(request, "news/review.html", context)


def news_belongs_to_reviewer_role(user) -> bool:
    from apps.accounts.models import User

    return bool(
        getattr(user, "is_authenticated", False)
        and getattr(user, "is_active", False)
        and getattr(user, "role", "") == User.Role.REVIEWER
    )


@login_required
@require_POST
@audit_rejections("news.review.rejected")
def news_review_action_view(request, news_id):
    try:
        news = review_news(
            actor=request.user,
            news_id=news_id,
            decision=request.POST.get("decision"),
            comment=request.POST.get("comment", ""),
        )
        messages.success(request, f"新闻稿处理完成，当前状态：{news.get_status_display()}。")
        return redirect(reverse("news-review-page"))
    except DomainError as exc:
        messages.error(request, str(exc))
        return HttpResponseBadRequest(str(exc))
