"""
Synthetic Workload — Saleor Performance Tuning
===============================================
Methodology  : Controlled Experiment — Wohlin et al. (2012)
Locality     : Zipfian(α, N=32) — Breslau et al. (1999, IEEE INFOCOM)
Composition  : Coverage decision — Feitelson (2015) §3.4
Arrival      : Poisson process — think time ~ Exponential(μ)

CCU/VU Relationship (Little's Law):
  CCU = VU × W_resp / (W_resp + W_think)
  Normal:    ratio is stable across measurement window → any window valid
  FlashSale: ratio shifts as W_resp grows under load → measure at peak plateau

Configuration:
  All tunables are read from environment variables (see .env).
  Think time controls CCU/VU ratio — lower think time → higher CCU.

Usage:
  # Load environment, then run:
  source .env

  # Scenario A — Normal Load
  SCENARIO=normal locust -f locustfile.py NormalLoadUser \\
    --users 100 --spawn-rate 7 --run-time 12m --host http://localhost:8000

  # Scenario B — Flash Sale (LoadTestShape controls VU over time)
  SCENARIO=flash_sale locust -f locustfile.py FlashSaleUser \\
    --host http://localhost:8000

Measurement windows:
  Normal    → minutes 2–12  (after ramp; ratio is stable, use any 5-min slice)
  FlashSale → minutes 4–9   (peak plateau; capture peak CCU/VU behavior)

Runs per scenario: N=3. Report median(P95). Discard run if error_rate > 1%.
"""

import os
import math
import random
import time
import threading
import numpy as np
from locust import HttpUser, task, LoadTestShape, events

# ── Scenario selection ─────────────────────────────────────────────────────────

SCENARIO = os.environ.get("SCENARIO", "normal")   # "normal" | "flash_sale"

# ── Seed configuration ─────────────────────────────────────────────────────────
# Product slugs queried from DB: SELECT slug FROM product_product ORDER BY slug;
# Total: 32 products (from Saleor populatedb)

CHANNEL    = os.environ.get("SALEOR_CHANNEL", "default-channel")
ZIPF_ALPHA = float(os.environ.get("ZIPF_ALPHA", "1.0"))

PRODUCT_SLUGS = [
    "apple-juice",
    "ascii-tee",
    "balance-trail-720",
    "banana-juice",
    "battle-tested-at-brands-like-lush",
    "bean-juice",
    "blue-hoodie",
    "blue-plimsolls",
    "blue-polygon-shirt",
    "carrot-juice",
    "cubes-fountain-tee",
    "dark-polygon-tee",
    "darko-polo",
    "dash-force",
    "dry-sunglasses",
    "enterprise-cloud-on-premises-tales",
    "gift-card",
    "gift-card-50",
    "gift-card-500",
    "grey-hoodie",
    "headless-omnichannel-commerce",
    "mighty-mug",
    "monokai-dimmed-sunnies",
    "own-your-stack-and-data",
    "pirates-beanie",
    "reversed-monotype-tee",
    "tactical-neck-warmer",
    "team-shirt",
    "the-dash-cushion",
    "white-hoodie",
    "white-parrot-cusion",
    "white-plimsolls",
]

N_PRODUCTS = len(PRODUCT_SLUGS)  # 32

KEYWORDS = [
    "shirt", "hoodie", "tee", "juice", "polo",
    "shoe", "mug", "cushion", "beanie", "sunglasses",
]

# ── Checkout constants ─────────────────────────────────────────────────────────
# Reusable address for shipping + billing (VN country for populatedb shipping zones)
_TEST_ADDRESS = {
    "firstName": "Perf", "lastName": "Test",
    "streetAddress1": "123 Le Loi",
    "city": "Ho Chi Minh City",
    "country": "VN", "phone": "+84901234567",
}
DUMMY_GATEWAY = os.environ.get("DUMMY_GATEWAY", "mirumee.payments.dummy")

# ── Zipfian locality weights ───────────────────────────────────────────────────
# P(k) = (1/k^α) / H(N,α)  — Breslau et al. (1999, IEEE INFOCOM)
# With N=32, α=1.0: E[rank] ≈ 8.0,  σ ≈ 8.4

