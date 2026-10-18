"""业务错误：携带 HTTP 状态码，由 main.py 统一转成 {"error": ...} 响应（保持旧 API 契约）。"""


class ApiError(Exception):
    def __init__(self, status_code: int, message: str, extra: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.extra = extra or {}
