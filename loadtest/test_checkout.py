#!/usr/bin/env python3
"""
Debug script: Test the full Saleor checkout flow step-by-step.
Identifies exactly which step fails and why.
"""
import requests
import json
import time
import sys

API_URL = "http://localhost:8000/graphql/"
CHANNEL = "default-channel"
TIMEOUT = 30  # seconds per request

def gql(query, variables, label):
    print(f"\n{'='*60}")
    print(f"STEP: {label}")
    print(f"{'='*60}")
    start = time.time()
    try:
        r = requests.post(API_URL, json={"query": query, "variables": variables}, timeout=TIMEOUT)
        elapsed = time.time() - start
        data = r.json()
        print(f"Status: {r.status_code}")
        print(f"Time: {elapsed:.2f}s")
        print(f"Response: {json.dumps(data, indent=2)}")
        
        # Check for GraphQL errors
        if "errors" in data:
            print(f"*** GRAPHQL ERRORS: {data['errors']}")
        
        return data
    except requests.exceptions.Timeout:
        elapsed = time.time() - start
        print(f"*** TIMEOUT after {elapsed:.2f}s")
        return None
    except Exception as e:
        elapsed = time.time() - start
        print(f"*** ERROR after {elapsed:.2f}s: {e}")
        return None


# Step 0: Get variants
print("\n" + "="*60)
print("STEP 0: Get available variants")
print("="*60)
data = gql(
    """query GetVariants($channel: String!, $first: Int!) {
      products(channel: $channel, first: $first) {
        edges { node { id name variants { id name sku } } }
      }
    }""",
    {"channel": CHANNEL, "first": 5},
    "Get Variants"
)
if not data:
    sys.exit(1)

variant_id = None
for edge in data["data"]["products"]["edges"]:
    for v in edge["node"]["variants"]:
        variant_id = v["id"]
        print(f"  Selected variant: {v['name']} (id={v['id']}, sku={v['sku']})")
        break
    if variant_id:
        break

if not variant_id:
    print("*** No variants found!")
    sys.exit(1)


# Step 1: Create checkout
data = gql(
    """mutation CheckoutCreate($channel: String!, $email: String!) {
      checkoutCreate(input: { channel: $channel, email: $email, lines: [] }) {
        checkout { id token }
        errors { field message code }
      }
    }""",
    {"channel": CHANNEL, "email": "debug@test.local"},
    "1. Create Checkout"
)
if not data:
    sys.exit(1)

checkout_data = data.get("data", {}).get("checkoutCreate", {})
errors = checkout_data.get("errors", [])
if errors:
    print(f"*** Checkout creation errors: {errors}")
    sys.exit(1)

checkout = checkout_data.get("checkout")
if not checkout:
    print("*** No checkout returned!")
    sys.exit(1)

checkout_id = checkout["id"]
print(f"  Checkout ID: {checkout_id}")


# Step 2: Add line
data = gql(
    """mutation CheckoutLinesAdd($checkoutId: ID!, $lines: [CheckoutLineInput!]!) {
      checkoutLinesAdd(checkoutId: $checkoutId, lines: $lines) {
        checkout { id totalPrice { gross { amount currency } } }
        errors { field message code }
      }
    }""",
    {"checkoutId": checkout_id, "lines": [{"variantId": variant_id, "quantity": 1}]},
    "2. Add Line"
)
if not data:
    print("*** Add line timed out or failed")
    sys.exit(1)

add_errors = data.get("data", {}).get("checkoutLinesAdd", {}).get("errors", [])
if add_errors:
    print(f"*** Add line errors: {add_errors}")
    sys.exit(1)


# Step 3: Shipping address
data = gql(
    """mutation CheckoutShippingAddressUpdate($checkoutId: ID!, $address: AddressInput!) {
      checkoutShippingAddressUpdate(checkoutId: $checkoutId, shippingAddress: $address) {
        checkout { id
          availableShippingMethods { id name price { amount currency } }
          shippingMethod { id name }
        }
        errors { field message code }
      }
    }""",
    {
        "checkoutId": checkout_id,
        "address": {
            "firstName": "Perf", "lastName": "Test",
            "streetAddress1": "123 Le Loi",
            "city": "Ho Chi Minh City",
            "country": "VN", "phone": "+84901234567",
        },
    },
    "3. Set Shipping Address"
)
if not data:
    print("*** Shipping address timed out or failed")
    sys.exit(1)

ship_errors = data.get("data", {}).get("checkoutShippingAddressUpdate", {}).get("errors", [])
if ship_errors:
    print(f"*** Shipping errors: {ship_errors}")

