"""
tests/conftest.py
=================
Setup dummy environment untuk semua test.
Patch config.settings supaya tests bisa berjalan tanpa .env real.
"""
import os
import sys

# Dummy env vars sebelum modul apapun di-import
_DUMMY_ENV = {
    "TELEGRAM_API_ID": "12345678",
    "TELEGRAM_API_HASH": "abcdef1234567890abcdef1234567890",
    "TELEGRAM_PHONE": "+628123456789",
    "TELEGRAM_BOT_TOKEN": "dummy-test-bot-token-not-a-real-secret",
    "TELEGRAM_CONTROL_CHAT_ID": "-100123456789",
    "BITGET_API_KEY": "test_api_key",
    "BITGET_API_SECRET": "test_api_secret",
    "BITGET_PASSPHRASE": "test_passphrase",
    "SIGNAL_TOPIC_ID": "999",
    "DRY_RUN": "true",
    "BITGET_USE_SANDBOX": "true",
}

for k, v in _DUMMY_ENV.items():
    os.environ.setdefault(k, v)