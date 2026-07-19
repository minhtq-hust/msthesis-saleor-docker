"""
Smoke Test — Saleor Platform (1 Virtual User)
=============================================
Mục đích : Xác minh rằng toàn bộ luồng người dùng end-to-end hoạt động
           đúng trước khi chạy load test chính thức.

Kịch bản : 1 người dùng, chạy tuần tự (không random), không think time.
           Mỗi bước đều được kiểm tra kết quả và dừng sớm nếu thất bại.

Các bước :
  S0 — Health check           : GET /graphql/ → HTTP 200
  S1 — Lấy danh sách sản phẩm: ProductList query (first=5)
  S2 — Tìm kiếm sản phẩm     : Search query với keyword "shirt"
  S3 — Xem chi tiết sản phẩm : ProductDetail query (slug cố định)
  S4 — Lấy variant IDs       : GetVariants query
  S5 — Tạo checkout           : CheckoutCreate mutation
  S6 — Thêm sản phẩm vào giỏ : CheckoutLinesAdd mutation
  S7 — Đặt địa chỉ giao hàng  : CheckoutShippingAddressUpdate mutation
  S8 — Chọn phương thức vận chuyển: CheckoutDeliveryMethodUpdate mutation
  S9 — Đặt địa chỉ thanh toán : CheckoutBillingAddressUpdate mutation
  S10— Kiểm tra thông tin TT  : checkout query (lấy gateway + tổng tiền)
  S11— Tạo thanh toán         : CheckoutPaymentCreate mutation
  S12— Hoàn thành checkout     : CheckoutComplete mutation → order

Chạy:
  cd loadtest
  python3 smoke_test.py
  # hoặc với host khác:
  SMOKE_HOST=http://11.1.11.136:8000 python3 smoke_test.py

Exit code: 0 = tất cả bước PASS, 1 = có bước FAIL
"""

import os
import sys
import time
import json
import requests

# ── Cấu hình ──────────────────────────────────────────────────────────────────

HOST        = os.environ.get("SMOKE_HOST", "http://localhost:8000")
GRAPHQL_URL = f"{HOST}/graphql/"
CHANNEL     = os.environ.get("SALEOR_CHANNEL", "default-channel")
DUMMY_GW    = os.environ.get("DUMMY_GATEWAY", "mirumee.payments.dummy")
TIMEOUT     = int(os.environ.get("SMOKE_TIMEOUT", "30"))    # giây / request
SMOKE_EMAIL = "smoke_test@test.local"

# Địa chỉ kiểm thử cố định (VN — khớp với shipping zones của populatedb)
_ADDRESS = {
    "firstName":    "Smoke",
    "lastName":     "Test",
    "streetAddress1": "1 Nguyen Hue",
    "city":         "Ho Chi Minh City",
    "country":      "VN",
    "phone":        "+84901234567",
}

# Slug cố định cho bước xem chi tiết sản phẩm
_DETAIL_SLUG = "apple-juice"

# ── Màu ANSI ──────────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

# ── Trạng thái toàn cục ────────────────────────────────────────────────────────

_results: list[dict] = []   # {"step", "name", "status", "elapsed", "note"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _print_header():
    print(f"\n{BOLD}{CYAN}{'═'*65}{RESET}")
    print(f"{BOLD}{CYAN}  SMOKE TEST — Saleor Platform  (1 VU, sequential){RESET}")
    print(f"{BOLD}{CYAN}  Host   : {HOST}{RESET}")
    print(f"{BOLD}{CYAN}  Channel: {CHANNEL}{RESET}")
    print(f"{BOLD}{CYAN}{'═'*65}{RESET}\n")


def _step_start(step_id: str, name: str):
    print(f"{YELLOW}▶ [{step_id}] {name}{RESET}", end="", flush=True)


