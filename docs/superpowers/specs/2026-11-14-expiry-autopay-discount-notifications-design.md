# Autopay, expiry and winback notification redesign

**Date:** 2026-11-14  
**Status:** proposed design; implementation is not authorized yet  
**Audience:** a builder who has not participated in the notification audit

## 1. Context and intent

The current installation is bot-only, single-tariff, with a free trial and days_only referrals. Remnawave webhook user notifications are disabled and are not going to be enabled for reminders. The operator wants fewer repetitive notifications while preserving actionable expiry, successful renewal, and winback discount messages.

This design covers four related problems:

1. Autopay failure and expiry notifications can repeat the same warning during one subscription cycle.
2. The initial expired notification can be lost permanently after a transient Telegram failure.
3. Discount-wave cards and their post-claim screen do not match the current visual design.
4. Discount-wave pricing and lifetime rules contain reachable functional inconsistencies.

The implementation must be test-driven. Production code is not part of this document.

## 2. Goals

- Send at most one autopay-problem notification per subscription expiration cycle.
- Continue automatic balance attempts quietly after that notification.
- Always notify about a successful autopay renewal.
- Preserve the initial expired notification and make its delivery retryable.
- Keep both winback waves, but make their price, deadline, claim eligibility, and UI consistent.
- Reuse existing button registries, navigation callbacks, emoji helpers, and pricing rules.
- Preserve the current redesigned purchase menu, changing only the specified active-discount period-button presentation.

## 3. Non-goals and explicit exclusions

- Do not enable or redesign Remnawave webhook notifications.
- Do not change `NOTIFICATION_EXPIRED_1D_ENABLED=false`; the day-after-expiry reminder stays disabled.
- Do not change topup-triggered automatic extension of an expired subscription.
- Do not change premium-traffic checks or their five-minute default cadence.
- Do not change the initial `SUBSCRIPTION_EXPIRED` emoji policy except for delivery reliability.
- Do not add struck-through old prices or redesign the purchase menu beyond the explicitly specified active-discount period buttons.
- Do not change trial activation, paid-trial dormant branches, daily tariffs, cart/cabinet/miniapp flows, provider recurrent flows, legacy referral/autopay architecture, or simple-subscription mode.
- Do not edit locales other than `ru.json`. Python fallback strings may be kept consistent with the Russian template.
- Do not commit or push as part of this design work.

## 4. Current facts that constrain the design

### 4.1 Emoji gate

`app/services/monitoring_service.py::_send_message_with_logo` always calls `style_notification_text` without a notification type.

`app/utils/notification_emoji.py` currently:

- recognizes leading `⛔` and `⚠️` as warning glyphs;
- replaces them with custom emoji `5258382581375723416`;
- leaves the original Unicode glyph as the `<tg-emoji>` fallback;
- also reprocesses an existing leading `<tg-emoji>` according to its fallback glyph.

Therefore:

- the gate already works for `⛔ Подписка закончилась` and must remain unchanged for that message;
- merely putting a different `<tg-emoji>` in the wave template would not work, because the common gate would replace it again;
- the two discount-wave sends need a narrowly scoped gate opt-out.

### 4.2 Existing trial purchase button

The trial subscription menu builds «Купить подписку» with:

- `callback_data='subscription_upgrade'`;
- style-registry action `subscription:upgrade`;
- `style='primary'`;
- `icon_custom_emoji_id='5334552404736104314'`.

This is the button design to reuse for the wave success screen. No duplicate style definition is needed.

### 4.3 Selected discounted period-button UI

In the normal single-tariff purchase/renewal period menu, the no-discount appearance remains exactly as it is now.

When the user has any active applicable discount (promo group and/or personal offer), every period-selection button except navigation gets:

- `icon_custom_emoji_id='5335020345717973061'`, including the primary-period button whose usual custom moon icon is replaced for the discounted state;
- its existing primary style only for the configured primary period; other period buttons keep their existing non-primary style;
- the final discounted price, not the old price;
- the explicit combined discount label `Скидка {discount_percent}%`.

Selected visual form:

