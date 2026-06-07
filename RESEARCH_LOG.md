## [2026-04-28] - Experiment: Đổi tên nhánh master thành main
- **Hypothesis:** Có thể đổi tên nhánh chính từ master sang main và đồng bộ với remote GitHub.
- **Technical Implementation:** Sử dụng `git branch -m main`, sau đó `git push -u origin main` và xóa nhánh master trên remote.
- **Outcome:** SUCCESS
- **AI Analysis:** Việc đổi tên nhánh thành công sau khi xử lý xung đột lịch sử phân nhánh bằng rebase. Đây là thao tác chuẩn để đồng bộ hóa nhánh chính với quy chuẩn hiện đại.
- **Key Lesson:** Khi đổi tên nhánh chính, cần chú ý lịch sử phân nhánh giữa local và remote để tránh lỗi push bị từ chối.

## [2026-05-18] - Tái cấu trúc kiến trúc Monitoring Stack

- **Hypothesis:** Tách riêng tầng observability (monitoring/tracing) khỏi tầng ứng dụng (business logic) giúp quản lý, vận hành và trình bày kiến trúc rõ ràng hơn. Đồng thời, thêm OpenTelemetry Collector để chuyển đổi traces thành Prometheus metrics (RED: Rate, Error, Duration) cho phép giám sát response time trên Grafana.

- **Technical Implementation:**

  ### Kiến trúc TRƯỚC:
  ```
  saleor-platform:    api, db, redis, worker, dashboard, jaeger, mailpit
  saleor-monitoring:  grafana, prometheus, cadvisor
  ```
  - Jaeger nằm trong saleor-platform (lẫn observability với business logic)
  - Grafana chỉ có metrics tài nguyên (CPU/RAM/Network/Disk) từ cAdvisor
  - Response time chỉ xem được trên Jaeger UI, không có trên Grafana

  ### Kiến trúc SAU:
  ```
  saleor-platform:    api, db, redis, worker, dashboard, mailpit
  saleor-monitoring:  grafana, prometheus, cadvisor, otel-collector, jaeger
  ```
  - Tất cả observability tập trung trong saleor-monitoring
  - OTel Collector nhận traces từ Saleor API → forward tới Jaeger + tạo RED metrics
  - Prometheus scrape RED metrics từ OTel Collector → Grafana hiển thị response time

  ### Các file thay đổi:
  | File | Thay đổi |
  |------|----------|
  | `saleor-platform/docker-compose.yml` | Xóa service jaeger, xóa `depends_on: jaeger` trong api |
  | `saleor-platform/backend.env` | `OTEL_EXPORTER_OTLP_ENDPOINT` → `http://monitoring-otel-collector:4317` |
  | `saleor-monitoring/docker-compose.yml` | Thêm service jaeger + otel-collector |
  | `saleor-monitoring/otelcol/config.yaml` | Tạo mới — cấu hình spanmetrics connector |
  | `saleor-monitoring/prometheus/prometheus.yml` | Thêm scrape target `otel-collector:8889` |

  ### Pipeline dữ liệu mới:
  ```
  Saleor API ──(OTLP)──► OTel Collector ──┬──► Jaeger (traces UI)
                                           └──► Prometheus ──► Grafana (RED metrics)
  cAdvisor ──────────────────────────────────► Prometheus ──► Grafana (resource metrics)
  ```

  ### Metrics mới có sẵn trên Grafana (từ OTel Collector):
  - `otel_duration_milliseconds_*` — histogram response time
  - `otel_calls_total` — counter số request
  - Hỗ trợ tính P50, P95, P99 latency và request rate

- **Outcome:** PENDING (cần deploy và kiểm tra)

- **AI Analysis:** Việc tách observability stack ra riêng tuân theo nguyên tắc Separation of Concerns. OpenTelemetry Collector với spanmetrics connector là giải pháp chuẩn công nghiệp để chuyển đổi distributed traces thành aggregated metrics (RED), cho phép giám sát hiệu năng ứng dụng trên Grafana dashboard mà không cần thay đổi code ứng dụng. Trade-off duy nhất là mất `depends_on` cross-compose, nhưng OpenTelemetry SDK đã được thiết kế fault-tolerant nên không ảnh hưởng đến hoạt động của ứng dụng.

- **Key Lessons:**
  1. cAdvisor chỉ đo tài nguyên hệ thống (CPU/RAM/Network/Disk), không đo được response time — đó là metric tầng ứng dụng
  2. OpenTelemetry Collector đóng vai trò "cầu nối" giữa traces (Jaeger) và metrics (Prometheus)
  3. Cross-compose networking dùng container name (không phải service name) để resolve DNS
  4. Prometheus container chạy `user: root` để đọc được config file có permission hạn chế trên host

## [2026-05-21] - Xử lý sự cố treo API do OpenTelemetry Collector

