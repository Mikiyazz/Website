"""Explicit allow-list. Never deserialize an arbitrary dotted function from a message."""
QUEUES = {"system.probe": "maintenance", "files.validate": "validation", "exports.build": "exports"}


def handle_event(event):
    if event.topic == "system.probe":
        return
    # Real parsers/exporters need their own durable work lease and resource sandbox.
    # They must not perform long I/O inside the short consumer transaction.
    raise NotImplementedError(f"Handler not implemented: {event.topic}")