```text
[custom emoji 5335020345717973061] 30 дней — 135 ₽ · Скидка 20%
[custom emoji 5335020345717973061] 60 дней — 270 ₽ · Скидка 20%
[custom emoji 5335020345717973061] 90 дней — 405 ₽ · Скидка 20%
```

There is no struck-through old price, Unicode combining-strike workaround, or separate `🔥−N%` label. The back button keeps its current text, icon, callback, and style.

The percent is the effective sequential reduction from the undiscounted subtotal to the final price. If promo-group and personal-offer discounts stack, it reflects their combined result (for example, 10% followed by 20% is 28%), consistent with the actual charged amount.

This rule applies to the active purchase/renewal period-selection variants that share this menu pattern. It does not restyle unrelated bot buttons or the confirmation screen. When no applicable discount is active, the previous icons and price-only buttons return. The confirmation screen continues to show the final amount and balance transition.

## 5. Autopay and pre-expiry cadence

### 5.1 Considered options

**Option A — repair only the three-hour dedup.** Record the configured expiry warning after the merged card, but retain later warnings and the final failure reminder. This removes one duplicate but preserves too much noise.

**Option B — one autopay-problem message per cycle, recommended and selected.** Notify on the first eligible failure, make subsequent attempts quiet, remove the final failure reminder from the active cadence, and preserve success/expiry messages.

**Option C — no autopay-failure messages at all.** This is quieter but hides a correctable payment problem until service expires. Rejected.

### 5.2 Selected behavior for autopay enabled

A subscription expiration cycle is identified by the subscription and its current `end_date`.

1. On the first failed automatic balance attempt caused by **insufficient balance** that falls on an actual configured warning day, send one merged expiry/autopay-failure card.
2. `days_left` uses the existing `ceil(hours_left / 24)` convention. An actual warning day means `days_left` is a member of `AUTOPAY_WARNING_DAYS` and is allowed by the user's `subscription_expiry_days` preference. Membership in a numeric list is required; `days_left <= max(warning_days)` is not sufficient.
3. If `autopay_days_before` means no attempt occurs on the earliest configured day, no failure card is invented for that day. The first actual insufficient-balance attempt on another configured warning day can produce the single card.
4. After confirmed delivery, persist cycle-level autopay-attention coverage and the corresponding `SentNotification('expiring', warning_day)`. Neither marker depends on a three-hour timeout.
5. An explicit send failure does not mark the warning as covered; a later eligible failed attempt may retry delivery. An ambiguous timeout gets no immediate retry and is recorded as an unconfirmed terminal notification attempt for this cycle, prioritizing duplicate avoidance over another possibly duplicate card. It is not recorded as confirmed expiry delivery.
6. Until the cycle ends, further insufficient-balance attempts are quiet, including attempts on later configured warning days.
7. `AUTOPAY_FAILED` repeats, all generic `AUTOPAY_SHORT`/`AUTOPAY_OK` warnings, and `AUTOPAY_FAILED_FINAL` are not part of the active autopay-enabled cadence. Their settings and templates may remain dormant for compatibility; they must not produce messages in this installation.
8. `cause='charge_error'` and other non-balance/system failures do not consume the single user-facing warning and do not show a misleading topup card. They remain quiet operational failures with the existing logging/retry behavior.
9. A successful automatic renewal always sends `AUTOPAY_SUCCESS`, advances `end_date`, and starts a fresh notification cycle through the existing renewal/marker cleanup.
10. If every attempt fails, the normal `SUBSCRIPTION_EXPIRED` path still runs at expiration.
11. Topup after expiration and the existing automatic extension behavior remain unchanged.

The expected default sequence with insufficient balance is therefore:

- day 3: one merged failure/expiry message;
- days 2 and 1: quiet attempts, no failure message;
- final hours: quiet attempts, no final reminder;
- expiration: `SUBSCRIPTION_EXPIRED` if still unpaid;
- later successful autopay or topup-triggered extension: the corresponding existing success message(s).

### 5.3 Planned warnings for autopay-enabled users

For `subscription.autopay_enabled=True`, the generic planned-expiry path is skipped entirely, not merely deduped after a merged card. Autopay-enabled subscriptions use event-driven outcomes:

