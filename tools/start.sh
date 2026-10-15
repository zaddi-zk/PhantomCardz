#!/bin/bash
# NOTE: the server runs as ".../venv/bin/python main.py"; its cmdline does not
# contain "PhantomCardz", so we match on the interpreter+script instead.
cd /home/milo/PhantomCardz
pkill -f "venv/bin/python main.py" 2>/dev/null; sleep 1
nohup ./venv/bin/python main.py > market.log 2>&1 < /dev/null &
sleep 2 && tail -3 market.log