_ranks   = np.arange(1, N_PRODUCTS + 1)
_weights = 1.0 / (_ranks ** ZIPF_ALPHA)
_weights /= _weights.sum()


def pick_slug() -> str:
    return PRODUCT_SLUGS[np.random.choice(N_PRODUCTS, p=_weights)]


# ── Think time helpers — Exponential(μ) for Poisson inter-arrival ─────────────

def _exp_think(mean_seconds: float):
    """Returns a wait_time callable: inter-arrival ~ Exp(1/mean_seconds)."""
    return lambda self: random.expovariate(1.0 / mean_seconds)


NORMAL_THINK_TIME     = float(os.environ.get("NORMAL_THINK_TIME", "4.0"))
FLASH_SALE_THINK_TIME = float(os.environ.get("FLASH_SALE_THINK_TIME", "1.0"))

# ── Session duration — realistic user session lifecycle ───────────────────────
# Each VU: connect → perform tasks for SESSION_DURATION → disconnect.
# Instead of killing the VU (which drops the concurrent user count),
# we simulate a disconnect by clearing cookies & closing TCP connections,
# then seamlessly starting a new session. This keeps the active VU count
# exactly at the target while accurately modeling session churn.
#
# Rationale:
#   Normal   : 60–180s (avg 2 min) — casual browsing session
#   FlashSale: 30–90s  (avg 1 min) — decisive, goal-oriented session

NORMAL_SESSION_MIN = float(os.environ.get("NORMAL_SESSION_MIN", "60"))
NORMAL_SESSION_MAX = float(os.environ.get("NORMAL_SESSION_MAX", "180"))
FLASH_SESSION_MIN  = float(os.environ.get("FLASH_SESSION_MIN", "30"))
FLASH_SESSION_MAX  = float(os.environ.get("FLASH_SESSION_MAX", "90"))

# ── GraphQL queries and mutations ──────────────────────────────────────────────

PRODUCT_LIST_QUERY = """
query ProductList($channel: String!, $first: Int!) {
  products(channel: $channel, first: $first) {
    edges {
      node { id name slug pricing { priceRange { start { gross { amount currency } } } } }
    }
  }
}"""

SEARCH_QUERY = """
query ProductSearch($channel: String!, $search: String!, $first: Int!) {
  products(channel: $channel, search: $search, first: $first) {
    edges { node { id name slug } }
  }
}"""

PRODUCT_DETAIL_QUERY = """
query ProductDetail($slug: String!, $channel: String!) {
  product(slug: $slug, channel: $channel) {
    id name description
    variants {
      id name sku
      pricing { price { gross { amount currency } } }
      attributes { attribute { name } values { name } }
    }
    media { url alt }
  }
}"""

CHECKOUT_CREATE = """
mutation CheckoutCreate($channel: String!, $email: String!) {
  checkoutCreate(input: { channel: $channel, email: $email, lines: [] }) {
    checkout { id token }
    errors { field message }
  }
}"""

CHECKOUT_LINES_ADD = """
mutation CheckoutLinesAdd($checkoutId: ID!, $lines: [CheckoutLineInput!]!) {
  checkoutLinesAdd(checkoutId: $checkoutId, lines: $lines) {
    checkout { id totalPrice { gross { amount currency } } }
    errors { field message }
  }
}"""

CHECKOUT_SHIPPING_UPDATE = """
mutation CheckoutShippingAddressUpdate($checkoutId: ID!, $address: AddressInput!) {
  checkoutShippingAddressUpdate(checkoutId: $checkoutId, shippingAddress: $address) {
    checkout { id availableShippingMethods { id } }
    errors { field message }
  }
}"""

CHECKOUT_DELIVERY_METHOD_UPDATE = """
mutation CheckoutDeliveryMethodUpdate($checkoutId: ID!, $deliveryMethodId: ID!) {
  checkoutDeliveryMethodUpdate(id: $checkoutId, deliveryMethodId: $deliveryMethodId) {
    checkout { id totalPrice { gross { amount currency } } }
    errors { field message }
  }
}"""