- **Sự cố:** Saleor API bị treo hoàn toàn (không phản hồi, không có error log) ngay cả khi các container đều trạng thái "healthy". Cùng lúc, container `otel-collector` liên tục bị restart.
- **Phân tích nguyên nhân (Root Causes):**
  1. **Treo API:** OpenTelemetry SDK trong API cố gắng gửi traces tới endpoint `otel-collector:4317`. Do collector không hoạt động, thư viện tracing block các request threads để chờ kết nối (hoặc retry ngầm), khiến toàn bộ API bị kẹt mà không sinh ra log.
  2. **Crash OTel Collector:** File cấu hình `otelcol/config.yaml` khai báo tường minh các `dimensions` (như `service.name`, `span.name`) trong connector `spanmetrics`. Tuy nhiên, với phiên bản mới của Collector (0.152.1), các chiều dữ liệu này đã được tự động chèn mặc định. Việc khai báo lại gây ra lỗi validation `duplicate dimension name`.
  3. **Pull image liên tục:** Việc dùng tag `:latest` khiến Docker Engine luôn kiểm tra và pull lại image ở mỗi lần khởi tạo container.

- **Technical Implementation (Giải pháp):**
  1. **Dịch chuyển kiến trúc (Co-location):** Chuyển service `otel-collector` từ stack `saleor-monitoring` sang `saleor-platform`. Điều này đảm bảo Collector khởi động cùng lúc và chung lifecycle với API, loại bỏ rủi ro API thiếu endpoint gửi traces.
  2. **Sửa lỗi config:** Xóa block khai báo `dimensions` trùng lặp trong cấu hình `spanmetrics`.
  3. **Cố định phiên bản (Pinning):** Thay đổi image tag từ `:latest` sang version cố định `:0.152.1` để ngăn Docker re-pull.
  4. **Kết nối Cross-stack:** Cập nhật lại DNS routing: 
     - Collector (ở platform) đẩy traces tới `monitoring-jaeger` thông qua shared network.
     - Prometheus (ở monitoring) scrape metrics từ `platform-otel-collector`.

- **Outcome:** SUCCESS. Hệ thống API hoạt động bình thường, OTel Collector khởi động ổn định không bị crash, traces/metrics được luân chuyển xuyên stack chính xác.
- **Key Lessons:**
  1. Các service phụ trợ có ảnh hưởng trực tiếp tới blocking của request thread (như OpenTelemetry Exporter) nên được đặt **cùng stack (co-located)** với API thay vì tách rời sang stack monitoring thuần túy.
  2. Khi thư viện hoặc ứng dụng không in ra log lỗi mà chỉ "im lặng và treo", nguyên nhân thường nằm ở việc blocking I/O (như chờ network call tới một service không phản hồi).
  3. Tránh sử dụng tag `:latest` trong môi trường triển khai thực tế để đảm bảo tính dự đoán (predictability) và tránh lỗi phát sinh từ cập nhật phiên bản ngầm định.

## [2026-05-22] - Sự cố Docker Networking toàn hệ thống & Triển khai Portainer

- **Sự cố:** Không thể truy cập Portainer UI sau khi triển khai bằng `docker run`. Sau đó phát hiện không chỉ Portainer mà **toàn bộ container** (bao gồm Saleor Dashboard, API) đều không phản hồi HTTP — mặc dù TCP connect thành công và container process vẫn report "listening".

- **Phân tích nguyên nhân (Root Causes):**
  1. **Docker daemon networking bị hỏng:** Tầng mạng của Docker daemon bị corrupt sau thời gian hoạt động dài. TCP connections tới containers thành công (SYN-ACK) nhưng server không bao giờ gửi HTTP response (0 bytes received). Kiểm tra `/proc/PID/net/tcp6` trong container xác nhận process đang listen, nhưng traffic không được deliver tới application layer.
  2. **Port conflict:** Portainer tunnel server (mặc định port 8000) xung đột với Saleor API (cũng port 8000).

- **Technical Implementation (Giải pháp):**
  1. **Restart Docker daemon:** `sudo systemctl restart docker` — khắc phục hoàn toàn vấn đề networking cho tất cả containers.
  2. **Portainer standalone:** Triển khai Portainer bằng `docker run` độc lập (không thuộc compose nào), với `--tunnel-port 8099` để tránh xung đột port 8000, HTTP map tới port `9002` để tránh xung đột với Dashboard port `9000`.

- **Outcome:** SUCCESS. Tất cả services phục hồi sau restart daemon. Portainer hoạt động ổn định trên `localhost:9002`.
- **Key Lessons:**
  1. Khi **nhiều container cùng lúc** không phản hồi (không chỉ container mới), nguyên nhân gần như chắc chắn nằm ở Docker daemon chứ không phải ở ứng dụng — restart daemon là bước đầu tiên nên thử.
  2. HTTP status `000` từ curl (TCP connect OK nhưng 0 bytes nhận được) là dấu hiệu đặc trưng của Docker networking layer bị corrupt.
  3. Luôn kiểm tra port conflict khi triển khai service mới (Portainer tunnel port 8000 vs Saleor API port 8000).

