from django.urls import path
from .views import initialize

urlpatterns = [path("init/", initialize, name="upload-init")]