CHECKOUT_BILLING_UPDATE = """
mutation CheckoutBillingAddressUpdate($checkoutId: ID!, $address: AddressInput!) {
  checkoutBillingAddressUpdate(checkoutId: $checkoutId, billingAddress: $address) {
    checkout { id }
    errors { field message }
  }
}"""

CHECKOUT_PAYMENT_CREATE = """
mutation CheckoutPaymentCreate($checkoutId: ID!, $input: PaymentInput!) {
  checkoutPaymentCreate(id: $checkoutId, input: $input) {
    checkout { id }
    payment { id }
    errors { field message }
  }
}"""

CHECKOUT_COMPLETE = """
mutation CheckoutComplete($checkoutId: ID!) {
  checkoutComplete(checkoutId: $checkoutId) {
    order { id number status }
    errors { field message }
  }
}"""

GET_VARIANTS_QUERY = """
query GetVariants($channel: String!, $first: Int!) {
  products(channel: $channel, first: $first) {
    edges { node { variants { id } } }
  }
}"""

# ── Thread-safe variant ID pool ────────────────────────────────────────────────

_variant_ids: list[str] = []
_variant_lock = threading.Lock()


def _populate_variants(client) -> None:
    global _variant_ids
    with _variant_lock:
        if _variant_ids:
            return
        res = client.post(
            "/graphql/",
            json={"query": GET_VARIANTS_QUERY,
                  "variables": {"channel": CHANNEL, "first": 100}},
            name="_setup/variants",
        )
        try:
            for edge in res.json()["data"]["products"]["edges"]:
                for v in edge["node"]["variants"]:
                    _variant_ids.append(v["id"])
        except (KeyError, TypeError):
            pass


def _pick_variant() -> str | None:
    return random.choice(_variant_ids) if _variant_ids else None


# ── Shared task implementations ────────────────────────────────────────────────

def _browse(client) -> None:
    client.post("/graphql/", json={
        "query": PRODUCT_LIST_QUERY,
        "variables": {"channel": CHANNEL, "first": 20},
    }, name="browse/product_list")


def _search(client) -> None:
    client.post("/graphql/", json={
        "query": SEARCH_QUERY,
        "variables": {"channel": CHANNEL, "search": random.choice(KEYWORDS), "first": 10},
    }, name="search/products")


def _detail(client) -> None:
    client.post("/graphql/", json={
        "query": PRODUCT_DETAIL_QUERY,
        "variables": {"slug": pick_slug(), "channel": CHANNEL},
    }, name="detail/product")


