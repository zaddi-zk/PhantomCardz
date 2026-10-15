#!/usr/bin/env bash
#
# PhantomCardz smoke test
# Runs the cookie flow, the header flow, and regression smokes against a
# running server, printing PASS/FAIL per check.
#
# Usage: tools/smoke.sh            (defaults to http://localhost:8000)
#        BASE=http://host tools/smoke.sh
#
set -u

BASE="${BASE:-http://localhost:8000}"
PY="$(cd "$(dirname "$0")/.." && pwd)/venv/bin/python"
JAR="$(mktemp)"
JAR2="$(mktemp)"
trap 'rm -f "$JAR" "$JAR2"' EXIT

PASS=0
FAIL=0
pass() { printf 'PASS  %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf 'FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }

jfield() { $PY -c "import sys,json;print(json.load(sys.stdin).get('$1',''))" 2>/dev/null; }

ADMIN_JSON='{"email":"admin@phantomcardz.cc","password":"Boss123!"}'

# wait for the server to be ready (start.sh may return before uvicorn binds)
for _ in $(seq 1 40); do
  curl -s -o /dev/null "$BASE/api/health" && break
  sleep 0.5
done

echo "== A) cookie flow (browser dialect) =="

# A1: login sets a session cookie
HEADERS="$(curl -s -i -c "$JAR" -X POST "$BASE/api/login" \
  -H 'Content-Type: application/json' -d "$ADMIN_JSON")"
if printf '%s' "$HEADERS" | grep -qi '^set-cookie: session='; then
  pass "A1 login sets 'set-cookie: session=...'"
else
  fail "A1 login did NOT set a session cookie"
fi

# A2: cookie authenticates /api/user
USER_JSON="$(curl -s -b "$JAR" "$BASE/api/user")"
if printf '%s' "$USER_JSON" | grep -q 'admin@phantomcardz.cc'; then
  pass "A2 GET /api/user with cookie returns user JSON"
else
  fail "A2 GET /api/user with cookie -> $USER_JSON"
fi

# A3: logout returns a message
LOGOUT_JSON="$(curl -s -b "$JAR" -c "$JAR" -X POST "$BASE/api/logout")"
if printf '%s' "$LOGOUT_JSON" | grep -q '"message":"Logged out"'; then
  pass "A3 POST /api/logout returns {message}"
else
  fail "A3 POST /api/logout -> $LOGOUT_JSON"
fi

# A4: after logout the cookie is dead (browser deletes it -> No auth)
AFTER_JSON="$(curl -s -b "$JAR" "$BASE/api/user")"
if printf '%s' "$AFTER_JSON" | grep -q '"message":"No auth"'; then
  pass "A4 GET /api/user after logout is {\"message\":\"No auth\"}"
else
  fail "A4 GET /api/user after logout -> $AFTER_JSON"
fi

echo "== B) header flow (dual dialect) =="
TOK="$(curl -s -X POST "$BASE/api/login" -H 'Content-Type: application/json' \
  -d "$ADMIN_JSON" | grep -o '"token":"[^"]*' | cut -d'"' -f4)"
if [ -n "$TOK" ] && curl -s "$BASE/api/user" -H "Authorization: Bearer $TOK" \
  | grep -q 'admin@phantomcardz.cc'; then
  pass "B1 Bearer header still authenticates /api/user"
else
  fail "B1 Bearer header flow broken"
fi

echo "== C) regression smokes =="

HEALTH="$(curl -s "$BASE/api/health")"
printf '%s' "$HEALTH" | grep -q '"ok":true' \
  && pass "C1 GET /api/health" || fail "C1 health -> $HEALTH"

PRODUCTS="$(curl -s "$BASE/api/products")"
printf '%s' "$PRODUCTS" | grep -q '"id":1' \
  && pass "C2 GET /api/products" || fail "C2 products -> ${PRODUCTS:0:80}"

# cookie-authed mutation: refresh the jar first (A3 logged out)
curl -s -o /dev/null -c "$JAR2" -X POST "$BASE/api/login" \
  -H 'Content-Type: application/json' -d "$ADMIN_JSON"
CASH="$(curl -s -X POST "$BASE/api/orders/cashapp" -b "$JAR2" \
  -H 'Content-Type: application/json' -d '{"amount":20}')"
printf '%s' "$CASH" | grep -q '"status":"pending"' \
  && pass "C3 POST /api/orders/cashapp with cookie" \
  || fail "C3 cashapp -> $CASH"

echo "== D) patched SPA contracts =="

RANK="$(curl -s "$BASE/api/user/rank" -H "Authorization: Bearer $TOK")"
printf '%s' "$RANK" | grep -q '"discountPct"' \
  && pass "D1 /api/user/rank exposes discountPct" \
  || fail "D1 rank -> $RANK"

curl -s -o /dev/null -X POST "$BASE/api/payments/crypto/create" \
  -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -d '{"amount":25,"currency":"BTC"}'
DID="$($PY -c "import urllib.request,json;r=urllib.request.Request('$BASE/api/deposits',headers={'Authorization':'Bearer $TOK'});print(json.load(urllib.request.urlopen(r))[0]['id'])")"
STATUS="$(curl -s "$BASE/api/payments/crypto/$DID/status" -H "Authorization: Bearer $TOK")"
printf '%s' "$STATUS" | grep -q '"status":"pending"' \
  && pass "D2 GET /api/payments/crypto/{id}/status" \
  || fail "D2 crypto status -> $STATUS"

echo
echo "-----------------------------------------"
printf 'TOTAL: %d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]