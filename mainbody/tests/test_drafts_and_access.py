import pytest
from django.test import Client
from apps.common.errors import Conflict
from apps.submissions.models import Submission
from apps.submissions.services import create_draft, save_draft
from apps.reviews.models import ReviewAssignment
from apps.reviews.selectors import reviewable_submissions
from apps.accounts.models import User


def test_draft_creation_is_idempotent(student, activity):
    first = create_draft(actor=student, activity_id=activity.pk)
    second = create_draft(actor=student, activity_id=activity.pk)
    assert first.pk == second.pk
    assert Submission.objects.count() == 1


def test_stale_draft_cannot_overwrite_new_data(student, activity):
    draft = create_draft(actor=student, activity_id=activity.pk)
    save_draft(actor=student, submission_id=draft.pk, expected_version=0, data={"title": "first"})
    with pytest.raises(Conflict):
        save_draft(actor=student, submission_id=draft.pk, expected_version=0, data={"title": "stale"})
    draft.refresh_from_db()
    assert draft.draft_data == {"title": "first"}
    assert draft.version == 1


def test_cross_student_access_denied(client, student, other_student, activity):
    draft = create_draft(actor=student, activity_id=activity.pk)
    client.force_login(other_student)
    assert client.get(f"/api/v1/submissions/{draft.pk}/").status_code == 404
    response = client.patch(f"/api/v1/submissions/{draft.pk}/", {"expected_version": 0, "draft_data": {}}, content_type="application/json")
    assert response.status_code == 404


def test_reviewer_scope_is_enforced(student, other_student, activity):
    a = create_draft(actor=student, activity_id=activity.pk)
    create_draft(actor=other_student, activity_id=activity.pk)
    reviewer = User.objects.create_user(username="reviewer", role=User.Role.REVIEWER)
    ReviewAssignment.objects.create(reviewer=reviewer, activity=activity, class_name=student.class_name)
    assert list(reviewable_submissions(reviewer, activity.pk)) == [a]
    assert not reviewable_submissions(student, activity.pk).exists()


def test_csrf_remains_enabled(student, activity):
    client = Client(enforce_csrf_checks=True)
    client.force_login(student)
    response = client.post("/api/v1/submissions/", {"activity_id": str(activity.pk)}, content_type="application/json")
    assert response.status_code == 403


def test_unimplemented_routes_do_not_report_success(client, student, activity):
    client.force_login(student)
    draft = create_draft(actor=student, activity_id=activity.pk)
    assert client.post("/api/v1/uploads/init/", {}, content_type="application/json").status_code == 501
    assert client.post(f"/api/v1/submissions/{draft.pk}/submit/", {}, content_type="application/json").status_code == 501


def test_anonymous_api_is_unauthorized(client):
    assert client.get("/api/v1/submissions/").status_code == 401


def test_basic_pages_render(client):
    assert client.get("/accounts/login/").status_code == 200
    assert client.get("/health/live/").json() == {"status": "alive"}
