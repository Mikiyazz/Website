from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from apps.common.errors import FeatureUnavailable


class DisabledStorage:
    def ensure_available(self):
        raise FeatureUnavailable("对象存储适配器尚未接入，当前不能上传文件。")


def get_storage():
    if settings.STORAGE_BACKEND == "disabled":
        return DisabledStorage()
    raise ImproperlyConfigured("Unknown STORAGE_BACKEND: implement and register the adapter first.")
