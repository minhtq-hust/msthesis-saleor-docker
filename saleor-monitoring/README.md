# Saleor Monitoring Stack

Hệ thống giám sát (monitoring) cho Saleor Platform, sử dụng **Prometheus**, **Grafana** và **cAdvisor** để thu thập và hiển thị metrics của các container Docker.

## Mục lục

- [Kiến trúc tổng quan](#kiến-trúc-tổng-quan)
- [Yêu cầu](#yêu-cầu)
- [Cấu trúc thư mục](#cấu-trúc-thư-mục)
- [Hướng dẫn sử dụng](#hướng-dẫn-sử-dụng)
  - [Khởi chạy Saleor Platform trước](#1-khởi-chạy-saleor-platform-trước)
  - [Khởi chạy Monitoring Stack](#2-khởi-chạy-monitoring-stack)
  - [Kiểm tra trạng thái](#3-kiểm-tra-trạng-thái)
  - [Truy cập giao diện Web](#4-truy-cập-giao-diện-web)
- [Thiết lập Grafana](#thiết-lập-grafana)
  - [Đăng nhập lần đầu](#đăng-nhập-lần-đầu)
  - [Thêm Prometheus làm Data Source](#thêm-prometheus-làm-data-source)
  - [Import Dashboard](#import-dashboard-cho-docker-monitoring)
- [Cấu hình chi tiết](#cấu-hình-chi-tiết)
  - [Prometheus config](#prometheus-prometheusyml)
  - [Docker Compose](#giải-thích-cấu-hình-docker-compose)
- [Hướng dẫn PromQL](#hướng-dẫn-promql-prometheus-query-language)
  - [Giải thích cú pháp cơ bản](#giải-thích-cú-pháp-cơ-bản)
  - [CPU](#cpu--ai-đang-chiếm-cpu)
  - [Memory](#memory--ai-đang-chiếm-ram)
  - [Network](#network--ai-đang-truyền-nhiều-dữ-liệu)
  - [Disk I/O](#disk-io--ai-đang-đọcghi-ổ-đĩa-nhiều)
  - [Response Time](#response-time--thời-gian-phản-hồi)
- [Các lệnh thường dùng](#các-lệnh-thường-dùng)
- [Thứ tự khởi chạy / dừng](#thứ-tự-khởi-chạy--dừng)
- [Troubleshooting](#troubleshooting)

## Kiến trúc tổng quan

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Host (Linux)                       │
│                                                             │
│  ┌─── saleor-platform ───────────────────────────────────┐  │
│  │  api (8000) │ db (5432) │ redis (6379) │ worker       │  │
│  │  dashboard (9000) │ mailpit (8025) │ otel-collector   │  │
│  └───────────────────────┬───────────────────────────────┘  │
│                          │ network: saleor-backend-tier      │
│  ┌─── saleor-monitoring ─┼───────────────────────────────┐  │
│  │                       │                               │  │
│  │  ┌──────────┐   scrape   ┌───────────┐                │  │
│  │  │Prometheus├───────────►│ cAdvisor  │                │  │
│  │  │  :9090   ├──────┐     │   :8080   │                │  │
│  │  └────┬─────┘      │     └─────┬─────┘                │  │
│  │       │ ds         │scrape     │ đọc metrics          │  │
│  │  ┌────▼─────┐   ┌──┴───────┐ ┌─▼─────────┐            │  │
│  │  │ Grafana  │   │ Jaeger   │ │Docker API │            │  │
│  │  │  :3000   │   │ :16686   │ │ (socket)  │            │  │
│  │  └──────────┘   └──────────┘ └───────────┘            │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

**Luồng dữ liệu:**
1. **cAdvisor** đọc metrics tài nguyên từ Docker API.
2. **Saleor API** gửi traces (OTLP) tới **otel-collector** (cùng nằm trong saleor-platform).
3. **otel-collector** forward traces tới **Jaeger** và chuyển đổi thành dạng metrics RED.
4. **Prometheus** scrape metrics từ cAdvisor và từ otel-collector mỗi 15 giây.
5. **Grafana** và **Jaeger UI** được dùng để trực quan hóa dữ liệu (metrics và traces).

## Yêu cầu

| Yêu cầu | Phiên bản |
|----------|-----------|
| Docker Engine | ≥ 20.10 |
| Docker Compose | ≥ 2.0 (plugin) |
| OS | Linux (Ubuntu 22.04+) |
| Saleor Platform | Đã chạy (`docker compose up -d`) |

> **Lưu ý quan trọng:** Stack monitoring **phải được khởi chạy SAU** saleor-platform vì nó kết nối vào network `saleor-platform_saleor-backend-tier` được tạo bởi saleor-platform.

## Cấu trúc thư mục

```
saleor-monitoring/
├── docker-compose.yml          # Cấu hình 4 service: Grafana, Prometheus, cAdvisor, Jaeger
├── prometheus/
│   └── prometheus.yml          # Cấu hình scrape targets cho Prometheus
└── README.md                   # File này
```

## Hướng dẫn sử dụng

### 1. Khởi chạy Saleor Platform trước

```bash
cd ../saleor-platform
docker compose up -d
```

Đảm bảo saleor-platform đã chạy ổn định:
```bash
docker compose ps
```

### 2. Khởi chạy Monitoring Stack

```bash
cd ../saleor-monitoring
docker compose up -d
```

### 3. Kiểm tra trạng thái

```bash
docker compose ps -a
```

Kết quả mong đợi — tất cả 3 container đều **Up**:
```
NAME                    STATUS
monitoring-cadvisor     Up (healthy)
monitoring-grafana      Up
monitoring-prometheus   Up
```

### 4. Truy cập giao diện Web

| Service | URL | Mô tả |
|---------|-----|-------|
| **Grafana** | http://localhost:3000 | Dashboard trực quan |
| **Prometheus** | http://localhost:9090 | Truy vấn metrics trực tiếp |
| **cAdvisor** | http://localhost:8080 | Xem metrics container real-time |

## Thiết lập Grafana

### Đăng nhập lần đầu

- **URL:** http://localhost:3000
- **Username:** `admin`
- **Password:** `admin`
- Hệ thống sẽ yêu cầu đổi mật khẩu ngay sau khi đăng nhập

### Thêm Prometheus làm Data Source

1. Vào **Connections** → **Data sources** → **Add data source**
2. Chọn **Prometheus**
3. Cấu hình:
   - **Prometheus server URL:** `http://prometheus:9090`
     > Dùng tên container `prometheus` (không dùng `localhost`) vì Grafana truy cập qua Docker network nội bộ
4. Nhấn **Save & test** → Hiển thị ✅ "Successfully queried the Prometheus API"

### Import Dashboard cho Docker monitoring

1. Vào **Dashboards** → **New** → **Import**
2. Nhập Dashboard ID: `193` (Docker monitoring dashboard phổ biến nhất)
3. Chọn Data source: **Prometheus** (vừa tạo ở bước trên)
4. Nhấn **Import**

Một số Dashboard ID hữu ích khác:
| ID | Tên | Mô tả |
|----|-----|-------|
| `193` | Docker monitoring | Dashboard tổng quan Docker container |
| `14282` | Cadvisor Exporter | Chi tiết metrics từ cAdvisor |
| `1860` | Node Exporter Full | Metrics của máy host (cần thêm node-exporter) |

## Cấu hình chi tiết

### Prometheus (`prometheus/prometheus.yml`)

```yaml
global:
  scrape_interval: 15s        # Thu thập metrics mỗi 15 giây

scrape_configs:
  - job_name: 'prometheus'     # Prometheus tự giám sát chính nó
    static_configs:
      - targets: ['localhost:9090']

  - job_name: 'cadvisor'       # Thu thập metrics container từ cAdvisor
    static_configs:
      - targets: ['cadvisor:8080']
    metric_relabel_configs:
      # Tạo label "service" ngắn gọn (vd: api, db, redis, worker...)
      - source_labels: [container_label_com_docker_compose_service]
        target_label: service
      # Tạo label "project" (vd: saleor-platform, saleor-monitoring)
      - source_labels: [container_label_com_docker_compose_project]
        target_label: project
      # Xóa tất cả label container_label_* dài dòng
      - regex: 'container_label_.*'
        action: labeldrop
```

**Giải thích `metric_relabel_configs`:**
- cAdvisor mặc định gửi hàng chục label dài như `container_label_com_docker_compose_config_hash="abc123..."`
- Relabel config giúp **rút gọn** chúng thành `service="api"` và `project="saleor-platform"`
- Label `container_label_*` bị xóa sạch → tiết kiệm bộ nhớ Prometheus và legend Grafana gọn gàng

### Giải thích cấu hình Docker Compose

#### Prometheus
```yaml
prometheus:
  user: root    # Cần thiết vì file prometheus.yml trên host
                # có permission 640 (rw-r-----).
                # Prometheus mặc định chạy user "nobody" (UID 65534)
                # → không thuộc group osboxes → không đọc được file.
                # Chạy root trong container an toàn nhờ Docker isolation.
```

#### cAdvisor
```yaml
cadvisor:
  privileged: true              # Cần quyền truy cập cgroup, /proc
  pid: host                     # Nhìn thấy PID namespace của host
  volumes:
    - /var/run/docker.sock:ro   # Giao tiếp với Docker API
    - /run/containerd/containerd.sock:ro  # Hỗ trợ containerd runtime
    - /sys:ro                   # Đọc thông tin hệ thống (cgroup, cpu)
    - /var/lib/docker:ro        # Đọc metadata container/image
    - /dev/disk:ro              # Đọc thông tin disk I/O
  devices:
    - /dev/kmsg                 # Đọc kernel messages
```

#### Kết nối Network
```yaml
networks:
  saleor_net:
    external: true
    name: saleor-platform_saleor-backend-tier
    # Kết nối vào network của saleor-platform để Prometheus
    # có thể scrape metrics từ các service như api, db, redis...
    # (nếu cần bổ sung scrape target trong tương lai)
```

## Hướng dẫn PromQL (Prometheus Query Language)

Truy cập **Grafana Explore** (http://localhost:3000/explore) hoặc **Prometheus** (http://localhost:9090) để thử các query sau.

### Giải thích cú pháp cơ bản

Trước khi đọc các query, cần hiểu các hàm PromQL:

| Cú pháp | Ý nghĩa |
|---------|---------|
| `container_cpu_usage_seconds_total` | Metric gốc — tổng số **giây CPU** mà container đã sử dụng từ lúc khởi động. Đây là số luôn tăng (counter). |
| `rate(...[5m])` | Tính **tốc độ tăng trung bình** của counter trong 5 phút gần nhất. Biến counter tích lũy thành giá trị có ý nghĩa: "bao nhiêu CPU đang dùng mỗi giây". |
| `sum by (service) (...)` | **Gộp** tất cả time series có cùng `service` lại. Một container có thể có nhiều CPU core → nhiều series. `sum by` gộp chúng thành 1 giá trị duy nhất cho mỗi service. |
| `{service=~".+"}` | **Bộ lọc** — chỉ lấy metrics có label `service` không rỗng (loại bỏ các container hệ thống không thuộc Docker Compose). |
| `{project="saleor-platform"}` | Chỉ lấy container thuộc project `saleor-platform`, bỏ qua container monitoring. |
| `topk(5, ...)` | Chỉ hiển thị **5 giá trị lớn nhất**. |

### CPU — Ai đang chiếm CPU?

```promql
# CPU usage theo từng service (đơn vị: cores)
# Giá trị 0.05 = service đang dùng 5% của 1 core CPU
sum by (service) (rate(container_cpu_usage_seconds_total{service=~".+"}[5m]))
```

**Đọc kết quả như thế nào:**
- `api: 0.01` → Saleor API đang dùng 1% CPU (bình thường khi idle)
- `api: 0.5` → API đang dùng 50% 1 core (nhiều request GraphQL đang xử lý)
- `worker: 0.3` → Celery worker đang xử lý tác vụ nền (gửi email, đồng bộ...)

**Khi nào dùng:** Kiểm tra khi hệ thống phản hồi chậm → xác định service nào đang "ngốn" CPU.

```promql
# Chỉ xem các service Saleor (bỏ qua Prometheus, Grafana, cAdvisor)
sum by (service) (rate(container_cpu_usage_seconds_total{project="saleor-platform"}[5m]))
```

**Khi nào dùng:** Khi chỉ quan tâm đến hiệu năng của ứng dụng Saleor, không muốn bị nhiễu bởi monitoring.

### Memory — Ai đang chiếm RAM?

```promql
# RAM đang sử dụng theo service (đơn vị: MB)
sum by (service) (container_memory_usage_bytes{service=~".+"}) / 1024 / 1024
```

> Ở đây KHÔNG dùng `rate()` vì `container_memory_usage_bytes` là **gauge** (giá trị tức thời, lên/xuống), không phải counter. Đọc thẳng giá trị hiện tại là đủ.

**Đọc kết quả như thế nào:**
- `db: 120` → PostgreSQL đang dùng 120 MB RAM
- `api: 350` → Saleor API đang dùng 350 MB (bình thường cho Django + Uvicorn)
- `redis: 15` → Redis cache nhẹ, chỉ 15 MB

**Khi nào dùng:** 
- Kiểm tra trước khi deploy tính toán xem máy cần bao nhiêu RAM
- Phát hiện memory leak: nếu RAM của `api` tăng liên tục theo thời gian mà không giảm

```promql
# RAM chỉ của Saleor Platform
sum by (service) (container_memory_usage_bytes{project="saleor-platform"}) / 1024 / 1024
```

### Network — Ai đang truyền nhiều dữ liệu?

```promql
# Tốc độ NHẬN dữ liệu vào (bytes/giây)
sum by (service) (rate(container_network_receive_bytes_total{service=~".+"}[5m]))

# Tốc độ GỬI dữ liệu ra (bytes/giây)
sum by (service) (rate(container_network_transmit_bytes_total{service=~".+"}[5m]))
```

> Dùng `rate()` vì `*_bytes_total` là counter tích lũy. `rate()` chuyển thành "bytes/giây" — dễ đọc hơn.

**Đọc kết quả như thế nào:**
- `api` gửi nhiều = đang phản hồi nhiều response GraphQL cho client
- `db` nhận nhiều = đang nhận nhiều SQL queries từ api
- `redis` nhận/gửi ít = bình thường (cache hit/miss nhanh, dữ liệu nhỏ)

**Khi nào dùng:** Phát hiện bottleneck mạng, ví dụ `db` nhận quá nhiều → cần tối ưu query hoặc thêm index.

### Disk I/O — Ai đang đọc/ghi ổ đĩa nhiều?

```promql
# Tốc độ ĐỌC disk (bytes/giây)
sum by (service) (rate(container_fs_reads_bytes_total{service=~".+"}[5m]))

# Tốc độ GHI disk (bytes/giây)
sum by (service) (rate(container_fs_writes_bytes_total{service=~".+"}[5m]))
```

**Đọc kết quả như thế nào:**
- `db` ghi nhiều = PostgreSQL đang ghi WAL (Write-Ahead Log) hoặc flush dữ liệu
- `api` đọc nhiều = có thể đang load static files hoặc media uploads
- `worker` ghi nhiều = Celery đang xử lý tác vụ có ghi file (export, report...)

**Khi nào dùng:** Khi hệ thống chậm mà CPU, RAM bình thường → có thể do I/O disk là bottleneck.

### Response Time — Thời gian phản hồi

> **Lưu ý quan trọng:** cAdvisor **không đo được response time**. cAdvisor chỉ giám sát tài nguyên hệ thống (CPU, RAM, Network, Disk) — nó không biết ứng dụng bên trong container phản hồi nhanh hay chậm.

Response time là metric ở **tầng ứng dụng (application-level)**. Trong hệ thống Saleor hiện tại, response time được đo qua **Jaeger** (đã có sẵn trong saleor-platform).

#### Jaeger — Xem response time trực tiếp

Saleor API đã được cấu hình sẵn OpenTelemetry (`OTEL_TRACES_EXPORTER=otlp`) và gửi traces đến Jaeger:

| Thông tin | Giá trị |
|-----------|--------|
| **URL** | http://localhost:16686 |
| **Service name** | `saleor` |
| **Dữ liệu có sẵn** | Duration (response time), spans (từng bước xử lý), errors |

**Cách xem:**
1. Truy cập http://localhost:16686
2. Chọn **Service** = `saleor`
3. Nhấn **Find Traces**
4. Mỗi trace hiển thị tổng thời gian xử lý (duration) và chi tiết từng span:
   - `HTTP GET /graphql/` → tổng response time
   - `PostgreSQL query` → thời gian truy vấn DB
   - `Redis GET` → thời gian đọc cache

**Khi nào dùng Jaeger thay vì Prometheus/Grafana:**

| Mục đích | Dùng công cụ nào |
|----------|------------------|
| API phản hồi chậm → muốn biết **bước nào** gây chậm | **Jaeger** (xem từng span trong trace) |
| Muốn biết container nào **chiếm nhiều tài nguyên** | **Grafana + Prometheus** (CPU, RAM, Network) |
| Phát hiện **lỗi** trong xử lý request | **Jaeger** (trace có tag `error=true`) |
| Giám sát tổng quan **sức khỏe hệ thống** liên tục | **Grafana dashboard** (biểu đồ theo thời gian) |

#### So sánh: Metrics (Prometheus) vs Traces (Jaeger)

```
Prometheus/Grafana (Metrics)          Jaeger (Traces)
────────────────────────              ─────────────────────
"API dùng 50% CPU"                    "Request GET /graphql/ mất 230ms"
"DB dùng 120 MB RAM"                  "  └─ DB query mất 180ms (bottleneck!)"
"Network nhận 5 KB/s"                 "  └─ Redis cache hit: 2ms"
                                      "  └─ Serialize response: 48ms"
Trả lời: CÁI GÌ đang bận?            Trả lời: TẠI SAO lại chậm?
```

## Các lệnh thường dùng

```bash
# Khởi chạy monitoring
docker compose up -d

# Dừng monitoring
docker compose down

# Xem logs
docker compose logs -f              # Tất cả
docker compose logs -f prometheus   # Chỉ Prometheus
docker compose logs -f cadvisor     # Chỉ cAdvisor
docker compose logs -f grafana      # Chỉ Grafana

# Kiểm tra Prometheus targets
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool

# Kiểm tra cAdvisor metrics
curl -s http://localhost:8080/metrics | head -50

# Restart một service
docker compose restart prometheus

# Xóa toàn bộ data và chạy lại từ đầu (mất dashboard Grafana đã tạo)
docker compose down -v
docker compose up -d
```

## Thứ tự khởi chạy / dừng

### Khởi chạy (Start)
```bash
# 1. Saleor Platform TRƯỚC
cd saleor-platform && docker compose up -d

# 2. Monitoring SAU
cd ../saleor-monitoring && docker compose up -d
```

### Dừng (Stop)
```bash
# 1. Monitoring TRƯỚC
cd saleor-monitoring && docker compose down

# 2. Saleor Platform SAU
cd ../saleor-platform && docker compose down
```

> Thứ tự này quan trọng vì monitoring kết nối vào network của saleor-platform. Nếu dừng saleor-platform trước, monitoring sẽ mất kết nối network.

## Troubleshooting

### Prometheus bị "Restarting" liên tục

**Triệu chứng:** `docker compose ps` hiển thị Prometheus ở trạng thái `Restarting`

**Nguyên nhân thường gặp:**

1. **Permission denied khi đọc config:**
   ```
   Error loading config: open /etc/prometheus/prometheus.yml: permission denied
   ```
   → Đảm bảo `user: root` đã được thêm trong `docker-compose.yml` cho service prometheus

2. **Permission denied khi ghi data:**
   ```
   open /prometheus/queries.active: permission denied
   ```
   → Xóa volume cũ và tạo lại:
   ```bash
   docker compose down -v
   docker compose up -d
   ```

### cAdvisor không hiển thị metrics container

**Nguyên nhân:** Thiếu mount Docker socket hoặc thiếu quyền `privileged`.

**Kiểm tra:**
```bash
curl -s http://localhost:8080/metrics | grep container_cpu_usage_seconds_total
```

Nếu không có output → kiểm tra logs:
```bash
docker compose logs cadvisor
```

### Grafana không kết nối được Prometheus

- **Sai URL:** Dùng `http://prometheus:9090` (tên container), KHÔNG dùng `http://localhost:9090`
- **Chưa cùng network:** Kiểm tra cả hai container đều thuộc cùng network:
  ```bash
  docker network inspect saleor-platform_saleor-backend-tier
  ```

### Network "saleor-platform_saleor-backend-tier" not found

**Nguyên nhân:** Saleor Platform chưa được khởi chạy.

**Giải pháp:**
```bash
cd ../saleor-platform
docker compose up -d
cd ../saleor-monitoring
docker compose up -d
```