## [2026-05-22] - Tổ chức Infrastructure & Dashboard Observability

- **Hypothesis:** Tách các service hạ tầng (Portainer, Nginx proxy) ra khỏi compose stack, tổ chức vào folder `infra/` riêng, giúp quản lý rõ ràng hơn. Đồng thời tạo Grafana dashboard phản ánh đúng "Freeze Line" — chỉ giám sát những gì cần thiết cho thesis.

- **Technical Implementation:**

  ### Cấu trúc mới:
  ```
  SaleorPlatform_Docker/
  ├── saleor-platform/      # Business logic (compose)
  ├── saleor-monitoring/     # Observability (compose)
  └── infra/                 # Infrastructure — standalone containers
      ├── nginx/default.conf   # Reverse proxy config
      ├── run-nginx.sh         # docker run nginx (port 80)
      └── run-portainer.sh     # docker run portainer (port 9002)
  ```

  ### Nginx Reverse Proxy — truy cập không cần port:
  | URL | Service | Port gốc |
  |-----|---------|-----------|
  | `grf.slr.rwx.dev` | Grafana | :3000 |
  | `prom.slr.rwx.dev` | Prometheus | :9090 |
  | `jgr.slr.rwx.dev` | Jaeger | :16686 |
  | `ptn.slr.rwx.dev` | Portainer | :9002 |
  | `cad.slr.rwx.dev` | cAdvisor | :8080 |

  Nginx container dùng `--add-host="host.docker.internal:host-gateway"` để resolve host ports từ bên trong container. Các domain được map trong `/etc/hosts` tới `127.0.0.1`.

  ### Dashboard "Saleor Observability":
  File: `saleor-monitoring/grafana/saleor-observability-dashboard.json`

  Bao gồm 4 row sections:
  1. **Summary** — Running containers, Total Memory, Total CPU, API P95 (stat panels)
  2. **API Performance RED** — Request Rate, Error Rate, P50/P95/P99 Latency (từ spanmetrics)
  3. **API Latency by Endpoint** — Top slow spans, Request Rate by span_name
  4. **Container Resources** — CPU, Memory, Network Rx/Tx per container (từ cAdvisor)

  Dùng Grafana template variable `${DS_PROMETHEUS}` (type: datasource) thay vì hardcode UID — portable và dễ quản lý hơn.

  ### Đánh giá observer theo Freeze Line:
  | Observer | Trạng thái | Lý do |
  |----------|-----------|-------|
  | cAdvisor | ✅ Giữ | Container CPU/RAM/Network/Disk |
  | OTel spanmetrics | ✅ Giữ | API P50/P95/P99, RED metrics |
  | Jaeger | ✅ Giữ | Drill-down traces |
  | postgres_exporter | ❌ Không cần | `pg_stat_statements` + `EXPLAIN ANALYZE` đủ |
  | redis_exporter | ❌ Không cần | `Redis INFO` command đủ |
  | celery-exporter | ❌ Không cần | Celery không ảnh hưởng P95 API |

  ### Các file thay đổi:
  | File | Thay đổi |
  |------|----------|
  | `saleor-monitoring/docker-compose.yml` | Xóa service nginx (chuyển sang infra/) |
  | `saleor-monitoring/grafana/saleor-observability-dashboard.json` | Tạo mới — dashboard theo Freeze Line |
  | `infra/nginx/default.conf` | Tạo mới — reverse proxy cho *.slr.rwx.dev |
  | `infra/run-nginx.sh` | Tạo mới — standalone nginx script |
  | `infra/run-portainer.sh` | Tạo mới — standalone portainer script |
  | `/etc/hosts` | Thêm `ptn.slr.rwx.dev`, `cad.slr.rwx.dev` |

- **Outcome:** SUCCESS. Tất cả proxy endpoints hoạt động. Dashboard JSON sẵn sàng import.
- **Key Lessons:**
  1. Dashboard JSON dùng biến `${DS_PROMETHEUS}` cần khai báo trong `templating.list` với `"type": "datasource"` — nếu thiếu, tất cả panels sẽ báo "No data".
  2. Nginx container trên Linux cần `--add-host="host.docker.internal:host-gateway"` để proxy_pass tới host ports (khác macOS, Docker Desktop Linux đã tự thêm).
  3. Không cần thêm exporter nếu đã có cách lấy thông tin tương đương (pg_stat_statements thay cho postgres_exporter, Redis INFO thay cho redis_exporter) — đúng nguyên tắc "đủ dùng".

## [2026-05-22] - Fix: Domain `.dev` bị HSTS preload trong Firefox

- **Sự cố:** Truy cập `http://grf.slr.rwx.dev` từ Firefox báo "can't connect to the server", mặc dù curl từ terminal hoạt động bình thường. Đã tắt DNS-over-HTTPS (`network.trr.mode=5`), xóa DNS cache → vẫn không được.