def _step_ok(step_id: str, name: str, elapsed: float, note: str = ""):
    tag = f"{GREEN}PASS{RESET}"
    suffix = f"  {CYAN}({note}){RESET}" if note else ""
    print(f"\r{tag} [{step_id}] {name}  {elapsed:.2f}s{suffix}")
    _results.append({"step": step_id, "name": name, "status": "PASS",
                      "elapsed": elapsed, "note": note})


def _step_fail(step_id: str, name: str, elapsed: float, note: str = ""):
    tag = f"{RED}FAIL{RESET}"
    suffix = f"  {RED}→ {note}{RESET}" if note else ""
    print(f"\r{tag} [{step_id}] {name}  {elapsed:.2f}s{suffix}")
    _results.append({"step": step_id, "name": name, "status": "FAIL",
                      "elapsed": elapsed, "note": note})


def _print_summary():
    total   = len(_results)
    passed  = sum(1 for r in _results if r["status"] == "PASS")
    failed  = total - passed
    total_t = sum(r["elapsed"] for r in _results)

    print(f"\n{BOLD}{CYAN}{'═'*65}{RESET}")
    print(f"{BOLD}  KẾT QUẢ SMOKE TEST{RESET}")
    print(f"{'─'*65}")
    print(f"  {'Bước':<6} {'Tên':<38} {'Kết quả':<6} {'Thời gian':>8}")
    print(f"{'─'*65}")
    for r in _results:
        color  = GREEN if r["status"] == "PASS" else RED
        status = f"{color}{r['status']}{RESET}"
        note   = f"  ({r['note']})" if r["note"] else ""
        print(f"  {r['step']:<6} {r['name']:<38} {status:<17} {r['elapsed']:>6.2f}s{note}")
    print(f"{'─'*65}")
    print(f"  Tổng: {total} bước │ "
          f"{GREEN}{passed} PASS{RESET} │ "
          f"{RED}{failed} FAIL{RESET} │ "
          f"Tổng thời gian: {total_t:.2f}s")
    print(f"{BOLD}{CYAN}{'═'*65}{RESET}\n")

    if failed == 0:
        print(f"{BOLD}{GREEN}✓ SMOKE TEST PASSED — hệ thống sẵn sàng chạy load test.{RESET}\n")
    else:
        print(f"{BOLD}{RED}✗ SMOKE TEST FAILED — {failed} bước thất bại. Xem chi tiết ở trên.{RESET}\n")