def _checkout(client, email: str) -> None:
    # ── Step 1: Create checkout ──────────────────────────────────────────────
    res = client.post("/graphql/", json={
        "query": CHECKOUT_CREATE,
        "variables": {"channel": CHANNEL, "email": email},
    }, name="checkout/1_create")
    try:
        checkout = res.json()["data"]["checkoutCreate"]["checkout"]
        if not checkout:
            return
        checkout_id = checkout["id"]
    except (KeyError, TypeError):
        return

    # ── Step 2: Add line ─────────────────────────────────────────────────────
    variant_id = _pick_variant()
    if not variant_id:
        return

    res = client.post("/graphql/", json={
        "query": CHECKOUT_LINES_ADD,
        "variables": {"checkoutId": checkout_id,
                      "lines": [{"variantId": variant_id, "quantity": 1}]},
    }, name="checkout/2_add_line")
    try:
        if res.json()["data"]["checkoutLinesAdd"]["errors"]:
            return
    except (KeyError, TypeError):
        return

    # ── Step 3: Shipping address ─────────────────────────────────────────────
    # Response includes availableShippingMethods so we can pick one for step 4.
    res = client.post("/graphql/", json={
        "query": CHECKOUT_SHIPPING_UPDATE,
        "variables": {
            "checkoutId": checkout_id,
            "address": _TEST_ADDRESS,
        },
    }, name="checkout/3_shipping_addr")
    try:
        ship_data = res.json()["data"]["checkoutShippingAddressUpdate"]
        if ship_data["errors"]:
            return
        methods = ship_data["checkout"]["availableShippingMethods"]
        if not methods:
            return
        shipping_method_id = methods[0]["id"]
    except (KeyError, TypeError):
        return

    # ── Step 4: Delivery method ──────────────────────────────────────────────
    # Required by clean_checkout_shipping() → delivery_method must not be None.
    res = client.post("/graphql/", json={
        "query": CHECKOUT_DELIVERY_METHOD_UPDATE,
        "variables": {"checkoutId": checkout_id,
                      "deliveryMethodId": shipping_method_id},
    }, name="checkout/4_delivery")
    try:
        del_data = res.json()["data"]["checkoutDeliveryMethodUpdate"]
        if del_data["errors"]:
            return
        total_amount = del_data["checkout"]["totalPrice"]["gross"]["amount"]
    except (KeyError, TypeError):
        return

    # ── Step 5: Billing address ──────────────────────────────────────────────
    # Required by clean_billing_address() → billing_address must not be None.
    res = client.post("/graphql/", json={
        "query": CHECKOUT_BILLING_UPDATE,
        "variables": {
            "checkoutId": checkout_id,
            "address": _TEST_ADDRESS,
        },
    }, name="checkout/5_billing_addr")
    try:
        if res.json()["data"]["checkoutBillingAddressUpdate"]["errors"]:
            return
    except (KeyError, TypeError):
        return

    # ── Step 6: Create payment ───────────────────────────────────────────────
    # Required by clean_checkout_payment() → is_fully_paid() must be True.
    res = client.post("/graphql/", json={
        "query": CHECKOUT_PAYMENT_CREATE,
        "variables": {
            "checkoutId": checkout_id,
            "input": {
                "gateway": DUMMY_GATEWAY,
                "amount": total_amount,
                "token": "not-charged",
            },
        },
    }, name="checkout/6_payment")
    try:
        if res.json()["data"]["checkoutPaymentCreate"]["errors"]:
            return
    except (KeyError, TypeError):
        return

    # ── Step 7: Complete checkout ────────────────────────────────────────────
    client.post("/graphql/", json={
        "query": CHECKOUT_COMPLETE,
        "variables": {"checkoutId": checkout_id},
    }, name="checkout/7_complete")


# ── Scenario A: Normal Load User ───────────────────────────────────────────────
# Composition: Browse:Search:Detail:Checkout = 6:2:1:1
# Think time : Exponential(μ=4s) → CCU/VU ≈ 5% (stable)
# Session    : Uniform(60, 180)s — user browses, maybe buys, then leaves
# Control    : --users sets VU; CCU emerges from Little's Law

class NormalLoadUser(HttpUser):
    wait_time = _exp_think(NORMAL_THINK_TIME)

    def on_start(self) -> None:
        _populate_variants(self.client)
        self.email = f"perf_{random.randint(1, 999_999)}@test.local"
        self._session_start = time.monotonic()
        self._session_duration = random.uniform(
            NORMAL_SESSION_MIN, NORMAL_SESSION_MAX
        )

    def _check_session(self) -> None:
        """Simulate a disconnect and reconnect if session duration exceeded."""
        if time.monotonic() - self._session_start >= self._session_duration:
            # 1. Simulate disconnect (clear cookies, close TCP connections)
            self.client.cookies.clear()
            self.client.close()
            
            # Simulate the gap between a user leaving and a new user arriving
            time.sleep(random.uniform(0, 20))
            
            # 2. Start a new session
            self.email = f"perf_{random.randint(1, 999_999)}@test.local"
            self._session_start = time.monotonic()
            self._session_duration = random.uniform(
                NORMAL_SESSION_MIN, NORMAL_SESSION_MAX
            )
    @task(6)
    def browse(self) -> None:
        self._check_session()
        _browse(self.client)

    @task(2)
    def search(self) -> None:
        self._check_session()
        _search(self.client)

    @task(1)
    def detail(self) -> None:
        self._check_session()
        _detail(self.client)

    @task(1)
    def checkout(self) -> None:
        self._check_session()
        _checkout(self.client, self.email)


# ── Scenario B: Flash Sale User ────────────────────────────────────────────────
# Composition: Browse:Search:Detail:Checkout = 4:1:1:4
# Think time : Exponential(μ=1s) → CCU/VU ≈ 33% at healthy W_resp
#              As system saturates: W_resp ↑ → CCU/VU ↑ → non-linear pressure
# Session    : Uniform(30, 90)s — decisive, goal-oriented session
# Control    : FlashSaleShape controls VU over time (see below)

