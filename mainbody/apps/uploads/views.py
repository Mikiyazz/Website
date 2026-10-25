from apps.accounts.permissions import require_student
from apps.common.api import endpoint
from integrations.storage import get_storage


@endpoint("POST")
def initialize(request):
    require_student(request.user)
    get_storage().ensure_available()
    # Keep the route fail-closed even after adding a provider: the orchestration
    # (reservation -> remote init -> compensation) still needs implementation.
    from apps.common.errors import FeatureUnavailable
    raise FeatureUnavailable("分片上传业务编排尚未实现。")