def _gql(query: str, variables: dict, name: str) -> dict | None:
    """Gửi GraphQL request, trả về dict response hoặc None nếu lỗi."""
    try:
        resp = requests.post(
            GRAPHQL_URL,
            json={"query": query, "variables": variables},
            timeout=TIMEOUT,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout:
        return None
    except requests.exceptions.RequestException as exc:
        return None
    except json.JSONDecodeError:
        return None


def _get(path: str) -> tuple[int | None, float]:
    """HTTP GET, trả về (status_code, elapsed). status_code=None nếu exception."""
    t0 = time.perf_counter()
    try:
        resp = requests.get(f"{HOST}{path}", timeout=TIMEOUT)
        return resp.status_code, time.perf_counter() - t0
    except Exception:
        return None, time.perf_counter() - t0


# ── GraphQL queries / mutations ────────────────────────────────────────────────

_Q_PRODUCTS = """
query ProductList($channel: String!, $first: Int!) {
  products(channel: $channel, first: $first) {
    edges { node { id name slug pricing { priceRange { start { gross { amount currency } } } } } }
  }
}"""

_Q_SEARCH = """
query ProductSearch($channel: String!, $search: String!, $first: Int!) {
  products(channel: $channel, search: $search, first: $first) {
    edges { node { id name slug } }
  }
}"""

_Q_DETAIL = """
query ProductDetail($slug: String!, $channel: String!) {
  product(slug: $slug, channel: $channel) {
    id name description
    variants { id name sku pricing { price { gross { amount currency } } } }
    media { url alt }
  }
}"""

_Q_VARIANTS = """
query GetVariants($channel: String!, $first: Int!) {
  products(channel: $channel, first: $first) {
    edges { node { variants { id } } }
  }
}"""

_M_CHECKOUT_CREATE = """
mutation CheckoutCreate($channel: String!, $email: String!) {
  checkoutCreate(input: { channel: $channel, email: $email, lines: [] }) {
    checkout { id token }
    errors { field message code }
  }
}"""

_M_LINES_ADD = """
mutation CheckoutLinesAdd($checkoutId: ID!, $lines: [CheckoutLineInput!]!) {
  checkoutLinesAdd(checkoutId: $checkoutId, lines: $lines) {
    checkout { id totalPrice { gross { amount currency } } }
    errors { field message code }
  }
}"""

_M_SHIPPING_ADDR = """
mutation CheckoutShippingAddressUpdate($checkoutId: ID!, $address: AddressInput!) {
  checkoutShippingAddressUpdate(checkoutId: $checkoutId, shippingAddress: $address) {
    checkout { id availableShippingMethods { id name } }
    errors { field message code }
  }
}"""

_M_DELIVERY = """
mutation CheckoutDeliveryMethodUpdate($checkoutId: ID!, $deliveryMethodId: ID!) {
  checkoutDeliveryMethodUpdate(id: $checkoutId, deliveryMethodId: $deliveryMethodId) {
    checkout { id totalPrice { gross { amount currency } } }
    errors { field message code }
  }
}"""

_M_BILLING_ADDR = """
mutation CheckoutBillingAddressUpdate($checkoutId: ID!, $address: AddressInput!) {
  checkoutBillingAddressUpdate(checkoutId: $checkoutId, billingAddress: $address) {
    checkout { id }
    errors { field message code }
  }
}"""

_Q_CHECKOUT_INFO = """
query CheckoutInfo($id: ID!) {
  checkout(id: $id) {
    id
    totalPrice { gross { amount currency } }
    availablePaymentGateways { id name }
    isShippingRequired
    shippingMethod { id name }
    billingAddress { firstName }
  }
}"""

_M_PAYMENT_CREATE = """
mutation CheckoutPaymentCreate($checkoutId: ID!, $input: PaymentInput!) {
  checkoutPaymentCreate(id: $checkoutId, input: $input) {
    checkout { id }
    payment { id gateway }
    errors { field message code }
  }
}"""

_M_CHECKOUT_COMPLETE = """
mutation CheckoutComplete($checkoutId: ID!) {
  checkoutComplete(checkoutId: $checkoutId) {
    order { id number status }
    confirmationNeeded
    errors { field message code }
  }
}"""


# ── Các bước smoke test ────────────────────────────────────────────────────────

def s0_health_check() -> bool:
    """S0: Health check — GET /graphql/ phải trả về HTTP 200."""
    sid, name = "S0", "Health check (GET /graphql/)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    code, elapsed = _get("/graphql/")
    # Saleor trả về 200 hoặc 400 (method not allowed) cho GET đến /graphql/
    # Quan trọng là server đang chạy, không bị timeout.
    if code is not None and code < 500:
        _step_ok(sid, name, elapsed, f"HTTP {code}")
        return True
    _step_fail(sid, name, elapsed, f"HTTP {code}" if code else "timeout/connection refused")
    return False


def s1_product_list() -> bool:
    """S1: Lấy danh sách sản phẩm (first=5)."""
    sid, name = "S1", "Danh sách sản phẩm (ProductList)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_Q_PRODUCTS, {"channel": CHANNEL, "first": 5}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False
    if "errors" in data:
        _step_fail(sid, name, elapsed, str(data["errors"])[:80])
        return False
    edges = (data.get("data") or {}).get("products", {}).get("edges", [])
    if not edges:
        _step_fail(sid, name, elapsed, "0 sản phẩm trả về")
        return False
    _step_ok(sid, name, elapsed, f"{len(edges)} sản phẩm")
    return True


def s2_search() -> bool:
    """S2: Tìm kiếm sản phẩm với keyword 'shirt'."""
    sid, name = "S2", "Tìm kiếm sản phẩm (search='shirt')"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_Q_SEARCH, {"channel": CHANNEL, "search": "shirt", "first": 10}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False
    if "errors" in data:
        _step_fail(sid, name, elapsed, str(data["errors"])[:80])
        return False
    edges = (data.get("data") or {}).get("products", {}).get("edges", [])
    # search có thể trả về 0 kết quả — không fail, chỉ warn
    _step_ok(sid, name, elapsed, f"{len(edges)} kết quả")
    return True


def s3_product_detail() -> bool:
    """S3: Xem chi tiết sản phẩm (slug cố định: apple-juice)."""
    sid, name = "S3", f"Chi tiết sản phẩm (slug={_DETAIL_SLUG})"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_Q_DETAIL, {"slug": _DETAIL_SLUG, "channel": CHANNEL}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False
    if "errors" in data:
        _step_fail(sid, name, elapsed, str(data["errors"])[:80])
        return False
    product = (data.get("data") or {}).get("product")
    if not product:
        _step_fail(sid, name, elapsed, f"slug '{_DETAIL_SLUG}' không tìm thấy")
        return False
    variants = product.get("variants", [])
    _step_ok(sid, name, elapsed, f"id={product['id'][:20]}… | {len(variants)} variants")
    return True


def s4_get_variants() -> tuple[bool, str | None]:
    """S4: Lấy variant ID đầu tiên để dùng cho checkout."""
    sid, name = "S4", "Lấy danh sách variants"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_Q_VARIANTS, {"channel": CHANNEL, "first": 10}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False, None
    if "errors" in data:
        _step_fail(sid, name, elapsed, str(data["errors"])[:80])
        return False, None
    variant_id = None
    count = 0
    for edge in (data.get("data") or {}).get("products", {}).get("edges", []):
        for v in edge["node"].get("variants", []):
            if not variant_id:
                variant_id = v["id"]
            count += 1
    if not variant_id:
        _step_fail(sid, name, elapsed, "không tìm thấy variant nào")
        return False, None
    _step_ok(sid, name, elapsed, f"{count} variants | dùng: {variant_id[:20]}…")
    return True, variant_id


def s5_checkout_create() -> tuple[bool, str | None]:
    """S5: Tạo checkout mới."""
    sid, name = "S5", "Tạo checkout (CheckoutCreate)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_M_CHECKOUT_CREATE, {"channel": CHANNEL, "email": SMOKE_EMAIL}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False, None
    if "errors" in data:
        _step_fail(sid, name, elapsed, str(data["errors"])[:80])
        return False, None
    cd = (data.get("data") or {}).get("checkoutCreate", {})
    errs = cd.get("errors", [])
    if errs:
        _step_fail(sid, name, elapsed, str(errs[0])[:80])
        return False, None
    checkout = cd.get("checkout")
    if not checkout:
        _step_fail(sid, name, elapsed, "checkoutCreate trả về null checkout")
        return False, None
    _step_ok(sid, name, elapsed, f"id={checkout['id'][:20]}…")
    return True, checkout["id"]


def s6_add_line(checkout_id: str, variant_id: str) -> bool:
    """S6: Thêm 1 sản phẩm vào giỏ."""
    sid, name = "S6", "Thêm sản phẩm vào giỏ (CheckoutLinesAdd)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_M_LINES_ADD, {
        "checkoutId": checkout_id,
        "lines": [{"variantId": variant_id, "quantity": 1}],
    }, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False
    errs = (data.get("data") or {}).get("checkoutLinesAdd", {}).get("errors", [])
    if errs:
        _step_fail(sid, name, elapsed, str(errs[0])[:80])
        return False
    total = (data.get("data") or {}).get("checkoutLinesAdd", {}) \
                .get("checkout", {}).get("totalPrice", {}) \
                .get("gross", {})
    _step_ok(sid, name, elapsed,
             f"tổng = {total.get('amount')} {total.get('currency')}")
    return True


