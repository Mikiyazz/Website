class DomainError(Exception):
    code = "invalid_request"
    status = 400


class Forbidden(DomainError):
    code, status = "forbidden", 403


class Conflict(DomainError):
    code, status = "conflict", 409


class QuotaExceeded(DomainError):
    code, status = "quota_exceeded", 409


class FeatureUnavailable(DomainError):
    code, status = "not_implemented", 501
