#!/bin/bash
nohup /Users/dp/repo/.venv/bin/python3 /Users/dp/repo/claude-remote/daemon.py > /tmp/daemon.log 2>&1 &
echo $!