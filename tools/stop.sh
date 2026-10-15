#!/bin/bash
# NOTE: match the interpreter+script, not "PhantomCardz" (not in the cmdline).
pkill -f "venv/bin/python main.py"; sleep 1; echo "stopped"