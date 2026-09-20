from app.errors import bad_request


def get_actor(x_user_id: str | None) -> str:
    if not x_user_id or not x_user_id.strip():
        raise bad_request("missing_actor", "X-User-Id header is required")
    return x_user_id.strip()
