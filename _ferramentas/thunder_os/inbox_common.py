"""Provider-neutral inbox validation. No transport, environment or persistence."""
class InboxError(ValueError):
    pass

def identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise InboxError("invalid_identifier")
    return value
