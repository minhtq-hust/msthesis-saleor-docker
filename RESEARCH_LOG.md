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
  # Lệnh import (Basic Auth: admin / Qu@ngm!nh1998 → base64)
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