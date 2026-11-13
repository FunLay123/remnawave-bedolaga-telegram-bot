# Trial onboarding and payment resilience — design

## Intent

For a Telegram-only VPN bot, recover three currently silent drop-offs: trial users who never connect, payments that remain pending or cannot be verified, and users whose YooKassa/Heleket payment fails.

## Scope

1. **Trial onboarding through the existing Remnawave webhook.** Use `user.not_connected` and `user.first_connected`; do not add a polling service. Suppress duplicate nudges and suppress nudges after the first successful connection. Ensure the unified web server can start when the Remnawave webhook is the only enabled HTTP feature.
2. **Independent payment alerts.** Extend the existing `AutoPaymentVerificationService` to alert the admin about payments pending for at least 30 minutes and provider verification failures. Alerts must work when the general `ADMIN_NOTIFICATIONS_ENABLED` switch is false, while still requiring an admin chat and respecting the new payment-alert switch. Repeat a given payment alert no more than once per 6 hours.
3. **Failed-payment fallback.** For YooKassa `canceled`/`failed` and Heleket `cancel`/`fail`/`system_fail`, offer other enabled payment methods for the same amount. Do this proactively after provider-driven status updates and in the existing manual status-check screens. Reuse the amount-first `topup_amount|method|kopeks` callbacks. Exclude only the exact failed method; YooKassa card and YooKassa SBP remain valid alternatives for each other.

## Non-goals

- No cabinet, Mini App, Web API, checkout, pricing, crediting, referral, tariff, or panel-sync redesign.
- No database migration and no new payment provider.
- No new trial polling worker or fallback notification scheduler.
- No Remnawave panel configuration, deployment, reverse-proxy, DNS, or secret changes performed by the agent.
- No expiry-notification migration from `MonitoringService` to Remnawave webhooks.

## Constraints

- Python 3.14, aiogram 3, SQLAlchemy async, existing Redis cache helpers.
- Telegram bot only; current payment methods are YooKassa, Heleket, and Telegram Stars.
- Use the existing `SentNotification` table for Remnawave connection markers; no schema changes.
- Payment alert defaults: enabled, pending age 30 minutes, repeat cooldown 6 hours, maximum 20 lines per digest.
- Payment alerts use a new `PAYMENTS` category and optional `ADMIN_NOTIFICATIONS_PAYMENTS_TOPIC_ID`; only this dedicated path bypasses the general admin-notification master switch.
- Failed-payment fallback deduplicates proactive messages per provider/payment for 24 hours; manual status screens may always render the fallback.
- Russian user-facing additions go only into `app/localization/locales/ru.json`.
- Follow TDD and run focused tests for each task.
- Append one changelog entry per logical task to `../claude-changes.md` outside the repository.
- Do not push. Do not change production configuration or contact external services.

## Acceptance criteria

- Repeated `user.not_connected` for the same subscription and hour threshold sends at most one nudge; after `user.first_connected`, no later not-connected nudge is sent.
- A failed nudge delivery does not create a false delivered marker.
- Remnawave-only webhook configuration satisfies the unified-server startup predicate.
- A payment at 29 minutes pending produces no alert; at 30 minutes it produces one. Provider check exceptions produce a sanitized alert without exception payloads or credentials.
- Repeated alerts for the same provider/payment/kind are suppressed for 6 hours, including when Redis is unavailable (single-process memory fallback).
- The dedicated payment alert reaches the configured admin chat with `ADMIN_NOTIFICATIONS_ENABLED=false`; other admin notification paths retain their existing gating.
- Failed YooKassa/Heleket payments show the same amount and only alternative methods; if there are no alternatives, the support/back path remains usable.
- Proactive fallback is idempotent for 24 hours and never runs for successful, pending, overpaid, or refunded payments.
- Focused tests pass, and the pre-existing affected suites remain green.