# Check available shipping methods
checkout_after_ship = data.get("data", {}).get("checkoutShippingAddressUpdate", {}).get("checkout", {})
available_methods = checkout_after_ship.get("availableShippingMethods", [])
current_method = checkout_after_ship.get("shippingMethod")
print(f"  Available shipping methods: {available_methods}")
print(f"  Current shipping method: {current_method}")

# Step 3b: If shipping method not set, try to set one
if available_methods and not current_method:
    method_id = available_methods[0]["id"]
    print(f"  --> Setting shipping method to: {method_id}")
    data = gql(
        """mutation CheckoutDeliveryMethodUpdate($checkoutId: ID!, $deliveryMethodId: ID!) {
          checkoutDeliveryMethodUpdate(id: $checkoutId, deliveryMethodId: $deliveryMethodId) {
            checkout { id shippingMethod { id name } }
            errors { field message code }
          }
        }""",
        {"checkoutId": checkout_id, "deliveryMethodId": method_id},
        "3b. Set Delivery Method"
    )
    if data:
        dm_errors = data.get("data", {}).get("checkoutDeliveryMethodUpdate", {}).get("errors", [])
        if dm_errors:
            print(f"*** Delivery method errors: {dm_errors}")

# Step 3c: Set billing address too
data = gql(
    """mutation CheckoutBillingAddressUpdate($checkoutId: ID!, $address: AddressInput!) {
      checkoutBillingAddressUpdate(checkoutId: $checkoutId, billingAddress: $address) {
        checkout { id }
        errors { field message code }
      }
    }""",
    {
        "checkoutId": checkout_id,
        "address": {
            "firstName": "Perf", "lastName": "Test",
            "streetAddress1": "123 Le Loi",
            "city": "Ho Chi Minh City",
            "country": "VN", "phone": "+84901234567",
        },
    },
    "3c. Set Billing Address"
)


# Step 4: Complete checkout (without payment — dummy gateway)
# First, check what payment gateways are available
data = gql(
    """query CheckoutPaymentInfo($id: ID!) {
      checkout(id: $id) {
        id
        totalPrice { gross { amount currency } }
        availablePaymentGateways { id name currencies config { field value } }
        isShippingRequired
        shippingMethod { id name }
        billingAddress { firstName lastName }
        shippingAddress { firstName lastName }
      }
    }""",
    {"id": checkout_id},
    "4a. Check Payment Info"
)

if data:
    checkout_info = data.get("data", {}).get("checkout", {})
    gateways = checkout_info.get("availablePaymentGateways", [])
    total = checkout_info.get("totalPrice", {}).get("gross", {})
    print(f"  Total: {total}")
    print(f"  Available gateways: {gateways}")
    print(f"  Shipping required: {checkout_info.get('isShippingRequired')}")
    print(f"  Shipping method: {checkout_info.get('shippingMethod')}")
    print(f"  Billing address: {checkout_info.get('billingAddress')}")
    print(f"  Shipping address: {checkout_info.get('shippingAddress')}")

    # Try creating a payment if there's a gateway
    if gateways:
        gateway_id = gateways[0]["id"]
        amount = total.get("amount", 0)
        currency = total.get("currency", "USD")
        print(f"  --> Creating payment with gateway {gateway_id}, amount={amount} {currency}")
        data = gql(
            """mutation CheckoutPaymentCreate($checkoutId: ID!, $input: PaymentInput!) {
              checkoutPaymentCreate(id: $checkoutId, input: $input) {
                checkout { id }
                payment { id gateway }
                errors { field message code }
              }
            }""",
            {
                "checkoutId": checkout_id,
                "input": {
                    "gateway": gateway_id,
                    "amount": amount,
                    "token": "not-charged",
                }
            },
            "4b. Create Payment"
        )
        if data:
            pay_errors = data.get("data", {}).get("checkoutPaymentCreate", {}).get("errors", [])
            if pay_errors:
                print(f"*** Payment errors: {pay_errors}")

# Step 5: Complete checkout
data = gql(
    """mutation CheckoutComplete($checkoutId: ID!) {
      checkoutComplete(checkoutId: $checkoutId) {
        order { id number status }
        confirmationNeeded
        errors { field message code }
      }
    }""",
    {"checkoutId": checkout_id},
    "5. Complete Checkout"
)
if data:
    complete_errors = data.get("data", {}).get("checkoutComplete", {}).get("errors", [])
    order = data.get("data", {}).get("checkoutComplete", {}).get("order")
    if complete_errors:
        print(f"\n*** CHECKOUT COMPLETE ERRORS: {complete_errors}")
    if order:
        print(f"\n✓ ORDER CREATED: {order}")
    else:
        print(f"\n*** NO ORDER CREATED")

print("\n" + "="*60)
print("CHECKOUT FLOW TEST COMPLETE")
print("="*60)
