import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                "logger": record.name, "message": record.getMessage()}
        for key in ("request_id", "method", "route", "status", "duration_ms", "event_id", "error_type"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        # Exception type is safe to log; exception strings may contain signed URLs or credentials.
        if record.exc_info and record.exc_info[0]:
            data["exception_type"] = record.exc_info[0].__name__
        return json.dumps(data, ensure_ascii=False)
