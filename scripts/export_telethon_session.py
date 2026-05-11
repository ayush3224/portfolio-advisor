"""Export an existing Telethon file session to a portable string.

Usage:
    python scripts/export_telethon_session.py [path/to/session]

Default session path: $TELETHON_SESSION_FILE (falls back to
/root/portfolio-advisor/portfolio_advisor_session). Telethon expects the
path WITHOUT the .session suffix.

After running, copy the printed string into .env as:
    TELETHON_SESSION_STRING=<paste here>

This is a one-time operation. The string then survives across machines and
container rebuilds; no phone-code prompt will fire on the VPS.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Ensure project root is importable when invoked as `python scripts/...`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402  (needs sys.path tweak)

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    print("ERROR: telethon not installed. Run: pip install telethon", file=sys.stderr)
    sys.exit(1)


async def main(session_path: str) -> int:
    if not config.TELETHON_API_ID or not config.TELETHON_API_HASH:
        print("ERROR: TELETHON_API_ID / TELETHON_API_HASH not set in env.",
              file=sys.stderr)
        return 1

    session_file = session_path + ".session"
    if not os.path.exists(session_file):
        print(f"ERROR: session file not found: {session_file}", file=sys.stderr)
        print("Hint: pass the path WITHOUT the .session suffix.", file=sys.stderr)
        return 1

    client = TelegramClient(
        session_path,
        int(config.TELETHON_API_ID),
        config.TELETHON_API_HASH,
    )
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: existing session is not authorised. Run an interactive\n"
              "       login first (Telethon will prompt for phone + code).",
              file=sys.stderr)
        await client.disconnect()
        return 1

    string = StringSession.save(client.session)
    await client.disconnect()

    print()
    print("=" * 64)
    print("TELETHON_SESSION_STRING (copy the line below into .env):")
    print("=" * 64)
    print(string)
    print("=" * 64)
    return 0


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else config.TELETHON_SESSION_FILE
    sys.exit(asyncio.run(main(path)))
