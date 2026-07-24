#!/bin/bash
# =============================================================================
# run_baseline.sh — Baseline Run Wrapper
# =============================================================================
# Mục đích: Thu đủ 4 lớp data cho mỗi baseline run
#   Lớp 1: Locust CSV (stats + stats_history)
#   Lớp 2: Prometheus time range metadata (để query sau)
#   Lớp 3: Prometheus window metadata cho Jaeger query
#   Lớp 4: pg_stat_statements snapshot (slow queries)
#
# Sử dụng:
#   ./run_baseline.sh <run_id> [users] [duration]
#
# Ví dụ:
#   ./run_baseline.sh baseline_0.1_run1
#   ./run_baseline.sh baseline_0.1_run1 50 12m
#
# Output files (trong $RESULT_DIR/):
#   <run_id>_stats.csv                — aggregate latency/RPS per endpoint
#   <run_id>_stats_history.csv        — time-series latency/RPS (15s buckets)
#   <run_id>_failures.csv             — error details
#   <run_id>_exceptions.csv           — exception details
#   <run_id>_ccu_history.csv          — concurrent user history
#   <run_id>_meta.json                — run metadata: timestamps, config, VUs
#   <run_id>_pg_slow_queries.txt      — top 10 slow queries post-run
#   <run_id>_pg_connections.txt       — DB connection state post-run
#   <run_id>_pg_locks.txt             — lock contention post-run
#   <run_id>_redis_info.txt           — Redis keyspace stats post-run
# =============================================================================

set -e

# --- Config -------------------------------------------------------------------
APP_HOST="osboxes@11.1.11.136"
APP_PORT="8000"
LOADTEST_DIR="/mnt/appdata/thesis/project/saleor_rmtclient/loadtest"
RESULT_DIR="$LOADTEST_DIR/results"
DB_CONTAINER="saleor-platform-db-1"
REDIS_CONTAINER="saleor-platform-redis-1"
DB_USER="saleor"
DB_NAME="saleor"

# --- Arguments ----------------------------------------------------------------
RUN_ID="${1:-baseline_run_$(date +%Y%m%d_%H%M%S)}"
VUS="${2:-50}"
DURATION="${3:-12m}"

# Validate
if [[ -z "$RUN_ID" ]]; then
  echo "Usage: $0 <run_id> [users=50] [duration=12m]"
  exit 1
fi

echo ""
echo "============================================================"
echo "  Baseline Run: $RUN_ID"
echo "  VUs: $VUS | Duration: $DURATION | Host: http://$APP_HOST:$APP_PORT"
echo "============================================================"
echo ""

# =============================================================================
# PHASE A: PRE-RUN
# =============================================================================
echo "[A] Pre-run setup..."

# Ghi timestamp bắt đầu (Unix epoch, dùng để query Prometheus sau)
T_START=$(date +%s)
T_START_ISO=$(date -Iseconds)

# Reset pg_stat_statements để số liệu chỉ phản ánh run này
echo "  [A1] Resetting pg_stat_statements..."
ssh "$APP_HOST" "docker exec $DB_CONTAINER psql -U $DB_USER -d $DB_NAME \
  -c 'SELECT pg_stat_statements_reset();' -q"

# Ghi pre-run DB connection state (baseline trước khi load)
echo "  [A2] Snapshotting pre-run DB state..."
ssh "$APP_HOST" "docker exec $DB_CONTAINER psql -U $DB_USER -d $DB_NAME -c \
  \"SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY state;\" \
  --csv" > "$RESULT_DIR/${RUN_ID}_pre_run_connections.txt" 2>/dev/null || true

echo "  [A] Done. Start time: $T_START_ISO ($T_START)"
echo ""

# =============================================================================
# PHASE B: RUN LOCUST
# =============================================================================
echo "[B] Starting Locust (${VUS} VUs, ${DURATION})..."
echo "    Output prefix: $RESULT_DIR/$RUN_ID"
echo ""

cd "$LOADTEST_DIR"

locust -f locustfile.py \
  --headless \
  --users "$VUS" \
  --spawn-rate 5 \
  --run-time "$DURATION" \
  --csv "$RESULT_DIR/$RUN_ID" \
  --csv-full-history \
  --host "http://$(echo $APP_HOST | cut -d@ -f2):$APP_PORT" \
  --loglevel WARNING

echo ""
echo "[B] Locust run complete."

# =============================================================================
# PHASE C: POST-RUN DATA COLLECTION
# =============================================================================
T_END=$(date +%s)
T_END_ISO=$(date -Iseconds)
echo "[C] Post-run data collection..."

# C1: pg_stat_statements — top 10 slow queries
echo "  [C1] Collecting slow query data..."
ssh "$APP_HOST" "docker exec $DB_CONTAINER psql -U $DB_USER -d $DB_NAME --csv -c \
  \"SELECT
      LEFT(query, 120) AS query_short,
      calls,
      round(mean_exec_time::numeric, 2) AS mean_ms,
      round(total_exec_time::numeric, 2) AS total_ms,
      rows,
      round(100.0 * shared_blks_hit / NULLIF(shared_blks_hit + shared_blks_read, 0), 1) AS buffer_hit_pct,
      round(stddev_exec_time::numeric, 2) AS stddev_ms
   FROM pg_stat_statements
   WHERE query NOT LIKE '%pg_stat%'
     AND query NOT LIKE '%BEGIN%'
   ORDER BY mean_exec_time DESC
   LIMIT 10;\"" > "$RESULT_DIR/${RUN_ID}_pg_slow_queries.txt" 2>/dev/null

