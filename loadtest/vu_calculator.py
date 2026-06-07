"""
VU Calculator — Dựa trên kết quả baseline và constraints infra cố định.

Constraints:
  - Uvicorn workers: 2 (sync threadpool ~12 threads each → ~24 concurrent slots)
  - PG max_connections: 100 (available for API: ~86)
  - CONN_MAX_AGE=0: 1 DB conn per active request, held for full duration
  - limit-max-requests=10000 per worker

Approach:
  1. Estimate effective concurrent DB connections at given VU
  2. Ensure DB connections stay under 86 (safety margin)
  3. Ensure total requests stay under 20K for test duration (2 workers × 10K)
"""

# ── From Normal baseline Run 1 (the good run) ─────────────────────────
# These are median response times (seconds) — represent "healthy" behavior
NORMAL_RESPONSE_TIMES = {
    "browse":           0.530,   # weight=6
    "search":           0.280,   # weight=2
    "detail":           0.750,   # weight=1
    "checkout_full":   13.0 + 3.6 + 56.0 + 0.37,  # 7 steps sequential: ~73s total
    # checkout weight=1, but holds DB conn for ~73s!
}

FLASH_RESPONSE_TIMES = {
    "browse":           0.940,
    "search":           0.490,
    "detail":           1.400,
    "checkout_full":   16.0 + 2.9 + 34.0 + 0.003,  # Flash: ~53s (shorter due to less completion)
}

NORMAL_WEIGHTS = {"browse": 6, "search": 2, "detail": 1, "checkout_full": 1}
FLASH_WEIGHTS  = {"browse": 4, "search": 1, "detail": 1, "checkout_full": 4}

# ── DB connection budget ──────────────────────────────────────────────
PG_MAX = 100
PG_RESERVED_CELERY = 9      # 8 prefork + 1 beat
PG_RESERVED_SYSTEM = 5      # PG internal
PG_AVAILABLE = PG_MAX - PG_RESERVED_CELERY - PG_RESERVED_SYSTEM  # = 86

# ── limit-max-requests budget ────────────────────────────────────────
MAX_REQUESTS_PER_WORKER = 10000
NUM_WORKERS = 2
TOTAL_REQUEST_BUDGET = MAX_REQUESTS_PER_WORKER * NUM_WORKERS  # 20,000

def calc_vu_metrics(vu, think_time, weights, response_times, test_duration_min):
    """Calculate key metrics for given VU count."""
    total_weight = sum(weights.values())
    
    # Weighted average response time per task invocation
    w_resp = sum(
        (weights[k] / total_weight) * response_times[k]
        for k in weights
    )
    
    # Little's Law: CCU = VU × W_resp / (W_resp + W_think)
    ccu = vu * w_resp / (w_resp + think_time)
    
    # Request rate = VU / (W_resp + W_think)
    req_rate = vu / (w_resp + think_time)
    
    # Total requests in test duration
    test_duration_s = test_duration_min * 60
    total_requests = req_rate * test_duration_s
    
    # Checkout is 7 separate GraphQL requests, each holds DB conn
    # During checkout (~73s normal, ~53s flash), 1 VU holds 1 DB conn continuously
    checkout_fraction = weights["checkout_full"] / total_weight
    checkout_duration = response_times["checkout_full"]
    
    # Expected concurrent checkouts at any time
    # = VU × (checkout_fraction × checkout_duration) / (W_resp + W_think)
    concurrent_checkouts = vu * (checkout_fraction * checkout_duration) / (w_resp + think_time)
    
    # Each checkout = 1 DB connection held for full duration
    # Each non-checkout request = 1 DB connection held for ~0.5s
    non_checkout_fraction = 1 - checkout_fraction
    non_checkout_resp = sum(
        (weights[k] / total_weight) * response_times[k]
        for k in weights if k != "checkout_full"
    ) / non_checkout_fraction if non_checkout_fraction > 0 else 0
    concurrent_non_checkout = vu * (non_checkout_fraction * non_checkout_resp) / (w_resp + think_time)
    
    # Total estimated concurrent DB connections
    total_db_conns = concurrent_checkouts + concurrent_non_checkout
    
    return {
        "VU": vu,
        "think_time": think_time,
        "W_resp_avg": w_resp,
        "CCU": ccu,
        "req_rate": req_rate,
        "total_requests": total_requests,
        "concurrent_checkouts": concurrent_checkouts,
        "concurrent_db_conns": total_db_conns,
        "db_utilization": total_db_conns / PG_AVAILABLE * 100,
        "request_budget_pct": total_requests / TOTAL_REQUEST_BUDGET * 100,
    }

print("=" * 90)
print("NORMAL SCENARIO — think_time=4.0s, test_duration=12min")
print("=" * 90)
print(f"{'VU':>4} | {'W_resp':>6} | {'CCU':>5} | {'req/s':>5} | {'Total Req':>9} | {'Budget%':>7} | {'DB Conns':>8} | {'DB Util%':>8} | Status")
print("-" * 90)
for vu in [10, 20, 30, 40, 50, 60, 80, 100]:
    m = calc_vu_metrics(vu, 4.0, NORMAL_WEIGHTS, NORMAL_RESPONSE_TIMES, 12)
    status = "✅ OK" if m["db_utilization"] < 80 and m["request_budget_pct"] < 80 else (
        "⚠️  RISKY" if m["db_utilization"] < 100 and m["request_budget_pct"] < 100 else "❌ OVERLOAD"
    )
    print(f"{m['VU']:4d} | {m['W_resp_avg']:5.1f}s | {m['CCU']:5.1f} | {m['req_rate']:5.1f} | {m['total_requests']:9.0f} | {m['request_budget_pct']:6.1f}% | {m['concurrent_db_conns']:8.1f} | {m['db_utilization']:7.1f}% | {status}")

print()
print("=" * 90)
print("FLASH SALE SCENARIO — think_time=1.0s, measurement window=5min (peak phase)")
print("=" * 90)
print(f"{'VU':>4} | {'W_resp':>6} | {'CCU':>5} | {'req/s':>5} | {'Total Req':>9} | {'Budget%':>7} | {'DB Conns':>8} | {'DB Util%':>8} | Status")
print("-" * 90)
# Flash sale total duration = 14 min, but peak phase has most requests
for vu in [10, 20, 30, 40, 50, 60, 80, 100]:
    m = calc_vu_metrics(vu, 1.0, FLASH_WEIGHTS, FLASH_RESPONSE_TIMES, 14)
    status = "✅ OK" if m["db_utilization"] < 80 and m["request_budget_pct"] < 80 else (
        "⚠️  RISKY" if m["db_utilization"] < 100 and m["request_budget_pct"] < 100 else "❌ OVERLOAD"
    )
    print(f"{m['VU']:4d} | {m['W_resp_avg']:5.1f}s | {m['CCU']:5.1f} | {m['req_rate']:5.1f} | {m['total_requests']:9.0f} | {m['request_budget_pct']:6.1f}% | {m['concurrent_db_conns']:8.1f} | {m['db_utilization']:7.1f}% | {status}")

print()
print("=" * 90)
print("NOTES")
print("=" * 90)
print(f"  PG max_connections available for API: {PG_AVAILABLE}")
print(f"  Total request budget (2 workers × 10K): {TOTAL_REQUEST_BUDGET:,}")
print(f"  Normal checkout sequential duration: {sum([13.0, 3.6, 56.0, 0.37]):.1f}s")
print(f"  Flash checkout sequential duration:  {sum([16.0, 2.9, 34.0, 0.003]):.1f}s")
print(f"  DB Util% < 80% = safe, 80-100% = risky, >100% = connection exhaustion")