class FlashSaleUser(HttpUser):
    wait_time = _exp_think(FLASH_SALE_THINK_TIME)

    def on_start(self) -> None:
        _populate_variants(self.client)
        self.email = f"perf_{random.randint(1, 999_999)}@test.local"
        self._session_start = time.monotonic()
        self._session_duration = random.uniform(
            FLASH_SESSION_MIN, FLASH_SESSION_MAX
        )

    def _check_session(self) -> None:
        """Simulate a disconnect and reconnect if session duration exceeded."""
        if time.monotonic() - self._session_start >= self._session_duration:
            # 1. Simulate disconnect (clear cookies, close TCP connections)
            self.client.cookies.clear()
            self.client.close()
            
            # Simulate the gap between a user leaving and a new user arriving
            time.sleep(random.uniform(0, 10))
            
            # 2. Start a new session
            self.email = f"perf_{random.randint(1, 999_999)}@test.local"
            self._session_start = time.monotonic()
            self._session_duration = random.uniform(
                FLASH_SESSION_MIN, FLASH_SESSION_MAX
            )
    @task(4)
    def browse(self) -> None:
        self._check_session()
        _browse(self.client)

    @task(1)
    def search(self) -> None:
        self._check_session()
        _search(self.client)

    @task(1)
    def detail(self) -> None:
        self._check_session()
        _detail(self.client)

    @task(4)
    def checkout(self) -> None:
        self._check_session()
        _checkout(self.client, self.email)


# ── LoadTestShape — Flash Sale traffic curve ───────────────────────────────────
#
# VU count
# 500       ┌──────────────┐
#           │  peak (meas) │
# 200 ──────┘              └──────
# 100 ──┘                        └──
#   0 ─────────────────────────────► time (min)
#       0   2   4         9  11  14
#
# Measurement window = minutes 4–9 (peak plateau)
# Same shape must be used for BOTH baseline and post-tuning runs.
#
# Only active when SCENARIO=flash_sale.
# For SCENARIO=normal, use --users flag directly (no shape needed).
#
# NOTE: Class is conditionally defined so Locust only detects LoadTestShape
#       when SCENARIO=flash_sale. Otherwise --users/--spawn-rate/--run-time work.

def _parse_stage(env_key: str, default: str) -> tuple[int, int, int]:
    """Parse 'duration,users,spawn_rate' from env var."""
    raw = os.environ.get(env_key, default)
    parts = [int(x.strip()) for x in raw.split(",")]
    return (parts[0], parts[1], parts[2])


if SCENARIO == "flash_sale":
    class FlashSaleShape(LoadTestShape):
        """
        Flash Sale traffic curve.
        Stages represent realistic user arrival during a time-limited promotion:
          - Pre-sale (0–2 min):  early visitors arrive slowly (Poisson, low λ)
          - Ramp-up  (2–4 min):  sale announced, burst of arrivals
          - Peak     (4–9 min):  maximum concurrent users — MEASUREMENT WINDOW
          - Taper    (9–14 min): sale ends, users leave gradually

        All stages are configurable via FLASH_STAGE_1..5 env vars.
        Format: DURATION_SEC,TARGET_USERS,SPAWN_RATE
        """

        # (duration_seconds, target_users, spawn_rate)
        stages = [
            _parse_stage("FLASH_STAGE_1", "120,100,10"),   # 0–2 min
            _parse_stage("FLASH_STAGE_2", "240,300,30"),   # 2–4 min
            _parse_stage("FLASH_STAGE_3", "540,500,20"),   # 4–9 min  PEAK
            _parse_stage("FLASH_STAGE_4", "660,200,15"),   # 9–11 min
            _parse_stage("FLASH_STAGE_5", "840,50,5"),     # 11–14 min
        ]

        def tick(self):
            run_time = self.get_run_time()
            for duration, users, spawn_rate in self.stages:
                if run_time < duration:
                    return (users, spawn_rate)
            return None   # stop after all stages complete