- **Phân tích nguyên nhân:**
  - `.dev` là TLD thật do Google sở hữu, nằm trong **HSTS preload list** của tất cả browser hiện đại (Firefox, Chrome, Edge).
  - HSTS preload **bắt buộc** browser nâng cấp mọi request `.dev` lên HTTPS (port 443), bất kể có gõ `http://` hay không.
  - Nginx reverse proxy chỉ listen trên port 80 (HTTP) → Firefox cố kết nối port 443 → "Connection refused" → "can't connect".
  - `curl` hoạt động vì nó không áp dụng HSTS preload list — nó truy cập đúng port 80 HTTP như được yêu cầu.

- **Technical Implementation:**
  - Đổi tất cả domain từ `*.slr.rwx.dev` sang `*.saleor.local`
  - TLD `.local` không nằm trong HSTS preload list → Firefox truy cập HTTP port 80 bình thường

  ### Domain mapping mới:
  | URL | Service | Port gốc |
  |-----|---------|-----------|
  | `grf.saleor.local` | Grafana | :3000 |
  | `prom.saleor.local` | Prometheus | :9090 |
  | `jgr.saleor.local` | Jaeger | :16686 |
  | `ptn.saleor.local` | Portainer | :9002 |
  | `cad.saleor.local` | cAdvisor | :8080 |

  ### Các file thay đổi:
  | File | Thay đổi |
  |------|----------|
  | `infra/nginx/default.conf` | `server_name` đổi từ `*.slr.rwx.dev` → `*.saleor.local` |
  | `infra/run-nginx.sh` | Cập nhật comment và echo |
  | `infra/run-portainer.sh` | Cập nhật comment và echo |
  | `/etc/hosts` | Thêm dòng `127.0.0.1 grf.saleor.local prom.saleor.local ...` |

- **Outcome:** SUCCESS. Tất cả endpoint truy cập được từ Firefox mà không cần nhớ port.
- **Key Lessons:**
  1. **Không bao giờ dùng `.dev` cho local development** — nó là TLD thật và bị HSTS preload. Các TLD an toàn cho local: `.local`, `.test`, `.localhost`, `.internal`.
  2. Khi `curl` hoạt động nhưng browser thì không, nghĩ đến: HSTS, CORS, hoặc browser security policies — không phải DNS hay network.
  3. HSTS preload là danh sách **hardcode trong browser binary**, không thể tắt bằng setting hay xóa cache.

## [2026-05-22] - Import Dashboard "Saleor Observability" vào Grafana

- **Thực hiện:** Import dashboard JSON vào Grafana qua API.

  ```bash
  # Lệnh import (Basic Auth: admin / **** → base64)
  curl -s -X POST 'http://localhost:3000/api/dashboards/db' \
    -H 'Content-Type: application/json' \
    -H 'Authorization: Basic YWRtaW46UXVAbmdtIW5oMTk5OA==' \
    -d @saleor-monitoring/grafana/saleor-observability-dashboard.json
  ```

  Kết quả:
  ```json
  {
    "status": "success",
    "uid": "saleor-observability",
    "url": "/d/saleor-observability/saleor-observability"
  }
  ```

- **Truy cập:** `http://grf.saleor.local/d/saleor-observability/saleor-observability`
- **Lưu ý:** Nếu cần re-import sau khi sửa JSON, dùng lại lệnh trên (có `"overwrite": true` trong JSON).

  ### Dashboard "Docker monitoring" (cũ):
  - Vẫn giữ nguyên, nhưng cần vào **Settings → Variables** → thêm variable `DS_PROMETHEUS` (type: `Data source`, query: `prometheus`) để các panel hiển thị dữ liệu thay vì "No data".

## [2026-06-07] - Bật Full-stack Distributed Tracing cho toàn bộ hệ thống Saleor

- **Hypothesis:** Có thể cấu hình distributed tracing cho tất cả cấu phần của hệ thống Saleor (API, Celery Worker, PostgreSQL, Redis) để theo dõi chi tiết luồng xử lý request từ đầu đến cuối, xác định bottleneck ở từng tầng.