# C2: pg_stat_activity — connection state breakdown
echo "  [C2] Collecting DB connection state..."
ssh "$APP_HOST" "docker exec $DB_CONTAINER psql -U $DB_USER -d $DB_NAME --csv -c \
  \"SELECT
      state,
      wait_event_type,
      count(*) AS conn_count
   FROM pg_stat_activity
   WHERE datname = '$DB_NAME'
   GROUP BY state, wait_event_type
   ORDER BY conn_count DESC;\"" > "$RESULT_DIR/${RUN_ID}_pg_connections.txt" 2>/dev/null

# C3: pg_locks — lock contention snapshot
echo "  [C3] Collecting lock contention data..."
ssh "$APP_HOST" "docker exec $DB_CONTAINER psql -U $DB_USER -d $DB_NAME --csv -c \
  \"SELECT
      relation::regclass AS table_name,
      mode,
      granted,
      count(*) AS lock_count
   FROM pg_locks
   WHERE relation IS NOT NULL
   GROUP BY relation, mode, granted
   ORDER BY lock_count DESC
   LIMIT 20;\"" > "$RESULT_DIR/${RUN_ID}_pg_locks.txt" 2>/dev/null

# C4: Redis INFO — keyspace và memory
echo "  [C4] Collecting Redis stats..."
ssh "$APP_HOST" "docker exec $REDIS_CONTAINER redis-cli INFO stats" \
  | grep -E "keyspace_hits|keyspace_misses|evicted_keys|total_commands" \
  > "$RESULT_DIR/${RUN_ID}_redis_info.txt" 2>/dev/null || true
ssh "$APP_HOST" "docker exec $REDIS_CONTAINER redis-cli INFO memory" \
  | grep -E "used_memory_human|mem_fragmentation" \
  >> "$RESULT_DIR/${RUN_ID}_redis_info.txt" 2>/dev/null || true

echo "  [C] Done."
echo ""

# =============================================================================
# PHASE D: WRITE METADATA FILE
# =============================================================================
echo "[D] Writing metadata..."

# Tính duration thực tế
ACTUAL_DURATION=$((T_END - T_START))

# Đọc total requests và error rate từ stats.csv
STATS_FILE="$RESULT_DIR/${RUN_ID}_stats.csv"
if [[ -f "$STATS_FILE" ]]; then
  TOTAL_REQUESTS=$(tail -1 "$STATS_FILE" | cut -d',' -f3)
  TOTAL_FAILURES=$(tail -1 "$STATS_FILE" | cut -d',' -f4)
  AGG_RPS=$(tail -1 "$STATS_FILE" | cut -d',' -f10)
  AGG_P95=$(tail -1 "$STATS_FILE" | cut -d',' -f16)
else
  TOTAL_REQUESTS="N/A"
  TOTAL_FAILURES="N/A"
  AGG_RPS="N/A"
  AGG_P95="N/A"
fi

# Write JSON metadata
cat > "$RESULT_DIR/${RUN_ID}_meta.json" << METAEOF
{
  "run_id": "$RUN_ID",
  "scenario": "normal",
  "config_version": "Gunicorn+2xUvicornWorker",
  "workers": 2,
  "threads_per_worker": 12,
  "vus": $VUS,
  "spawn_rate": 5,
  "duration_requested": "$DURATION",
  "duration_actual_seconds": $ACTUAL_DURATION,
  "t_start_unix": $T_START,
  "t_end_unix": $T_END,
  "t_start_iso": "$T_START_ISO",
  "t_end_iso": "$T_END_ISO",
  "app_host": "$APP_HOST",
  "app_port": $APP_PORT,
  "total_requests": "$TOTAL_REQUESTS",
  "total_failures": "$TOTAL_FAILURES",
  "aggregate_rps": "$AGG_RPS",
  "aggregate_p95_ms": "$AGG_P95",
  "files": {
    "stats": "${RUN_ID}_stats.csv",
    "stats_history": "${RUN_ID}_stats_history.csv",
    "failures": "${RUN_ID}_failures.csv",
    "meta": "${RUN_ID}_meta.json",
    "pg_slow_queries": "${RUN_ID}_pg_slow_queries.txt",
    "pg_connections": "${RUN_ID}_pg_connections.txt",
    "pg_locks": "${RUN_ID}_pg_locks.txt",
    "redis_info": "${RUN_ID}_redis_info.txt"
  },
  "prometheus_query_range": {
    "note": "Use t_start_unix and t_end_unix to query Prometheus API",
    "example_cpu": "http://11.1.11.136:9090/api/v1/query_range?query=rate(container_cpu_usage_seconds_total{service=~\".+\"}[1m])*100&start=$T_START&end=$T_END&step=15"
  }
}
METAEOF

echo "  Written: ${RUN_ID}_meta.json"

# =============================================================================
# SUMMARY
# =============================================================================
echo ""
echo "============================================================"
echo "  Run Complete: $RUN_ID"
echo "============================================================"
echo "  Duration:     ${ACTUAL_DURATION}s"
echo "  Requests:     $TOTAL_REQUESTS (failures: $TOTAL_FAILURES)"
echo "  Aggregate:    RPS=$AGG_RPS  P95=${AGG_P95}ms"
echo ""
echo "  Prometheus window:"
echo "    t_start = $T_START  ($T_START_ISO)"
echo "    t_end   = $T_END  ($T_END_ISO)"
echo ""
echo "  Output files in: $RESULT_DIR/"
echo "    $(ls $RESULT_DIR/${RUN_ID}_* 2>/dev/null | xargs -I{} basename {} | tr '\n' ' ')"
echo ""
echo "  Next: Check error rate in ${RUN_ID}_failures.csv"
echo "  If error_rate > 1%: DISCARD this run and retry."
echo "============================================================"
