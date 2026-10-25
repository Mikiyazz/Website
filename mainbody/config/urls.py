from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path
from django.views.generic import TemplateView
from apps.common.health import live, ready

urlpatterns = [
    path("health/live/", live),
    path("health/ready/", ready),
    path("admin/", admin.site.urls),
    path("accounts/login/", LoginView.as_view(template_name="registration/login.html"), name="login"),
    path("accounts/logout/", LogoutView.as_view(), name="logout"),
    path("api/v1/activities/", include("apps.activities.urls")),
    path("api/v1/submissions/", include("apps.submissions.urls")),
    path("api/v1/uploads/", include("apps.uploads.urls")),
    path("", login_required(TemplateView.as_view(template_name="dashboard.html")), name="dashboard"),
]
if settings.DEBUG:
    urlpatterns += [path("dev/architecture/", TemplateView.as_view(template_name="architecture.html"))]