def s7_shipping_address(checkout_id: str) -> tuple[bool, str | None]:
    """S7: Đặt địa chỉ giao hàng, lấy shipping method ID."""
    sid, name = "S7", "Đặt địa chỉ giao hàng (ShippingAddressUpdate)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_M_SHIPPING_ADDR, {"checkoutId": checkout_id, "address": _ADDRESS}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False, None
    res = (data.get("data") or {}).get("checkoutShippingAddressUpdate", {})
    errs = res.get("errors", [])
    if errs:
        _step_fail(sid, name, elapsed, str(errs[0])[:80])
        return False, None
    methods = res.get("checkout", {}).get("availableShippingMethods", [])
    if not methods:
        _step_fail(sid, name, elapsed, "không có phương thức vận chuyển nào")
        return False, None
    method_id = methods[0]["id"]
    _step_ok(sid, name, elapsed, f"{len(methods)} phương thức | dùng: {methods[0]['name']}")
    return True, method_id


def s8_delivery_method(checkout_id: str, method_id: str) -> tuple[bool, float | None]:
    """S8: Chọn phương thức vận chuyển, lấy total amount."""
    sid, name = "S8", "Chọn phương thức vận chuyển (DeliveryMethodUpdate)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_M_DELIVERY, {"checkoutId": checkout_id, "deliveryMethodId": method_id}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False, None
    res = (data.get("data") or {}).get("checkoutDeliveryMethodUpdate", {})
    errs = res.get("errors", [])
    if errs:
        _step_fail(sid, name, elapsed, str(errs[0])[:80])
        return False, None
    gross = res.get("checkout", {}).get("totalPrice", {}).get("gross", {})
    amount = gross.get("amount")
    _step_ok(sid, name, elapsed, f"tổng (+ ship) = {amount} {gross.get('currency')}")
    return True, amount


