#!/bin/bash
BASE=${BASE:-http://localhost:8000}
CT='Content-Type: application/json'
PASS=0; FAIL=0
ok(){ echo "PASS  $1"; PASS=$((PASS+1)); }
bad(){ echo "FAIL  $1 -> $2"; FAIL=$((FAIL+1)); }
chk(){ n="$1"; b="$2"; w="$3"; if echo "$b" | grep -q "$w"; then ok "$n"; else bad "$n" "$b"; fi; }
# admin session
A=$(curl -s -X POST $BASE/api/login -H "$CT" -d '{"email":"admin@phantomcardz.cc","password":"Boss123!"}')
TOK=$(echo "$A" | grep -o '"token":"[^"]*' | cut -d'"' -f4)
chk "admin login" "$A" '"token"'
AH="Authorization: Bearer $TOK"
# fresh victim
TS=$(date +%s)
R=$(curl -s -X POST $BASE/api/register -H "$CT" -d "{\"email\":\"journey$TS@test.com\",\"password\":\"Journee123\"}")
chk "register" "$R" '"token"'
UT=$(echo "$R" | grep -o '"token":"[^"]*' | cut -d'"' -f4)
UH="Authorization: Bearer $UT"
BAL(){ curl -s $BASE/api/user -H "$UH" | grep -o '"balance":[0-9.-]*' | cut -d: -f2; }
B0=$(BAL)
# 1) deposit lifecycle
D=$(curl -s -X POST $BASE/api/orders/cashapp -H "$UH" -H "$CT" -d '{"amount":15}')
chk "deposit created" "$D" '"reference"'
DID=$(curl -s $BASE/api/deposits -H "$UH" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
chk "approve" "$(curl -s -X POST $BASE/api/admin/deposits/$DID/approve -H "$AH")" '"ok":true'
B1=$(BAL)
if awk "BEGIN{exit !($B1 > $B0 + 14.5)}"; then ok "balance credited after approval ($B0 -> $B1)"; else bad "balance" "$B0 -> $B1"; fi
# 2) card purchase = instant delivery
CB=$(curl -s -X POST $BASE/api/cards -H "$UH" -H "$CT" -d '{"baseId":1}')
chk "card delivered instantly" "$CB" '"cvv"'
chk "balance counted" "$CB" '"newBalance"'
CID=$(echo "$CB" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
chk "card in my cards" "$(curl -s $BASE/api/user/cards -H "$UH")" '"number"'
chk "checker" "$(curl -s -X POST $BASE/api/checker/check -H "$UH" -H "$CT" -d "{\"cardId\":$CID}")" '"valid"'
# 3) product purchase = items delivered
O=$(curl -s -X POST $BASE/api/orders -H "$UH" -H "$CT" -d '{"variantId":1,"quantity":1}')
chk "product ordered, items delivered" "$O" '"items"'
chk "export works" "$(curl -s $BASE/api/orders/export -H "$UH")" '"content"'
# 4) password reset END-TO-END (dev fallback)
F=$(curl -s -X POST $BASE/api/password/forgot -H "$CT" -d "{\"email\":\"journey$TS@test.com\"}")
chk "forgot issues code" "$F" '"message"'
CODE=$(echo "$F" | grep -o '"devCode":"[0-9]*' | grep -o '[0-9]*$')
if [ -z "$CODE" ]; then CODE=$(tail -50 market.log 2>/dev/null | grep -o "reset code.*" | tail -1 | grep -o '[0-9]\{6\}'); fi
if [ -n "$CODE" ]; then
  ok "code obtained ($CODE)"
  RS=$(curl -s -X POST $BASE/api/password/reset -H "$CT" -d "{\"email\":\"journey$TS@test.com\",\"code\":\"$CODE\",\"newPassword\":\"NewPass123\"}")
  chk "reset with code" "$RS" '"ok":true'
  RE=$(curl -s -X POST $BASE/api/login -H "$CT" -d "{\"email\":\"journey$TS@test.com\",\"password\":\"NewPass123\"}")
  chk "login with NEW password" "$RE" '"token"'
else
  bad "reset code retrieval" "$F"
fi
echo "--- JOURNEY RESULTS: PASS=$PASS FAIL=$FAIL ---"
[ "$FAIL" -eq 0 ] && echo "JOURNEY ALL GREEN"
