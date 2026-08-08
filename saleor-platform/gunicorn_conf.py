import os

# --- Server ---
bind = '0.0.0.0:8000'
workers = 2
worker_class = 'uvicorn.workers.UvicornWorker'
keepalive = 35
graceful_timeout = 30
max_requests = 10000
accesslog = None

# --- OTel: monkey-patch AFTER fork, let Saleor's initialize_telemetry() own the TracerProvider ---
#
# Chuỗi sự kiện trong mỗi worker process:
#   [1] post_fork() chạy → chỉ monkey-patch thư viện (KHÔNG set TracerProvider)
#   [2] Worker load Django ASGI app → Saleor initialize_telemetry() chạy
#           → _OTelSDKConfigurator.configure() đọc env vars (OTEL_TRACES_EXPORTER,
#             OTEL_EXPORTER_OTLP_ENDPOINT...) → gọi set_tracer_provider() DUY NHẤT
#
# Tại sao monkey-patch PHẢI xảy ra trong post_fork() (TRƯỚC khi Django load)?
#   - PsycopgInstrumentor().instrument() phải wrap hàm psycopg.execute TRƯỚC khi
#     Django tạo database connections. Nếu instrument() chạy SAU khi connections
#     đã được tạo, các connection cũ sẽ không được trace.
#   - Các instrumentor dùng trace.get_tracer() tại THỜI ĐIỂM REQUEST (lazy lookup),
#     nên TracerProvider chưa cần có mặt lúc instrument() được gọi.
#
# Kết quả: không còn warning "Overriding of current TracerProvider is not allowed"
#          vì TracerProvider chỉ được set 1 lần duy nhất (bởi Saleor).
def post_fork(server, worker):
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
    from opentelemetry.instrumentation.celery import CeleryInstrumentor

    PsycopgInstrumentor().instrument()
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    URLLib3Instrumentor().instrument()
    CeleryInstrumentor().instrument()

    server.log.info(
        'Worker %s: OTel libraries instrumented (TracerProvider will be set by Saleor)',
        worker.pid,
    )
