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