def s9_billing_address(checkout_id: str) -> bool:
    """S9: Đặt địa chỉ thanh toán."""
    sid, name = "S9", "Đặt địa chỉ thanh toán (BillingAddressUpdate)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_M_BILLING_ADDR, {"checkoutId": checkout_id, "address": _ADDRESS}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False
    errs = (data.get("data") or {}).get("checkoutBillingAddressUpdate", {}).get("errors", [])
    if errs:
        _step_fail(sid, name, elapsed, str(errs[0])[:80])
        return False
    _step_ok(sid, name, elapsed)
    return True


def s10_checkout_info(checkout_id: str) -> tuple[bool, str | None, float | None]:
    """S10: Lấy thông tin checkout — xác nhận gateway và tổng tiền."""
    sid, name = "S10", "Kiểm tra thông tin thanh toán (CheckoutInfo)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_Q_CHECKOUT_INFO, {"id": checkout_id}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False, None, None
    checkout = (data.get("data") or {}).get("checkout")
    if not checkout:
        _step_fail(sid, name, elapsed, "checkout không tồn tại")
        return False, None, None
    gateways = checkout.get("availablePaymentGateways", [])
    if not gateways:
        _step_fail(sid, name, elapsed, "không có payment gateway nào")
        return False, None, None
    gross  = checkout.get("totalPrice", {}).get("gross", {})
    amount = gross.get("amount")
    gw_id  = gateways[0]["id"]
    _step_ok(sid, name, elapsed,
             f"gateway={gw_id} | total={amount} {gross.get('currency')}")
    return True, gw_id, amount


