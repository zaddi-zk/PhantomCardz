#!/usr/bin/env bash
#
# PhantomCardz purchase-hardening checks.
#
# Proves the sellable-item path is glitch-proof: prices can't be negative/NaN,
# a discount can never invert an order into a credit, min-order quantities are
# enforced, an over-total discount clamps to $0.00, duplicate card ids aren't
# billed twice, and refunds are idempotent (credit exactly once).
#
# Usage: tools/hardening.sh   (defaults to http://localhost:8000)
#
set -u

BASE="${BASE:-http://localhost:8000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/venv/bin/python"
AJ="$(mktemp)"
trap 'rm -f "$AJ"' EXIT

PASS=0
FAIL=0
pass() { printf 'PASS  %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }

jget() { $PY -c "import sys,json;d=json.load(sys.stdin);
import functools
v=d
for k in '$1'.split('.'):
    v=v[int(k)] if k.isdigit() else v.get(k)
print('' if v is None else v)" 2>/dev/null; }

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
R="$(date +%s)$RANDOM"

for _ in $(seq 1 40); do curl -s -o /dev/null "$BASE/api/health" && break; sleep 0.5; done

curl -s -o /dev/null -c "$AJ" -X POST "$BASE/api/login" \
  -H 'Content-Type: application/json' -d '{"email":"admin@phantomcardz.cc","password":"Boss123!"}'
bal() { curl -s -b "$AJ" "$BASE/api/user" | $PY -c 'import sys,json;print(int(round(float(json.load(sys.stdin)["balance"]))))'; }

echo "== setup =="
PID="$(curl -s -b "$AJ" -X POST "$BASE/api/products" -H 'Content-Type: application/json' \
  -d "{\"name\":\"pczHard$R\"}" | jget id)"
V="$(curl -s -b "$AJ" -X POST "$BASE/api/variants" -H 'Content-Type: application/json' \
  -d "{\"productId\":$PID,\"name\":\"pczV$R\",\"price\":1000,\"minQuantity\":1}" | jget id)"
curl -s -o /dev/null -b "$AJ" -X POST "$BASE/api/stock" -H 'Content-Type: application/json' \
  -d "{\"variantId\":$V,\"rawContent\":\"a1\\na2\\na3\\na4\\na5\"}"
if [ -n "$PID" ] && [ -n "$V" ]; then pass "setup product=$PID variant=$V"; else fail "setup failed"; fi

echo "== A) price validation =="
C="$(code -b "$AJ" -X POST "$BASE/api/variants" -H 'Content-Type: application/json' \
  -d "{\"productId\":$PID,\"name\":\"bad\",\"price\":-5}")"
[ "$C" = "400" ] && pass "A1 negative variant price rejected (400)" || fail "A1 negative price -> $C"

C="$(code -b "$AJ" -X PATCH "$BASE/api/admin/variants/$V" -H 'Content-Type: application/json' -d '{"price":-1}')"
[ "$C" = "400" ] && pass "A2 negative variant PATCH rejected (400)" || fail "A2 negative PATCH -> $C"

echo "== B) discount creation guards =="
C="$(code -b "$AJ" -X POST "$BASE/api/admin/discount-codes" -H 'Content-Type: application/json' \
  -d "{\"code\":\"pczPERM$R\",\"type\":\"percent\",\"value\":150}")"
[ "$C" = "400" ] && pass "B1 percent>100 rejected (400)" || fail "B1 percent>100 -> $C"

C="$(code -b "$AJ" -X POST "$BASE/api/admin/discount-codes" -H 'Content-Type: application/json' \
  -d "{\"code\":\"pczNEG$R\",\"type\":\"percent\",\"value\":-10}")"
[ "$C" = "400" ] && pass "B2 negative percent rejected (400)" || fail "B2 negative percent -> $C"

curl -s -o /dev/null -b "$AJ" -X POST "$BASE/api/admin/discount-codes" -H 'Content-Type: application/json' \
  -d "{\"code\":\"pcz100$R\",\"type\":\"percent\",\"value\":100}"

echo "== C) 100% discount can not mint balance =="
B0="$(bal)"
RESP="$(curl -s -b "$AJ" -X POST "$BASE/api/orders" -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"variantId\":$V,\"quantity\":1}],\"code\":\"pcz100$R\"}")"
T="$(printf '%s' "$RESP" | jget total)"
B1="$(printf '%s' "$RESP" | jget newBalance)"
if [ "$T" = "0" ] && [ "$B1" = "$B0" ]; then
  pass "C1 100% off -> total 0, balance unchanged ($B0)"
else
  fail "C1 total=$T newBalance=$B1 (was $B0)"
fi

echo "== D) min-order quantity enforced =="
V2="$(curl -s -b "$AJ" -X POST "$BASE/api/variants" -H 'Content-Type: application/json' \
  -d "{\"productId\":$PID,\"name\":\"pczV2$R\",\"price\":100,\"minQuantity\":3}" | jget id)"
curl -s -o /dev/null -b "$AJ" -X POST "$BASE/api/stock" -H 'Content-Type: application/json' \
  -d "{\"variantId\":$V2,\"rawContent\":\"b1\\nb2\\nb3\"}"
C="$(code -b "$AJ" -X POST "$BASE/api/orders" -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"variantId\":$V2,\"quantity\":1}]}")"
[ "$C" = "400" ] && pass "D1 quantity<min rejected (400)" || fail "D1 below-min -> $C"
RESP="$(curl -s -b "$AJ" -X POST "$BASE/api/orders" -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"variantId\":$V2,\"quantity\":3}]}")"
OID="$(printf '%s' "$RESP" | jget id)"
[ "$(printf '%s' "$RESP" | jget total)" = "300" ] && pass "D2 exact min order ok (total 300)" || fail "D2 min order -> $RESP"

