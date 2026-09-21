from rest_framework.views import exception_handler as drf_exception_handler


def revstream_exception_handler(exc, context):
    """Normalize DRF error payloads.

    Field validation errors keep their detail structure under ``errors``;
    a flat ``message`` is always present for clients that only display text.
    """
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    detail = response.data
    if isinstance(detail, dict) and "detail" in detail and len(detail) == 1:
        message = str(detail["detail"])
        errors = None
    else:
        message = "Request validation failed"
        errors = detail

    response.data = {
        "message": message,
        "errors": errors,
    }
    return response
