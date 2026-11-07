from django.conf import settings
from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path
from django.views.generic import TemplateView
from apps.accounts import views as account_views
from apps.common.health import live, ready

urlpatterns = [
    path("health/live/", live),
    path("health/ready/", ready),
    path("admin/", admin.site.urls),
    path("accounts/login/", LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("accounts/register/", account_views.register_page_view, name="register-page"),
    path("accounts/password-change/", account_views.change_password_page_view, name="password-change-page"),
    path("accounts/change-password/", account_views.change_password_page_view, name="change-password-page"),
    path("student/workbench/", account_views.dashboard_view, name="student-workbench"),
    path("student/submissions/save/", account_views.student_save_draft_view, name="student-save-draft"),
    path("student/submissions/<uuid:submission_id>/submit/", account_views.student_submit_review_view, name="student-submit-review"),
    path("reviews/workbench/", account_views.dashboard_view, name="review-workbench"),
    path("reviews/submissions/<uuid:submission_id>/", account_views.review_submission_detail_view, name="review-submission-detail"),
    path("reviews/submissions/<uuid:submission_id>/action/", account_views.review_submission_action_view, name="review-submission-action"),
    path("admin-panel/", account_views.dashboard_view, name="admin-panel"),
    path("admin-panel/activities/save/", account_views.admin_save_activity_view, name="admin-activity-save"),
    path("admin-panel/activities/<uuid:activity_id>/whitelist/", account_views.admin_whitelist_action_view, name="admin-activity-whitelist"),
    path("api/v1/accounts/", include("apps.accounts.urls")),
    path("api/v1/activities/", include("apps.activities.urls")),
    path("api/v1/submissions/", include("apps.submissions.urls")),
    path("api/v1/reviews/", include("apps.reviews.urls")),
    path("api/v1/uploads/", include("apps.uploads.urls")),
    path("api/v1/exports/", include("apps.exports.urls")),
    path("", account_views.dashboard_view, name="dashboard"),
]
if settings.DEBUG:
    urlpatterns += [path("dev/architecture/", TemplateView.as_view(template_name="architecture.html"))]