- **Technical Implementation:**

  ### Kiến thức nền: Distributed Tracing là gì?

  Distributed Tracing là kỹ thuật theo dõi một request xuyên suốt nhiều thành phần (service, database, cache, queue) trong hệ thống phân tán. Mỗi request được gắn một **Trace ID** duy nhất, và mỗi bước xử lý bên trong tạo ra một **Span** — chứa thông tin: tên thao tác, thời gian bắt đầu, thời gian kết thúc (duration), và quan hệ cha-con với các span khác.

  **Ví dụ cụ thể:** Khi người dùng mở trang sản phẩm trên storefront:
  ```
  Trace ID: abc123
  ├─ Span 1: HTTP POST /graphql/ (40ms)           ← Saleor API nhận request
  │   ├─ Span 2: GraphQL query productBySlug (38ms) ← Parse và thực thi GraphQL
  │   │   ├─ Span 3: SELECT product... (5ms)         ← Truy vấn PostgreSQL
  │   │   ├─ Span 4: Redis GET cache_key (0.5ms)     ← Kiểm tra cache
  │   │   └─ Span 5: SELECT media... (3ms)            ← Truy vấn PostgreSQL lần 2
  │   └─ Span 6: Serialize response (2ms)            ← Chuyển đổi kết quả
  ```

  Nhờ chuỗi spans này, ta biết chính xác **request mất 40ms tổng cộng, trong đó SQL chiếm 8ms, Redis chiếm 0.5ms**, thay vì chỉ biết "response time = 40ms" mà không rõ đoạn nào chậm.

  ### Kiến thức nền: OpenTelemetry (OTel) hoạt động thế nào?

  OpenTelemetry là bộ công cụ chuẩn công nghiệp để thu thập telemetry data (traces, metrics, logs). Trong hệ thống Saleor, OTel hoạt động theo mô hình sau:

  ```
  ┌─────────────────── Trong container ứng dụng ───────────────────┐
  │                                                                 │
  │  [OTel SDK]                                                     │
  │  ├─ TracerProvider: quản lý việc tạo và xuất traces             │
  │  ├─ SpanProcessor: buffer spans rồi gửi theo batch              │
  │  └─ SpanExporter: gửi spans tới collector qua gRPC hoặc HTTP   │
  │                                                                 │
  │  [Auto-Instrumentation]                                         │
  │  ├─ psycopg: monkey-patch thư viện PostgreSQL driver            │
  │  │   → mỗi lần gọi cursor.execute(sql), tự tạo 1 span         │
  │  ├─ redis: monkey-patch thư viện Redis client                   │
  │  │   → mỗi lần gọi redis.get(key), tự tạo 1 span              │
  │  └─ celery: hook vào signal system của Celery                   │
  │      → mỗi lần submit/execute task, tự tạo span                │
  │                                                                 │
  └─────────────────────────────┬───────────────────────────────────┘
                                │ gửi spans (OTLP gRPC hoặc HTTP)
                                ▼
  ┌─────────────── OTel Collector (platform-otel-collector) ────────┐
  │  receivers:  nhận traces từ API và Worker                       │
  │  connectors: spanmetrics — chuyển traces thành Prometheus metrics│
  │  exporters:  gửi traces tới Jaeger + metrics tới Prometheus     │
  └─────────────────────────────────────────────────────────────────┘
  ```

  **Auto-Instrumentation** (tự động đo lường) hoạt động bằng cách **monkey-patching** — tức là thay thế các hàm gốc của thư viện bằng phiên bản có thêm logic tạo span. Ví dụ: hàm `cursor.execute("SELECT ...")` của psycopg2 bị thay thế bằng:
  ```python
  # Pseudocode — OTel tự làm việc này
  original_execute = cursor.execute
  def instrumented_execute(sql, params):
      with tracer.start_span("SELECT") as span:
          span.set_attribute("db.statement", sql)
          span.set_attribute("db.system", "postgresql")
          result = original_execute(sql, params)  # gọi hàm gốc
          return result
  cursor.execute = instrumented_execute
  ```

  Lệnh `opentelemetry-instrument` là wrapper CLI tự động:
  1. Khởi tạo TracerProvider với exporter (gRPC/HTTP tới collector)
  2. Tìm tất cả gói `opentelemetry-instrumentation-*` đã cài
  3. Gọi monkey-patch cho từng thư viện
  4. Rồi chạy ứng dụng chính (uvicorn/celery)

  ### Hiện trạng TRƯỚC khi thay đổi

  | Thành phần | Trạng thái tracing | Lý do |
  |---|---|---|
  | Saleor API | ⚠️ Có TracerProvider nhưng thiếu auto-instrumentation | Image có OTel SDK lõi nhưng thiếu gói `opentelemetry-instrumentation-*` |
  | Celery Worker | ❌ Không có tracing | Command `celery ...` không bọc qua `opentelemetry-instrument` |
  | PostgreSQL | ❌ Không có monitoring | `pg_stat_statements` chưa bật, slow query log tắt |
  | Redis | ❌ Không có monitoring | `slowlog` chưa cấu hình |

  ### Thay đổi 1: Cài OTel Auto-Instrumentation cho API

  **Vấn đề:** Image `minhtq2/saleor:1.02` chỉ có OTel SDK lõi (`opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`) nhưng **KHÔNG** có các gói instrumentation cụ thể. Có SDK mà không có instrumentor giống như có máy quay nhưng không có ống kính — SDK biết cách ghi/gửi span, nhưng không có ai tạo span.

  **Giải pháp:** Thêm `command` trong `docker-compose.yml` để cài gói lúc runtime (Phương án A — không cần rebuild image):

  ```yaml
  # saleor-platform/docker-compose.yml — service api
  command:
    - sh
    - -c
    - |
      pip install --quiet --no-cache-dir \
        opentelemetry-instrumentation-psycopg==0.53b1 \
        opentelemetry-instrumentation-redis==0.53b1 \
        opentelemetry-instrumentation-celery==0.53b1 \
        opentelemetry-instrumentation-httpx==0.53b1 \
        opentelemetry-instrumentation-urllib3==0.53b1 && \
      opentelemetry-instrument uvicorn saleor.asgi:application \
        --host=0.0.0.0 --port=8000 --lifespan=off \
        --ws=none --no-server-header --no-access-log \
        --timeout-keep-alive=35 --timeout-graceful-shutdown=30 \
        --limit-max-requests=10000
  ```

  **Tại sao không cài `opentelemetry-instrumentation-django`?** Saleor có module telemetry riêng (`saleor/core/telemetry/__init__.py`) đã tự xử lý tạo spans cho HTTP request, GraphQL query, DataLoader batch. Khi cài thêm gói Django instrumentation từ OTel, nó can thiệp vào Django import path và gây crash:
  ```
  AttributeError: module 'django.conf.global_settings' has no attribute 'TELEMETRY_TRACER_CLASS'
  ```
  Nguyên nhân: gói Django instrumentation load Django module sớm hơn bình thường, khiến Saleor's ASGI module (`saleor/asgi/__init__.py`) chạy `initialize_telemetry()` trước khi Django settings được configure đầy đủ.

  **Tại sao bỏ `--workers=2`?** Uvicorn với `--workers=N` (N>1) tạo N child processes bằng `multiprocessing.spawn` (không phải `fork`). Sự khác biệt cực kỳ quan trọng:

  | Kiểu | Cách hoạt động | Ảnh hưởng OTel |
  |---|---|---|
  | `fork()` | Copy toàn bộ bộ nhớ process cha sang con | Con **kế thừa** TracerProvider đã khởi tạo |
  | `spawn()` | Tạo process con hoàn toàn mới, rồi import lại code | Con **KHÔNG** có TracerProvider → `ProxyTracerProvider` (rỗng, trace_id=0x0) |

  Uvicorn dùng `spawn()` vì lý do an toàn (tránh lỗi khi fork process có nhiều threads). Kết quả: `opentelemetry-instrument` khởi tạo TracerProvider ở parent process, nhưng 2 worker processes con chạy request thực tế lại không có TracerProvider → traces không bao giờ được gửi đi.

  Bỏ `--workers` (mặc định 1 worker) → `opentelemetry-instrument` và ASGI app chạy cùng 1 process → TracerProvider hoạt động đúng. Hiệu năng không bị ảnh hưởng nhiều vì Saleor dùng async (asyncio event loop) nên 1 process vẫn xử lý được nhiều request đồng thời.

  ### Thay đổi 2: Bật Tracing cho Celery Worker

  **Vấn đề:** Worker có biến `OTEL_*` từ `backend.env` nhưng command khởi chạy là `celery -A saleor ...` — **THIẾU** wrapper `opentelemetry-instrument` → OTel SDK không bao giờ được khởi tạo.

  **Giải pháp:**
  ```yaml
  # saleor-platform/docker-compose.yml — service worker
  command:
    - sh
    - -c
    - |
      pip install --quiet --no-cache-dir \
        opentelemetry-instrumentation-celery==0.53b1 \
        opentelemetry-instrumentation-psycopg==0.53b1 \
        opentelemetry-instrumentation-redis==0.53b1 && \
      opentelemetry-instrument celery -A saleor --app=saleor.celeryconf:app worker --loglevel=info -B
  environment:
    - OTEL_SERVICE_NAME=saleor-worker         # Phân biệt với API trên Jaeger
    - OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf  # Dùng HTTP thay vì gRPC
    - OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318  # Port HTTP
    - OTEL_METRICS_EXPORTER=none              # Tắt metrics (chỉ cần traces)
  ```

  **Tại sao Worker dùng HTTP exporter (`http/protobuf`) thay vì gRPC?** Celery dùng `prefork` pool — tạo 8 child processes bằng `fork()`. Vấn đề: gRPC sử dụng **C-level threads** (libgrpc) để quản lý kết nối. Khi `fork()`, C-level threads không được sao chép đúng → kênh gRPC bị hỏng trong child processes:
  ```
  Transient error StatusCode.UNAVAILABLE encountered while exporting traces
  epoll_ctl failed: Invalid argument  ← Lỗi ở tầng C (epoll, kernel)
  ```

  HTTP exporter dùng thư viện `requests` (pure Python) — không có C-level threads → survive qua `fork()` bình thường.

  **Tại sao tắt `OTEL_METRICS_EXPORTER=none`?** `opentelemetry-instrument` mặc định cũng cố gửi metrics (không chỉ traces) tới collector. Nhưng OTel Collector chỉ có pipeline nhận traces (không có metrics receiver) → Worker gửi metrics tới `/v1/metrics` bị trả về 404. Tắt đi vì metrics đã có sẵn từ spanmetrics connector (chuyển traces → Prometheus metrics tự động).

  ### Thay đổi 3: PostgreSQL — Bật `pg_stat_statements` + Slow Query Log

  **Tại sao PostgreSQL không cần OpenTelemetry?** Bản thân tiến trình PostgreSQL không biết gì về OTel — nó chỉ là database engine nhận SQL và trả kết quả. Tracing SQL đã được thực hiện **từ phía client** (API/Worker) qua `opentelemetry-instrumentation-psycopg`: mỗi lần API gọi `cursor.execute(sql)`, psycopg instrumentor tự tạo span ghi nhận câu SQL, thời gian chạy, tên database, port.

  Tuy nhiên, phía PostgreSQL vẫn cần 2 cơ chế bổ sung:

  ```yaml
  # saleor-platform/docker-compose.yml — service db
  command: >
    postgres
      -c shared_preload_libraries=pg_stat_statements
      -c pg_stat_statements.track=all
      -c log_min_duration_statement=500
      -c log_statement=none
      -c log_line_prefix='%t [%p] %d '
  ```

  | Cơ chế | Mục đích | Giải thích |
  |---|---|---|
  | `pg_stat_statements` | Thống kê **tổng hợp** mọi câu SQL | Ghi nhận: mỗi câu SQL được gọi bao nhiêu lần (`calls`), tổng thời gian (`total_exec_time`), thời gian trung bình (`mean_exec_time`). Hữu ích để tìm câu SQL "ăn" nhiều thời gian nhất **tích lũy** — có thể 1 câu chạy nhanh (5ms) nhưng gọi 10,000 lần → 50 giây. |
  | `log_min_duration_statement=500` | Ghi log câu SQL chạy > 500ms | Bắt **slow queries** — câu SQL đơn lẻ chạy quá lâu. Xem bằng `docker compose logs db`. |

  **Tại sao `shared_preload_libraries` yêu cầu restart?** PostgreSQL load shared libraries (file `.so`) vào bộ nhớ **1 lần duy nhất** lúc khởi động. Không thể load/unload library khi đang chạy vì nó gắn vào shared memory segment mà tất cả connection processes dùng chung.

  Sau khi restart, cần tạo extension:
  ```sql
  CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
  ```

  ### Thay đổi 4: Redis — Bật Slowlog + Latency Monitor

  Tương tự PostgreSQL, Redis không cần OTel vì đã được trace từ phía client. Bổ sung monitoring nội bộ:

  ```yaml
  # saleor-platform/docker-compose.yml — service redis
  command: >
    redis-server
      --slowlog-log-slower-than 10000
      --slowlog-max-len 128
      --latency-monitor-threshold 50
  ```

  | Tham số | Đơn vị | Ý nghĩa |
  |---|---|---|
  | `slowlog-log-slower-than 10000` | microseconds (µs) | Ghi log lệnh Redis chạy > **10ms**. Redis cực nhanh (thường < 1ms), nên 10ms đã là bất thường. |
  | `slowlog-max-len 128` | entries | Giữ tối đa 128 entries mới nhất trong bộ nhớ (ring buffer). |
  | `latency-monitor-threshold 50` | milliseconds (ms) | Bật tính năng theo dõi latency spike. Redis ghi nhận mọi thời điểm có lệnh > 50ms để phân tích pattern. |

  ### Thay đổi 5: OTel Collector — Thêm HTTP Receiver

  ```yaml
  # saleor-platform/otelcol/config.yaml
  receivers:
    otlp:
      protocols:
        grpc:
          endpoint: 0.0.0.0:4317   # Cho API (gRPC hoạt động bình thường)
        http:
          endpoint: 0.0.0.0:4318   # Cho Worker (cần HTTP vì prefork + gRPC không tương thích)
  ```

  Collector giờ lắng nghe trên **2 port**: 4317 (gRPC, cho API) và 4318 (HTTP, cho Worker). Cả hai đều gửi vào cùng pipeline traces → Jaeger + spanmetrics → Prometheus.

  ### Luồng dữ liệu SAU khi thay đổi

  ```
  ┌── Saleor API (single-worker Uvicorn) ────────────────────────┐
  │  opentelemetry-instrument (khởi tạo TracerProvider)          │
  │  Saleor tự tạo spans:                                        │
  │    ├─ HTTP request (method, url, status_code)                │
  │    ├─ GraphQL query (document, operation_type, cost)         │
  │    └─ DataLoader batch_load (loader_name)                    │
  │  OTel auto-instrumentation tạo spans:                        │
  │    ├─ psycopg: SELECT/INSERT/UPDATE (db.statement, duration) │
  │    ├─ redis: GET/SET/PUBLISH (command, key)                  │
  │    ├─ httpx/urllib3: outbound HTTP (url, status_code)        │
  │    └─ celery: task.apply_async (task_name)                   │
  │         │                                                     │
  │         ▼ OTLP gRPC (:4317)                                  │
  └──────────┬────────────────────────────────────────────────────┘
             │
  ┌── OTel Collector ──────────────────────────────────────────────┐
  │  ├─► Jaeger (traces UI — xem chi tiết từng request)           │
  │  └─► spanmetrics → Prometheus → Grafana (RED metrics tổng hợp)│
  └──────────┬────────────────────────────────────────────────────┘
             │
  ┌── Celery Worker (8 prefork processes) ───────────────────────┐
  │  opentelemetry-instrument (khởi tạo TracerProvider)          │
  │  OTel auto-instrumentation tạo spans:                        │
  │    ├─ celery: task.execute (task_name, duration, result)     │
  │    ├─ psycopg: SQL queries trong task                        │
  │    └─ redis: Redis commands trong task                       │
  │         │                                                     │
  │         ▼ OTLP HTTP (:4318) — dùng HTTP vì prefork+gRPC lỗi │
  └──────────┘                                                    │
                                                                   │
  ┌── PostgreSQL (workaround — không dùng OTel) ────────────────┐
  │  pg_stat_statements: thống kê tổng hợp (calls, mean_time)   │
  │  slow query log: ghi câu SQL > 500ms vào log                 │
  │  (SQL traces đã có từ psycopg instrumentor phía client)      │
  └──────────────────────────────────────────────────────────────┘
                                                                   
  ┌── Redis (workaround — không dùng OTel) ─────────────────────┐
  │  slowlog: ghi lệnh > 10ms                                    │
  │  latency-monitor: theo dõi spike > 50ms                      │
  │  (Redis traces đã có từ redis instrumentor phía client)      │
  └──────────────────────────────────────────────────────────────┘
  ```

  ### Các file thay đổi

  | File | Thay đổi |
  |------|----------|
  | `saleor-platform/docker-compose.yml` | API: thêm command cài OTel instrumentors, bỏ --workers=2. Worker: thêm command cài OTel instrumentors + opentelemetry-instrument wrapper, thêm OTEL_SERVICE_NAME + HTTP exporter. DB: thêm command bật pg_stat_statements + slow query log. Redis: thêm command bật slowlog + latency monitor. |
  | `saleor-platform/otelcol/config.yaml` | Thêm HTTP protocol receiver trên port 4318 cho Worker |

  ### Ví dụ trace thực tế trên Jaeger

  Request `{ shop { name } }` tạo ra 4 spans liên kết:
  ```
  /graphql/ (HTTP POST, 200, 41ms)                    ← saleor.service
    └─ { shop { name } } (GraphQL query, 40ms)        ← saleor.service
        └─ SiteByIdLoader (batch_load, 37ms)           ← saleor.core
            └─ SELECT django_site... (psycopg, 10ms)   ← opentelemetry.instrumentation.psycopg
  ```

  Mỗi span chứa metadata chi tiết:
  - **HTTP span:** `http.request.method=POST`, `http.response.status_code=200`, `url.full=http://localhost:8000/graphql/`
  - **GraphQL span:** `graphql.document={ shop { name } }`, `graphql.operation.type=query`
  - **SQL span:** `db.statement=SELECT django_site...WHERE id IN (%s)`, `db.system=postgresql`, `net.peer.name=db`, `net.peer.port=5432`

