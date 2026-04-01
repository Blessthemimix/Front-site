# Changelog

## 0.1.0

- Refactored single-script bot into modules under `bot_app`.
- Added async FastAPI verification service with:
  - start endpoint (`/verify/start`)
  - finalize endpoint (`/verify/finalize`)
  - minimal HTML start form (`/`)
- Added one-time Discord link confirmation command (`/linkcode`) to bind web verification to a Discord account.
- Added queue-based role assignment: web app writes `pending_role_assignments`, bot worker applies roles.
- Added secure internal role assignment endpoint (`/internal/role-assignment`) protected by `X-Webhook-Secret`.
- Added sqlite schema initialization for users, claims, challenges, and assignment queue.
- Added role mapping via JSON config and environment-driven secrets.
- Added tests for verification logic, web flow, and helper logic.
- Added lint/test CI workflow and Docker deployment files.

## Behavioral notes

- **Compatibility mode implemented:** `VERIFICATION_MODE=rank_digit_count` reproduces original role mapping behavior from legacy `bot.py` (`len(str(global_rank))`).
- Additional modes are available:
  - `last_digit_of_userid`
  - `sum_of_digits_mod_X` (with `DIGIT_MODULUS`)
- If you want strict old behavior, keep `VERIFICATION_MODE=rank_digit_count`.