- no `SUBSCRIPTION_EXPIRING_AUTOPAY_OK` informational warning;
- no `SUBSCRIPTION_EXPIRING_AUTOPAY_SHORT` on any warning day, including day 1 after a covered day-3 failure;
- one eligible insufficient-balance merged card at most per cycle;
- `AUTOPAY_SUCCESS` after an actual successful charge;
- `SUBSCRIPTION_EXPIRED` after unsuccessful cycles.

If autopay is scheduled later than the earliest warning day, no balance-status message is sent before the attempt. The first actual insufficient-balance attempt on a configured warning day can still produce the single merged message.

Existing global and per-user notification gates remain applicable. A user who has disabled expiry notifications does not receive proactive merged expiry/failure cards; successful financial events remain governed by their existing rules.

### 5.4 Autopay disabled

For a paid subscription with autopay disabled, retain the configured manual expiry warnings (default days 3 and 1). They are the only proactive renewal reminders in this branch and use the existing per-day `SentNotification` markers.

If the user switches autopay off after a covered merged warning, the manual cadence may continue only for thresholds not already covered. It must not resend the same warning day.

### 5.5 Required autopay acceptance scenarios

- Failure on day 3, repeated failures on days 2 and 1: exactly one failure message before expiration and no generic `AUTOPAY_SHORT`/`AUTOPAY_OK`.
- Failure on day 3 followed by success on day 1: one failure message and one `AUTOPAY_SUCCESS`; no additional expiry warning.
- Boundary checks around 72/48/24 hours use the same `ceil(hours_left / 24)` day number and exact list membership in both the failure handler and tests.
- Failure on an unconfigured intermediate day: no merged warning for that day; a later first failure on an actual configured day may produce the single message.
- A late autopay schedule does not fabricate a failure before the first actual attempt.
- Explicit delivery failure may be retried on a later eligible failure; an ambiguous timeout cannot produce an immediate duplicate or a confirmed delivery marker.
- `charge_error` produces no topup card and does not consume the insufficient-balance warning.
- Jumping directly into a configured warning window does not bypass the one-message rule.
- Successful renewal starts a clean cycle and does not inherit failure suppression from the previous `end_date`.
- Autopay-off users retain their configured manual warnings.
- No behavior depends on Redis alone or on a three-hour suppression timestamp.

## 6. Reliable initial expired notification

### 6.1 Required behavior

The initial `SUBSCRIPTION_EXPIRED` send gets at most two additional attempts after the initial attempt: three attempts total.

- Attempt 1 occurs after the ACTIVE → EXPIRED transition, as now. Pending retry state is created only when this initial attempt actually occurs.
- Retry 1 and retry 2 occur on subsequent monitoring cycles, not in a blocking immediate retry loop.
- Retry recovery never infers pending state merely from an old EXPIRED row without a marker. Subscriptions that expired before deployment or outside this initial-attempt path are excluded, so no stale expiration messages are sent retroactively.
- A confirmed successful send records a persistent expired-notification marker for the current subscription cycle.
- Retry recovery considers only explicitly pending subscriptions that are still EXPIRED and belong to the same `end_date` cycle.
- If the user renews before a retry, the pending retry is cancelled; no stale expired message may be sent.
- Blocked/deactivated users and permanent Telegram errors are handled as unreachable and are not retried.
- After the third unsuccessful attempt, stop and leave an operational log entry.
- Renewal clears the expired marker and any pending retry state for the old cycle.

The retry state must survive a process restart. It may use existing persistent notification primitives; a new database schema is not required unless the implementation demonstrates that existing fields cannot represent it safely.

This reliability work covers the active Telegram bot path only. Email-only delivery is outside this bot-only design and keeps its existing behavior.

### 6.2 Delivery-result correctness

The expired path must distinguish confirmed delivery from a skipped/failed rich or classic send. In particular, the current `_send_message_with_logo` timeout returning `None` must not be mistaken for success.

Timeouts are ambiguous because Telegram might have accepted the message. Therefore:

- never perform an immediate duplicate-prone retry after a timeout;
- treat the attempt as unconfirmed and allow only the bounded later-cycle recovery;
- log the uncertainty rather than claiming guaranteed exactly-once Telegram delivery.

### 6.3 Required expired acceptance scenarios

