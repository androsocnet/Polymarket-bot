"""Windows-only single-trial public BOOK_ONLY guard entry point."""
from polymarket_bot.recorder.live_guard import main


if __name__ == '__main__':
    raise SystemExit(main())