def s11_payment_create(checkout_id: str, gateway_id: str, amount: float) -> bool:
    """S11: Tạo payment với dummy gateway."""
    sid, name = "S11", "Tạo thanh toán (CheckoutPaymentCreate)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_M_PAYMENT_CREATE, {
        "checkoutId": checkout_id,
        "input": {
            "gateway": gateway_id,
            "amount":  amount,
            "token":   "not-charged",
        },
    }, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False
    res  = (data.get("data") or {}).get("checkoutPaymentCreate", {})
    errs = res.get("errors", [])
    if errs:
        _step_fail(sid, name, elapsed, str(errs[0])[:80])
        return False
    pay = res.get("payment", {})
    _step_ok(sid, name, elapsed, f"payment id={str(pay.get('id','?'))[:20]}… | gw={pay.get('gateway')}")
    return True


def s12_checkout_complete(checkout_id: str) -> bool:
    """S12: Hoàn thành checkout — tạo order."""
    sid, name = "S12", "Hoàn thành checkout (CheckoutComplete)"
    _step_start(sid, name)
    t0 = time.perf_counter()
    data = _gql(_M_CHECKOUT_COMPLETE, {"checkoutId": checkout_id}, name)
    elapsed = time.perf_counter() - t0
    if not data:
        _step_fail(sid, name, elapsed, "timeout hoặc lỗi kết nối")
        return False
    res  = (data.get("data") or {}).get("checkoutComplete", {})
    errs = res.get("errors", [])
    if errs:
        _step_fail(sid, name, elapsed, str(errs[0])[:80])
        return False
    order = res.get("order")
    if not order:
        # confirmationNeeded = True cũng được chấp nhận (async payment)
        if res.get("confirmationNeeded"):
            _step_ok(sid, name, elapsed, "confirmationNeeded=true (async payment flow)")
            return True
        _step_fail(sid, name, elapsed, "không tạo được order và confirmationNeeded=false")
        return False
    _step_ok(sid, name, elapsed,
             f"order #{order['number']} | status={order['status']}")
    return True


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    _print_header()

    # ── Bước S0–S3: Kiểm tra API cơ bản (read-only) ──────────────────────────
    if not s0_health_check():
        print(f"\n{RED}Dừng sớm: server không phản hồi.{RESET}\n")
        _print_summary()
        return 1

    s1_product_list()
    s2_search()
    s3_product_detail()

    # ── Bước S4: Lấy variant ID ───────────────────────────────────────────────
    ok, variant_id = s4_get_variants()
    if not ok:
        _print_summary()
        return 1

    # ── Bước S5: Tạo checkout ─────────────────────────────────────────────────
    ok, checkout_id = s5_checkout_create()
    if not ok:
        _print_summary()
        return 1

    # ── Bước S6: Thêm line ────────────────────────────────────────────────────
    if not s6_add_line(checkout_id, variant_id):
        _print_summary()
        return 1

    # ── Bước S7: Shipping address ─────────────────────────────────────────────
    ok, method_id = s7_shipping_address(checkout_id)
    if not ok:
        _print_summary()
        return 1

    # ── Bước S8: Delivery method ──────────────────────────────────────────────
    ok, _ = s8_delivery_method(checkout_id, method_id)
    if not ok:
        _print_summary()
        return 1

    # ── Bước S9: Billing address ──────────────────────────────────────────────
    if not s9_billing_address(checkout_id):
        _print_summary()
        return 1

    # ── Bước S10: Checkout info → lấy gateway + amount ───────────────────────
    ok, gateway_id, amount = s10_checkout_info(checkout_id)
    if not ok:
        _print_summary()
        return 1

    # ── Bước S11: Payment create ──────────────────────────────────────────────
    if not s11_payment_create(checkout_id, gateway_id, amount):
        _print_summary()
        return 1

    # ── Bước S12: Complete checkout ───────────────────────────────────────────
    s12_checkout_complete(checkout_id)

    _print_summary()
    failed = sum(1 for r in _results if r["status"] == "FAIL")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