- Initial success: one message, no retries.
- Failure, failure, then success: three sends total across cycles and no later duplicate.
- Three failures: no fourth attempt in that cycle.
- Failure followed by renewal before the next cycle: no retry and no stale expired message.
- Pre-existing EXPIRED rows at deployment, without explicitly created pending retry state: no retroactive messages.
- Rich timeout and classic timeout: neither is recorded as confirmed delivery; bounded recovery follows the same attempt limit.
- Blocked user: no retry loop.

## 7. Discount-wave visual design

These changes apply only to `expired_discount_wave2` and `expired_discount_wave3`. Other promo-offer types retain their existing behavior unless explicitly stated.

### 7.1 Gate opt-out

Add a typed, narrowly scoped way for `_send_message_with_logo` to pass notification identity to `style_notification_text`.

- Mark the two wave notification types as manually styled/excluded.
- Do not disable the common gate globally.
- Do not alter the current `⛔` → `5258382581375723416` handling of `SUBSCRIPTION_EXPIRED`.

### 7.2 Wave card

Both wave templates use the same layout with their own percent, price, and deadline:

```html
<tg-emoji emoji-id="5303040337059543238">⚠️</tg-emoji> <b>Скидка {percent}% на продление подписки{tariff_label}</b>

Активируйте и используйте до <b>{expires_at}</b>
Цена со скидкой за {period}: <b>{discounted_price}</b>

Заберите скидку и продлите доступ.
```

Buttons, one per row:

1. **Получить скидку** — `style='primary'`, `icon_custom_emoji_id='5235695112419303615'`, existing `claim_discount_{offer_id}` callback.
2. **Пополнить баланс** — no style, `icon_custom_emoji_id='5258500422393415126'`, existing `balance_topup` callback.

The phrase «Активируйте и используйте до» is intentional: both claiming and payment initiation are bounded by the same displayed deadline.

Rich and classic sends must preserve the custom emoji. The gate must not replace it with the common warning icon.

### 7.3 Successful claim popup

For these two waves only, replace the hardcoded popup emoji. The exact popup text is:

```text
Скидка активирована!
```

### 7.4 Successful claim message

```html
<tg-emoji emoji-id="5303040337059543238">🎉</tg-emoji> <b>Скидка {percent}% активирована!</b>

Она суммируется с другими скидками.
Примените её при оплате до <b>{expires_at}</b>.
```

Buttons, one per row:

1. **Купить подписку** — reuse the trial-menu `subscription:upgrade` style: primary, icon `5334552404736104314`, callback `subscription_upgrade`.
2. **Главное меню** — reuse `build_main_menu_button` and `success:main_menu`: primary, icon `5314310000531766389`, callback `back_to_menu`.

The raw leading Unicode emoji is stripped by the existing button styling helper where applicable. Do not create another main-menu callback or duplicate style-registry entries.

The success screen remains a separate message, as it is now; this design does not add editing/deletion of the original card.

## 8. Correct wave pricing and discount collisions

### 8.1 Displayed card price

The displayed wave price must be calculated as follows:

1. Calculate the renewal subtotal for the same default/autopay/shortest period resolved today, and show that period explicitly in `{period}` on the card.
2. Include tariff/device pricing and the user's promo-group discount.
3. Exclude any existing one-shot personal promo offer from this base calculation.
4. Apply the new wave percent exactly once.
5. Use the same sequential arithmetic as `PricingEngine` and the checkout flow.

The post-claim button opens the normal period menu; it does not force or change the default period. If the user selects the period shown on the card, the prices must match. Selecting another period legitimately produces another price under the existing tariff rules.

Example with a 1000 ₽ base and no promo-group discount:

- previously claimed wave 2: 10%;
- wave 3: 20%;
- card must show 800 ₽, not 720 ₽;
- claiming wave 3 replaces the 10% personal offer with 20%;
- checkout must also show 800 ₽.

Promo-group discounts still stack sequentially. For example, promo-group 10% followed by wave 20% produces a combined 28% reduction, not a nominal 30%.

### 8.2 Do not downgrade a better personal discount

If the user already has an active personal discount greater than or equal to the proposed wave percent:

