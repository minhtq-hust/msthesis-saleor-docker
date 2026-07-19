import os

# --- Server ---
bind = '0.0.0.0:8000'
workers = 2
worker_class = 'uvicorn.workers.UvicornWorker'
keepalive = 35
graceful_timeout = 30
max_requests = 10000
accesslog = None

# --- OTel: init AFTER fork so each worker has its own exporter thread ---
def post_fork(server, worker):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({
        'service.name': os.environ.get('OTEL_SERVICE_NAME', 'saleor'),
    })

    endpoint = os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT', 'http://otel-collector:4318')
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint + '/v1/traces'))
    )
    trace.set_tracer_provider(provider)

    # Instrument the same libraries as before
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

    server.log.info('Worker %s: OTel initialized', worker.pid)
