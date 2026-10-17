#!/usr/bin/env bash
#
# PhantomCardz seller-marketplace checks.
#
# Proves the whole seller lifecycle: admin provisions accounts -> seller applies
# -> admin approves -> seller lists a product (pending, hidden from buyers) ->
# admin reviews it live -> seller stocks it -> buyer pays -> seller accrues
# earnings in escrow -> funds release after the hold -> seller requests a payout
# -> admin approves + pays. Also covers the card-supply path and per-seller
# scoping (a non-seller is denied).
#
# Uses admin-provisioned accounts + bearer tokens so it never trips the public
# register/login throttles and is safe to re-run.
#
# Usage: tools/sellers.sh   (defaults to http://localhost:8000)
#
set -u

BASE="${BASE:-http://localhost:8000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$ROOT/venv/bin/python"

PASS=0
FAIL=0
pass() { printf 'PASS  %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }

jget() { $PY -c "import sys,json;d=json.load(sys.stdin)
v=d
for k in '$1'.split('.'):
    v=v[int(k)] if k.isdigit() else v.get(k)
print('' if v is None else v)" 2>/dev/null; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
R="$(date +%s)$RANDOM"
E="seller$R@t.co"
BUYER="sbuyer$R@t.co"

for _ in $(seq 1 40); do curl -s -o /dev/null "$BASE/api/health" && break; sleep 0.5; done

# --- admin + provisioned accounts (bearer tokens, no throttle) ---
TA="$(curl -s -X POST "$BASE/api/login" -H 'Content-Type: application/json' \
  -d '{"email":"admin@phantomcardz.cc","password":"Boss123!"}' | jget token)"
SELLER="$(curl -s -X POST "$BASE/api/admin/users" -H "Authorization: Bearer $TA" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$E\",\"password\":\"Passw0rd!\",\"username\":\"s$R\"}")"
TS="$(printf '%s' "$SELLER" | jget token)"
UID_S="$(printf '%s' "$SELLER" | jget id)"
BUY="$(curl -s -X POST "$BASE/api/admin/users" -H "Authorization: Bearer $TA" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$BUYER\",\"password\":\"Passw0rd!\",\"username\":\"b$R\"}")"
TB="$(printf '%s' "$BUY" | jget token)"
UID_B="$(printf '%s' "$BUY" | jget id)"
if [ -n "$TA" ] && [ -n "$TS" ] && [ -n "$TB" ]; then
  pass "setup seller=$UID_S buyer=$UID_B"
else
  fail "setup failed (admin=$TA seller=$TS buyer=$TB)"
fi
# Deterministic escrow window for the checks below.
curl -s -o /dev/null -H "Authorization: Bearer $TA" -X POST "$BASE/api/admin/settings/payout-hold" \
  -H 'Content-Type: application/json' -d '{"hours":72}'

echo "== A) apply + approve =="
curl -s -o /dev/null -H "Authorization: Bearer $TS" -X POST "$BASE/api/seller/apply" \
  -H 'Content-Type: application/json' -d '{"note":"I sell quality logs"}'
curl -s -o /dev/null -H "Authorization: Bearer $TA" -X POST "$BASE/api/admin/sellers/$UID_S/approve" \
  -H 'Content-Type: application/json' -d '{"commissionBps":1000}'
ST="$(curl -s -H "Authorization: Bearer $TS" "$BASE/api/seller/me" | jget status)"
[ "$ST" = "approved" ] && pass "A1 seller approved (status=approved)" || fail "A1 status=$ST"
C="$(code -H "Authorization: Bearer $TB" "$BASE/api/seller/earnings")"
[ "$C" = "403" ] && pass "A2 non-seller denied /api/seller/earnings (403)" || fail "A2 non-seller -> $C"

echo "== B) seller lists product (pending, hidden) =="
PID="$(curl -s -H "Authorization: Bearer $TS" -X POST "$BASE/api/seller/products" -H 'Content-Type: application/json' \
  -d "{\"name\":\"pczSP$R\",\"description\":\"seller log\"}" | jget id)"
PUB="$(curl -s "$BASE/api/products" | $PY -c "import sys,json;print('yes' if any(p.get('name')=='pczSP$R' for p in json.load(sys.stdin)) else 'no')")"
if [ -n "$PID" ] && [ "$PUB" = "no" ]; then pass "B1 pending listing hidden from buyers"; else fail "B1 pid=$PID public=$PUB"; fi
MINE="$(curl -s -H "Authorization: Bearer $TS" "$BASE/api/seller/products" | $PY -c "import sys,json;print('yes' if any(p.get('id')==$PID for p in json.load(sys.stdin)) else 'no')")"
[ "$MINE" = "yes" ] && pass "B2 seller sees own pending listing" || fail "B2 seller cannot see own listing"

echo "== C) admin review -> live =="
curl -s -o /dev/null -H "Authorization: Bearer $TA" -X PATCH "$BASE/api/admin/products/$PID/review" \
  -H 'Content-Type: application/json' -d '{"status":"active"}'
PUB="$(curl -s "$BASE/api/products" | $PY -c "import sys,json;print('yes' if any(p.get('name')=='pczSP$R' for p in json.load(sys.stdin)) else 'no')")"
[ "$PUB" = "yes" ] && pass "C1 approved listing is now public" || fail "C1 listing not public after review"