- suppress creation/sending of that less-useful wave while the better discount is active;
- if a race still reaches the claim handler, do not overwrite the better discount;
- show an informational popup that a better or equal discount is already active;
- do not show the success purchase screen for a claim that changed nothing.

This rule does not merge two personal offers. It preserves the better one. Promo-group stacking remains unchanged.

### 8.3 Required pricing scenarios

- Existing 10% personal offer + new 20% wave: displayed and actual price both reflect 20%, never an accidental 28% personal-offer stack.
- Existing 50% personal offer + proposed 10% wave: the 50% offer is not downgraded.
- Promo-group 10% + wave 20%: group and offer stack once, sequentially.
- The card explicitly names its resolved period; when that same period is selected in single-tariff checkout, card and checkout use identical pricing inputs. Other user-selected periods retain their normal prices.

## 9. Discount validity and consumption

### 9.1 One deadline for claim and use

For a new wave claim:

- set `promo_offer_discount_expires_at` to the originating `DiscountOffer.expires_at`;
- do not restart a fresh 24-hour window at claim time;
- the deadline shown on the card, stored on the offer, stored on the user, and shown on the success screen must be identical.

Before the deadline, the discount behaves as a one-shot personal offer and stacks with promo-group discounts. After the deadline:

- `get_user_active_promo_discount_percent` returns zero;
- period buttons and confirmation return to the price without this personal offer, and the discounted-period custom icons/`Скидка N%` labels disappear when no other discount remains;
- old claim buttons cannot activate the offer;
- an already opened purchase screen must not gain a discount that was expired before its payment was initiated.

“Payment properly initiated” means an immediate balance debit confirmed while the discount is valid, or creation of a real provider invoice while it is valid. Such an invoice keeps its pricing snapshot through fulfillment.

A saved checkout, FSM draft, or topup intent is **not** payment initiation. For the existing topup-triggered expired-subscription extension, discount validity is evaluated when that extension actually executes after topup. An expired discount must not be revived by an old draft. This changes neither the availability nor the renewal behavior of topup-triggered extension; it only enforces the discount deadline. No saved-cart snapshot plumbing is added.

Consumption remains one-shot on the confirmed current payment paths. No provider-specific payment orchestration is broadened by this design.

### 9.2 Already claimed legacy wave discounts

Existing user rows can contain wave discounts with `promo_offer_discount_expires_at=NULL`. A bounded data backfill is required so the fix is not limited to new claims:

- for `expired_discount_wave2/3` sources with a matching claimed `DiscountOffer`, copy that offer's `expires_at`;
- if that deadline has passed, the existing expiry/cleanup path clears the stale personal discount;
- if no matching offer can be found, do not leave an indefinite wave discount and do not invent an original timestamp: grant one bounded 24-hour migration grace window from the backfill time and log the unmatched count without personal data.

No new user-facing migration notification is sent.

### 9.3 Required validity scenarios

- Claim one minute before the deadline: the discount still expires at the original card deadline, not 24 hours later.
- Claim after the deadline: rejected with no user discount changes.
- Valid discount at period selection: current discounted-price UI is shown.
- Same user after the deadline: the discounted-period icons, `Скидка N%` label, and personal discount disappear from pricing unless another discount remains.
- Successful payment consumes the discount once; later purchases use normal remaining discounts.
- A provider invoice created while valid keeps its snapshot; a saved checkout/topup intent does not preserve an expired discount for later automatic extension.
- Existing NULL-expiry wave rows are backfilled and cannot remain valid indefinitely.

## 10. Claim eligibility after renewal

An unclaimed offer whose `notification_type` is exactly `expired_discount_wave2` or `expired_discount_wave3` is valid only while its associated subscription is expired. This section does not apply to admin, broadcast, template, or other promo-offer types.

- On every successful current renewal/purchase path, deactivate outstanding unclaimed offers of these two types for that subscription as part of the successful operation.
- The claim handler applies this subscription-status guard only to these two types. Other offer types keep their existing eligibility rules.
- The wave claim handler must independently re-read the offer's subscription and reject the claim if the subscription is active or the offer has already been deactivated.
- The check/update must be safe against a renewal racing the claim; use the existing transaction/locking primitives rather than relying only on a stale callback message.
- A rejected post-renewal wave claim shows an informational popup and does not write `promo_offer_discount_*` fields or produce the success message.