echo "== E) duplicate card ids billed once =="
BID="$(curl -s -b "$AJ" -X POST "$BASE/api/admin/card-bases" -H 'Content-Type: application/json' \
  -d "{\"name\":\"pczBase$R\",\"refundable\":false}" | jget id)"
curl -s -o /dev/null -b "$AJ" -X POST "$BASE/api/cards" -H 'Content-Type: application/json' \
  -d "{\"baseId\":$BID,\"price\":3,\"extras\":\"4111111111111111|12/30|123\"}"
CID="$(curl -s -b "$AJ" "$BASE/api/cards?baseId=$BID" | jget 0.id)"
B0="$(bal)"
RESP="$(curl -s -b "$AJ" -X POST "$BASE/api/orders" -H 'Content-Type: application/json' \
  -d "{\"cardIds\":[$CID,$CID]}")"
T="$(printf '%s' "$RESP" | jget total)"
B1="$(printf '%s' "$RESP" | jget newBalance)"
if [ "$T" = "300" ] && [ "$B1" = "$((B0 - 300))" ]; then
  pass "E1 duplicate card id charged once (total 300)"
else
  fail "E1 total=$T newBalance=$B1 (was $B0)"
fi

echo "== F) refund is idempotent =="
B0="$(bal)"
C1="$(code -b "$AJ" -X POST "$BASE/api/admin/orders/$OID/refund" -H 'Content-Type: application/json' -d '{}')"
B1="$(bal)"
C2="$(code -b "$AJ" -X POST "$BASE/api/admin/orders/$OID/refund" -H 'Content-Type: application/json' -d '{}')"
B2="$(bal)"
if [ "$C1" = "200" ] && [ "$B1" = "$((B0 + 300))" ] && [ "$C2" = "400" ] && [ "$B2" = "$B1" ]; then
  pass "F1 refund credited once, second refund rejected"
else
  fail "F1 refund c1=$C1 c2=$C2 bal $B0->$B1->$B2"
fi

echo "== G) idempotency key =="
V3="$(curl -s -b "$AJ" -X POST "$BASE/api/variants" -H 'Content-Type: application/json' \
  -d "{\"productId\":$PID,\"name\":\"pczV3$R\",\"price\":100,\"minQuantity\":1}" | jget id)"
curl -s -o /dev/null -b "$AJ" -X POST "$BASE/api/stock" -H 'Content-Type: application/json' \
  -d "{\"variantId\":$V3,\"rawContent\":\"c1\\nc2\"}"
B0="$(bal)"
KEY="pczIDEM-$R"
R1="$(curl -s -b "$AJ" -H "Idempotency-Key: $KEY" -X POST "$BASE/api/orders" -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"variantId\":$V3,\"quantity\":1}]}")"
R2="$(curl -s -b "$AJ" -H "Idempotency-Key: $KEY" -X POST "$BASE/api/orders" -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"variantId\":$V3,\"quantity\":1}]}")"
B1="$(bal)"
if [ "$(printf '%s' "$R1" | jget id)" = "$(printf '%s' "$R2" | jget id)" ] && [ "$B1" = "$((B0 - 100))" ]; then
  pass "G1 same key replays one order, charged once"
else
  fail "G1 idem ids=$(printf '%s' "$R1" | jget id)/$(printf '%s' "$R2" | jget id) bal $B0->$B1"
fi

echo "== H) card purchase idempotency =="
BID2="$(curl -s -b "$AJ" -X POST "$BASE/api/admin/card-bases" -H 'Content-Type: application/json' \
  -d "{\"name\":\"pczBase2$R\",\"refundable\":false}" | jget id)"
curl -s -o /dev/null -b "$AJ" -X POST "$BASE/api/cards" -H 'Content-Type: application/json' \
  -d "{\"baseId\":$BID2,\"price\":2,\"extras\":\"4222222222222222|11/30|456\"}"
CID2="$(curl -s -b "$AJ" "$BASE/api/cards?baseId=$BID2" | jget 0.id)"
B0="$(bal)"
C1="$(curl -s -b "$AJ" -H "Idempotency-Key: pczC-$R" -X POST "$BASE/api/cards/$CID2/purchase" -H 'Content-Type: application/json' -d '{}')"
C2="$(curl -s -b "$AJ" -H "Idempotency-Key: pczC-$R" -X POST "$BASE/api/cards/$CID2/purchase" -H 'Content-Type: application/json' -d '{}')"
B1="$(bal)"
if [ "$(printf '%s' "$C1" | jget id)" = "$(printf '%s' "$C2" | jget id)" ] && [ "$B1" = "$((B0 - 200))" ]; then
  pass "H1 same key replays card purchase, charged once"
else
  fail "H1 card idem ids=$(printf '%s' "$C1" | jget id)/$(printf '%s' "$C2" | jget id) bal $B0->$B1"
fi

echo
echo "-----------------------------------------"
printf 'TOTAL: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
