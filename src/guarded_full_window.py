"""Separate CP6.3 entry. Fail closed; no live authorization or runnable backend."""
from polymarket_bot.recorder.full_window_guard import main

if __name__ == '__main__':
    raise SystemExit(main())