If a user claimed while genuinely eligible and subsequently renewed by another route, the already claimed discount is not revoked retroactively; it remains usable only until its original deadline. The bug being removed is claiming an outstanding offer after renewal, not honoring a claim made while eligible.

Required scenarios:

- Expired subscription + valid offer: claim succeeds.
- Renewed subscription + old unclaimed offer: claim is rejected and no discount is stored.
- Renewal followed by a click on the old card in the same callback session: still rejected.
- Claimed before renewal, not yet consumed: remains bounded by the original deadline.
- A non-wave `DiscountOffer` attached to an active subscription: its existing claim behavior is unchanged.

## 11. Verification expectations

Implementation must start with failing regression tests for each corrected behavior, followed by minimal production changes and green reruns.

At minimum, cover:

- the full autopay cadence and marker cleanup across `end_date` cycles;
- the absence of the three-hour duplicate and final failure reminder;
- initial-expired retries, renewal cancellation, permanent errors, and rich/classic timeout outcomes;
- wave gate exclusion and preservation of the initial expired gate behavior;
- exact wave text/button icons, styles, callbacks, and row order;
- discounted period buttons: icon `5335020345717973061` on every period row, preserved primary-period style, final price plus `Скидка N%`, unchanged Back button, and restoration of the no-discount appearance after expiry;
- popup text without an emoji;
- success-screen deadline, trial-style purchase button, and working `back_to_menu` button;
- wave price correctness with previous personal offers and promo groups;
- no downgrade of a better personal discount;
- identical claim/use deadlines, post-expiry pricing, one-shot consumption, and legacy backfill;
- claim rejection after renewal and the claim/renewal race boundary;
- Russian locale validity and unchanged non-Russian locales.

Run the relevant service/handler/pricing suites and then the repository's full test command. Report unrelated platform/guard failures explicitly rather than calling the full suite green.

Also run Ruff on changed Python files and `git diff --check`. Regenerate the project structure reference if the repository generator tracks the new design document or other changed paths.

## 12. Likely code areas, not an implementation prescription

- `app/services/monitoring_service.py` — cadence, markers, expired retry, wave price/send policy.
- `app/utils/notification_emoji.py` — typed wave gate exclusions.
- `app/handlers/subscription/promo.py` — wave-specific claim UI and eligibility.
- `app/utils/promo_offer.py` — active-discount deadline behavior.
- `app/database/crud/discount_offer.py` and renewal completion paths — offer invalidation.
- `app/database/crud/user.py` or an equivalent existing migration/maintenance path — bounded legacy backfill.
- `app/services/pricing_engine.py` only if a clean override is needed to exclude the previous personal offer from the wave preview base.
- `app/localization/locales/ru.json` — Russian wave/success strings only.
- Focused service/handler/pricing tests.

The builder may choose a simpler implementation that satisfies the same behavior without expanding scope.

## 13. Decisions made by this design rather than explicitly dictated by the operator

The operator asked for the recommended variant without further questions. The following are design recommendations included for review:

- one autopay-problem message per entire expiration cycle, rather than one per warning day;
- the entire generic planned-warning path is skipped for autopay-enabled subscriptions, while autopay-off manual warnings remain;
- no final autopay-failure reminder and no user-facing topup card for `charge_error` in the active cadence;
- expired retries occur on later monitoring cycles, apply only to Telegram attempts created after deployment, and never scan old expired rows retroactively;
- the discount use deadline equals the original claim deadline;
- a real provider invoice or an immediate balance debit counts as payment initiation; a saved checkout/topup intent does not;
- the wave card names its resolved period, but the purchase menu keeps its existing period choices and defaults;
- a wave cannot downgrade a better active personal discount;
- only the two expired-wave types are invalidated/rejected after renewal; other offer types are unchanged;
- unclaimed wave offers become ineligible after renewal, while a claim made earlier remains valid until its deadline;
- unmatched legacy NULL-expiry wave discounts receive one bounded migration grace window.

If these recommendations are accepted, the next step is a separate implementation plan, not immediate coding.