- **Outcome:** SUCCESS. Toàn bộ 4 cấu phần đã được cấu hình tracing/monitoring. Jaeger hiển thị 2 service: `saleor` (API) và `saleor-worker` (Worker) với đầy đủ spans. PostgreSQL track 44+ queries qua pg_stat_statements. Redis slowlog hoạt động.

- **Key Lessons:**
  1. **`opentelemetry-instrument` không tương thích với Uvicorn multi-worker (`--workers > 1`)** vì Uvicorn dùng `multiprocessing.spawn` (không phải `fork`). Child processes được tạo hoàn toàn mới, không kế thừa TracerProvider → traces không bao giờ được gửi đi. Giải pháp: chạy single worker hoặc cấu hình OTel programmatically trong code ứng dụng.
  2. **Celery prefork pool không tương thích với gRPC exporter** vì `fork()` phá hỏng C-level threads của libgrpc. Giải pháp: dùng HTTP/protobuf exporter thay gRPC.
  3. **Không cài `opentelemetry-instrumentation-django` cho Saleor** — Saleor có module telemetry riêng (`saleor.core.telemetry`) đã tự xử lý Django/HTTP/GraphQL tracing. Cài thêm gây xung đột settings loading order.
  4. **PostgreSQL và Redis không cần OTel trực tiếp** — tracing từ phía client (psycopg, redis instrumentor) đã đủ. Phía server chỉ cần cơ chế monitoring nội bộ (pg_stat_statements, slowlog) để bổ sung góc nhìn aggregate.
  5. **`opentelemetry-instrument` hoạt động bằng monkey-patching** — nó thay thế hàm gốc của thư viện bằng phiên bản có thêm logic tạo span. Đây là lý do cần cài đúng version gói instrumentation khớp với version OTel SDK (`==0.53b1` khớp với `opentelemetry-sdk 1.32.1`).
  6. **YAML `>` (folded block scalar) không phù hợp cho `sh -c` command phức tạp** — nó nối các dòng thành 1 dòng nhưng xử lý indentation sai. Dùng YAML list format `[sh, -c, |]` cho multiline shell scripts trong docker-compose.