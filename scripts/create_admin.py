#!/usr/bin/env python
"""Create or reset an admin user. 2FA (authenticator app + email code) is
enrolled on first login; it is required, not optional.

    .venv/bin/python scripts/create_admin.py <username> <email>
Prompts for the password (or reads ADMIN_PASSWORD from the environment for
non-interactive use)."""
from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect, migrate, one, transaction  # noqa: E402
from app.security import hash_password, password_policy_error  # noqa: E402


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    username, email = sys.argv[1].strip(), sys.argv[2].strip().lower()
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password (min 10 chars): ")
    err = password_policy_error(password)
    if err:
        print("Refused:", err)
        sys.exit(1)
    migrate()
    conn = connect()
    try:
        with transaction(conn):
            existing = one(conn, "SELECT id FROM admin_users WHERE username = ?", (username,))
            if existing:
                conn.execute("UPDATE admin_users SET email = ?, password_hash = ?, totp_secret = '', totp_enabled = 0, backup_codes = '[]', failed_attempts = 0, locked_until = NULL, is_active = 1 WHERE id = ?", (email, hash_password(password), existing["id"]))
                print(f"reset admin '{username}' (2FA re-enrolls on next login)")
            else:
                conn.execute("INSERT INTO admin_users(username, email, password_hash) VALUES (?, ?, ?)", (username, email, hash_password(password)))
                print(f"created admin '{username}'; 2FA enrolls on first login")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
