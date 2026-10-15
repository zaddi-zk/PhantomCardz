#!/bin/bash
BASE=${BASE:-http://localhost:8000}
PASS=0; FAIL=0
ok(){ echo "PASS  $1"; PASS=$((PASS+1)); }
bad(){ echo "FAIL  $1 -> $2"; FAIL=$((FAIL+1)); }
chk(){ n="$1"; b="$2"; w="$3"; if echo "$b" | grep -q "$w"; then ok "$n"; else bad "$n" "$b"; fi; }
CT='Content-Type: application/json'
ADMB=$(curl -s -c /tmp/fc_adm.jar -X POST $BASE/api/login -H "$CT" -d '{"email":"admin@phantomcardz.cc","password":"Boss123!"}')
chk "admin login" "$ADMB" '"token"'
TOK=$(echo "$ADMB" | grep -o '"token":"[^"]*' | cut -d'"' -f4)
curl -s -o /dev/null -X POST $BASE/api/register -H "$CT" -d '{"email":"fc_user@test.com","password":"FcUser12345"}'
UB=$(curl -s -X POST $BASE/api/login -H "$CT" -d '{"email":"fc_user@test.com","password":"FcUser12345"}')
chk "user login" "$UB" '"token"'
T2=$(echo "$UB" | grep -o '"token":"[^"]*' | cut -d'"' -f4)
A="Authorization: Bearer $TOK"; U="Authorization: Bearer $T2"
echo "--- PUBLIC ---"
chk "health"          "$(curl -s $BASE/api/health)" '"ok":true'
chk "announcements"   "$(curl -s $BASE/api/announcements)" '"content"'
chk "products"        "$(curl -s $BASE/api/products)" '"variants"'
chk "products/1"      "$(curl -s $BASE/api/products/1)" '"name"'
chk "variants"        "$(curl -s $BASE/api/variants)" 'stockCount'
chk "payment-methods" "$(curl -s $BASE/api/payment-methods)" 'cashapp'
chk "features"        "$(curl -s $BASE/api/settings/features)" 'checker'
chk "manual-payments" "$(curl -s $BASE/api/site-settings/manual-payments)" 'cashapp'
chk "min-deposits"    "$(curl -s $BASE/api/site-settings/min-deposits)" 'cashapp'
chk "card-bases"      "$(curl -s $BASE/api/card-bases)" 'stockCount'
chk "stock"           "$(curl -s $BASE/api/stock)" 'itemsInStock'
echo "--- AUTH GATES ---"
chk "no-auth user"    "$(curl -s $BASE/api/user)" '"message"'
chk "non-admin dash"  "$(curl -s $BASE/api/admin/dashboard -H "$U")" '"message"'
echo "--- USER FLOWS ---"
chk "user me"         "$(curl -s $BASE/api/user -H "$U")" '"email"'
chk "rank"            "$(curl -s $BASE/api/user/rank -H "$U")" 'discountPct'
chk "orders list"     "$(curl -s $BASE/api/orders -H "$U")" ']'
chk "orders export"   "$(curl -s $BASE/api/orders/export -H "$U")" '"content"'
chk "wallet txs"      "$(curl -s $BASE/api/wallet/transactions -H "$U")" ']'
chk "deposits list"   "$(curl -s $BASE/api/deposits -H "$U")" ']'
chk "user cards"      "$(curl -s $BASE/api/user/cards -H "$U")" ']'
chk "games history"   "$(curl -s $BASE/api/games/history -H "$U")" ']'
chk "seller me"       "$(curl -s $BASE/api/seller/me -H "$U")" '"status"'
chk "vouches"         "$(curl -s $BASE/api/vouches -H "$U")" ']'
chk "support list"    "$(curl -s $BASE/api/support -H "$U")" ']'
chk "tg link-code"    "$(curl -s $BASE/api/telegram/daily-rewards/link-code -H "$U")" '"code"'
echo "--- MONEY-INTEGRITY (glitch probes) ---"
chk "negative dice rejected"  "$(curl -s -X POST $BASE/api/games/dice -H "$U" -H "$CT" -d '{"amount":-5,"guessOver":50}')" '"Invalid amount"'
chk "negative cashapp rej"    "$(curl -s -X POST $BASE/api/orders/cashapp -H "$U" -H "$CT" -d '{"amount":-20}')" '"Invalid amount"'
BAL0=$(curl -s $BASE/api/user -H "$U" | grep -o '"balance":[0-9.]*' | cut -d: -f2)
CODE="FC$(date +%s)"
curl -s -o /dev/null -X POST $BASE/api/admin/codes -H "$A" -H "$CT" -d "{\"code\":\"$CODE\",\"value\":7}"
chk "redeem 1st"      "$(curl -s -X POST $BASE/api/wallet/redeem -H "$U" -H "$CT" -d "{\"code\":\"$CODE\"}")" '"redeemed":7'
chk "redeem 2nd blocked" "$(curl -s -X POST $BASE/api/wallet/redeem -H "$U" -H "$CT" -d "{\"code\":\"$CODE\"}")" '"message"'
DEP=$(curl -s -X POST $BASE/api/orders/cashapp -H "$U" -H "$CT" -d '{"amount":10}')
chk "deposit create"  "$DEP" '"reference"'
DID=$(echo "$DEP" | grep -o '"status":"pending"' >/dev/null && curl -s $BASE/api/deposits -H "$U" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
chk "deposit approve" "$(curl -s -X POST $BASE/api/admin/deposits/$DID/approve -H "$A")" '"ok":true'
chk "deposit dbl-appr blocked" "$(curl -s -X POST $BASE/api/admin/deposits/$DID/approve -H "$A")" '"message"'
BAL1=$(curl -s $BASE/api/user -H "$U" | grep -o '"balance":[0-9.]*' | cut -d: -f2)
if awk "BEGIN{exit !($BAL1 > $BAL0 + 6.9)}"; then ok "balance credited exactly once ($BAL0 -> $BAL1)"; else bad "balance drift" "$BAL0 -> $BAL1"; fi
echo "--- GAMES + CARDS ---"
chk "dice ok"        "$(curl -s -X POST $BASE/api/games/dice -H "$U" -H "$CT" -d '{"amount":1,"guessOver":50}')" '"roll"'
chk "spin ok"        "$(curl -s -X POST $BASE/api/games/spin -H "$U" -H "$CT" -d '{"amount":1}')" '"reels"'
chk "plinko ok"      "$(curl -s -X POST $BASE/api/games/plinko -H "$U" -H "$CT" -d '{"amount":1}')" '"bucket"'
M=$(curl -s -X POST $BASE/api/games/mines -H "$U" -H "$CT" -d '{"amount":1,"mines":3,"action":"start"}')
chk "mines start"    "$M" '"gameId"'
GID=$(echo "$M" | grep -o '"gameId":[0-9]*' | cut -d: -f2)
chk "mines pick"    "$(curl -s -X POST $BASE/api/games/mines -H "$U" -H "$CT" -d "{\"amount\":1,\"mines\":3,\"action\":\"pick\",\"tile\":0,\"gameId\":$GID}")" '"tile"'
chk "mines cashout" "$(curl -s -X POST $BASE/api/games/mines -H "$U" -H "$CT" -d "{\"amount\":1,\"mines\":3,\"action\":\"cashout\",\"gameId\":$GID}")" '"payout"'
CC=$(curl -s -X POST $BASE/api/cards -H "$U" -H "$CT" -d '{"baseId":1}')
chk "card buy"      "$CC" '"number"'
CID=$(echo "$CC" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
chk "checker"      "$(curl -s -X POST $BASE/api/checker/check -H "$U" -H "$CT" -d "{\"cardId\":$CID}")" '"valid"'
chk "card detail"   "$(curl -s $BASE/api/cards/$CID -H "$U")" '"number"'
echo "--- ADMIN ---"
chk "dashboard"     "$(curl -s $BASE/api/admin/dashboard -H "$A")" '"users"'
chk "users"         "$(curl -s $BASE/api/admin/users -H "$A")" '"email"'
chk "orders"        "$(curl -s $BASE/api/admin/orders -H "$A")" '"email"'
chk "deposits"      "$(curl -s $BASE/api/admin/deposits -H "$A")" '"email"'
chk "products"      "$(curl -s $BASE/api/admin/products -H "$A")" '"name"'
chk "cards"         "$(curl -s $BASE/api/admin/cards -H "$A")" '"number"'
chk "codes"         "$(curl -s $BASE/api/admin/codes -H "$A")" '"code"'
chk "discounts"     "$(curl -s $BASE/api/admin/discount-codes -H "$A")" '"percent"'
chk "ann config"    "$(curl -s $BASE/api/admin/announcements/config -H "$A")" 'bannerColor'
chk "adm features"  "$(curl -s $BASE/api/admin/settings/features -H "$A")" 'checker'
chk "integrations"   "$(curl -s $BASE/api/admin/integrations/status -H "$A")" 'telegram'
NP=$(curl -s -X POST $BASE/api/admin/products -H "$A" -H "$CT" -d '{"name":"FC Probe","description":"qa"}')
chk "product create" "$NP" '"ok":true'
NPID=$(echo "$NP" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
NV=$(curl -s -X POST $BASE/api/admin/products/$NPID/variants -H "$A" -H "$CT" -d '{"name":"FC V","price":1}')
chk "variant create" "$NV" '"ok":true'
VID=$(echo "$NV" | grep -o '"id":[0-9]*' | head -1 | cut -d: -f2)
chk "bulk stock"    "$(curl -s -X POST $BASE/api/admin/stock/bulk -H "$A" -H "$CT" -d "{\"variantId\":$VID,\"contents\":\"a@x.com:p1\nb@x.com:p2\"}")" '"added":2'
O=$(curl -s -X POST $BASE/api/orders -H "$U" -H "$CT" -d "{\"variantId\":$VID,\"quantity\":1}")
chk "order flow"    "$O" '"items"'
chk "del product"   "$(curl -s -X DELETE $BASE/api/admin/products/$NPID -H "$A")" '"ok":true'
printf '4111111111111111|12|2028|123\n' > /tmp/fc_lic.txt
chk "license-file" "$(curl -s -X POST "$BASE/api/admin/stock/license-file?baseId=1" -H "$A" -F "file=@/tmp/fc_lic.txt")" '"added":1'
echo "--- WORKER ---"
curl -s -o /dev/null -X PATCH $BASE/api/admin/users/2 -H "$A" -H "$CT" -d '{"rank":"worker"}'
chk "worker bases"  "$(curl -s $BASE/api/worker/card-bases -H "$A")" 'stockCount'
chk "worker orders" "$(curl -s $BASE/api/worker/orders -H "$A")" '"email"'
echo "--- RESULTS ---"
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "ALL GREEN" || echo "FAILURES PRESENT"