echo "== D) seller stocks; buyer pays =="
V="$(curl -s -H "Authorization: Bearer $TS" -X POST "$BASE/api/seller/variants" -H 'Content-Type: application/json' \
  -d "{\"productId\":$PID,\"name\":\"pczSV$R\",\"price\":1000,\"minQuantity\":1}" | jget id)"
curl -s -o /dev/null -H "Authorization: Bearer $TS" -X POST "$BASE/api/seller/stock" -H 'Content-Type: application/json' \
  -d "{\"variantId\":$V,\"rawContent\":\"log1\\nlog2\\nlog3\"}"
curl -s -o /dev/null -H "Authorization: Bearer $TA" -X POST "$BASE/api/admin/users/$UID_B/credit" \
  -H 'Content-Type: application/json' -d '{"amount":20,"note":"test"}'
RESP="$(curl -s -H "Authorization: Bearer $TB" -X POST "$BASE/api/orders" -H 'Content-Type: application/json' \
  -d "{\"items\":[{\"variantId\":$V,\"quantity\":1}]}")"
[ "$(printf '%s' "$RESP" | jget status)" = "completed" ] && pass "D1 buyer purchased (order $(printf '%s' "$RESP" | jget id))" || fail "D1 purchase -> $RESP"

echo "== E) escrow accrual (pending) =="
EARN="$(curl -s -H "Authorization: Bearer $TS" "$BASE/api/seller/earnings")"
P="$(printf '%s' "$EARN" | jget pendingCents)"
A="$(printf '%s' "$EARN" | jget availableCents)"
if [ "$P" = "900" ] && [ "$A" = "0" ]; then pass "E1 10% commission: pending 900, available 0"; else fail "E1 pending=$P available=$A (want 900/0)"; fi
C="$(code -H "Authorization: Bearer $TS" -X POST "$BASE/api/seller/payouts" -H 'Content-Type: application/json' -d '{"amount":9}')"
[ "$C" = "400" ] && pass "E2 payout of escrowed funds rejected (400)" || fail "E2 escrow payout -> $C"

echo "== F) release after hold + payout lifecycle =="
curl -s -o /dev/null -H "Authorization: Bearer $TA" -X POST "$BASE/api/admin/settings/payout-hold" \
  -H 'Content-Type: application/json' -d '{"hours":0}'
EARN="$(curl -s -H "Authorization: Bearer $TS" "$BASE/api/seller/earnings")"
A="$(printf '%s' "$EARN" | jget availableCents)"
W="$(printf '%s' "$EARN" | jget withdrawableCents)"
if [ "$A" = "900" ] && [ "$W" = "900" ]; then pass "F1 funds released (available 900)"; else fail "F1 available=$A withdrawable=$W"; fi
PO="$(curl -s -H "Authorization: Bearer $TS" -X POST "$BASE/api/seller/payouts" -H 'Content-Type: application/json' \
  -d '{"amount":9,"method":"cashapp","destination":"$testcash"}')"
POD="$(printf '%s' "$PO" | jget id)"
[ -n "$POD" ] && pass "F2 payout requested (id $POD)" || fail "F2 payout request -> $PO"
W2="$(curl -s -H "Authorization: Bearer $TS" "$BASE/api/seller/earnings" | jget withdrawableCents)"
[ "$W2" = "0" ] && pass "F3 requested funds reserved (withdrawable 0)" || fail "F3 withdrawable=$W2"
curl -s -o /dev/null -H "Authorization: Bearer $TA" -X POST "$BASE/api/admin/payouts/$POD/approve"
PS="$(curl -s -H "Authorization: Bearer $TA" -X POST "$BASE/api/admin/payouts/$POD/pay" | jget status)"
[ "$PS" = "paid" ] && pass "F4 admin paid the payout" || fail "F4 pay status=$PS"

echo "== G) card supply path =="
BID="$(curl -s -H "Authorization: Bearer $TS" -X POST "$BASE/api/seller/card-bases" -H 'Content-Type: application/json' \
  -d "{\"name\":\"pczSB$R\",\"price\":0}" | jget id)"
curl -s -o /dev/null -H "Authorization: Bearer $TS" -X POST "$BASE/api/seller/cards" -H 'Content-Type: application/json' \
  -d "{\"baseId\":$BID,\"price\":5,\"extras\":\"4111111111111111|12/30|123\"}"
CARD="$(curl -s -H "Authorization: Bearer $TB" "$BASE/api/cards?baseId=$BID")"
CID="$(printf '%s' "$CARD" | jget 0.id)"
MASK="$(printf '%s' "$CARD" | jget 0.cardNumber)"
A0="$(curl -s -H "Authorization: Bearer $TS" "$BASE/api/seller/earnings" | jget availableCents)"
curl -s -o /dev/null -H "Authorization: Bearer $TB" -X POST "$BASE/api/cards/$CID/purchase" -H 'Content-Type: application/json' -d '{}'
A1="$(curl -s -H "Authorization: Bearer $TS" "$BASE/api/seller/earnings" | jget availableCents)"
if [ -n "$A1" ] && [ "$((A1 - A0))" = "450" ]; then pass "G1 card sale earned 450 (500 - 10%)"; else fail "G1 card delta=$((A1 - A0)) (want 450; $A0->$A1)"; fi
case "$MASK" in *"••"*) pass "G2 buyer list masks PAN ($MASK)";; *) fail "G2 list not masked: $MASK";; esac

echo
echo "-----------------------------------------"
printf 'TOTAL: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
