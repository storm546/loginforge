#!/usr/bin/env bash
set -euo pipefail
python /app/mock_2captcha.py &
exec python /app/app.py
