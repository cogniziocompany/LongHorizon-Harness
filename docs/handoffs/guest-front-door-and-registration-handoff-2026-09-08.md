# Handoff: the $39 self-serve purchase and guest provisioning flow

## Handoff header

| Field | Value |
|---|---|
| Written | 2026-09-08 by the overseer session `5321285c-35e2-459a-9dae-92ea8811669f`, corrected the same day after Paxton rejected an earlier framing |
| For | The expert who will dig up the specs; harness tasks implement the outcome |
| Question in one line | A stranger pays $39 with nothing but an email. What has to happen, in order, for that email to become a working account inside its own Power Apps environment? |
| Repos | `cognizioware-powerplatform` (the product they land in), `mcp-cognizioware` (billing service, Stripe, meters, entitlement), purchase page repo **to be determined — it does not exist yet** |
| Prior art in-repo | `design/plans/guest-launch/DECISION-REGISTER.md` (D02), `design/plans/cognizioware-guest-launch-plan.md`, `design/grant-guest-b2b-read-only-access-d365-ce.md` |

## The flow, as the owner describes it

Stated by Paxton on 2026-09-08, and this supersedes any earlier reading in this repo:

1. A prospect lands on a **public purchase page**. Not the signed-in product. They have no account and no tenant relationship yet.
2. They pay **$39 per month** through **Stripe's own embedded snippets** — Stripe-hosted client-side elements, not a checkout flow we hand-build.
3. The **email they pay with is the identity**. There is no separate sign-up form to fill in.
4. On a successful transaction they are told, in plain words, that this email is now the account they use to reach `powerplatform.easybutt0n.ai`.
5. That email is provisioned as a **guest user in a new Power Apps environment**.
6. Everything past step 5 is being planned with other experts and is out of scope here.

The purchase page was originally intended to be Power Pages hosted in Power Platform. That is no longer the plan; the current intent is a standalone web app, Vue or similar. **The choice is not settled and the page is not built.**

## What corrects the record

Two things I had wrong earlier, written down so the next reader does not repeat them:

- **Licensing is not the blocking question.** The offering is $39 per seat per month, and a temporarily licensed guest inside our own tenant is the deliberate design — a cost of sale, not an obstacle to route around. The Power Pages external-user exemption analysis in `grant-guest-b2b-read-only-access-d365-ce.md` remains useful background but is not the deciding factor.
- **The purchase does not belong in the signed-in React app.** A buyer has no account at the moment of purchase, so any control placed inside the authenticated product is visible only to people who have already bought. A checkout button exists at `packages/frontend/src/checkout/CheckoutButton.tsx` and is mounted nowhere; leaving it unmounted is correct under this flow.

## What exists today, verified 2026-09-08

| Piece | State | Evidence |
|---|---|---|
| Purchase page | **Being built into the existing app** | no standalone pricing or checkout app exists in any repo; decision 2026-09-08 is to add a public route to the React app beside `/login`, rather than build a separate site. Harness task 53 |
| Stripe guest product, prices, meter | **Live, test mode** | dev: product `prod_VDG4R7cjnzMBxF`, base `price_1UCpYaQVmqxwMkjoBX4ATB3L`, metered `price_1UCpYbQVmqxwMkjoqmvb8wYT`, meter `cognizioware_guest_tokens` |
| Guest usage metering | **Proven end to end** | a guest usage event reached Stripe; the observation meter moved while the task meter stayed flat |
| Guest execution + charging switches | **On, dev and UAT** | enabled 2026-09-08 on both billing lanes |
| Billing service guest routes | **Live** | dev `sha-bfd65ec`, UAT `sha-b9b06b7` |
| Stripe return pages | **Built** | `/checkout/success`, `/checkout/cancel` in the SPA — usable as return targets from any purchase page |
| Guest identity provisioning | **Not built** | nothing creates an Entra guest, assigns a licence, or creates a Dataverse user from a payment |
| Per-customer Power Apps environment provisioning | **Not built** | no code provisions a new environment on purchase |
| Environment discovery in the product | **Broken, fix queued** | the app asks Microsoft Global Discovery with a token scoped to a single environment; Microsoft refuses; the error is mis-mapped to a 403 reading "stale environment" and the UI swallows it entirely. Harness task 51 |

## The gap, stated precisely

Steps 1 and 2 need a purchase page, now decided as a public route in the existing app (task 53) rather than a separate site. Steps 3 to 5 need an automated provisioning chain that does not exist. What *does* exist is everything in the middle: Stripe products and prices, a billing service that owns every server-side Stripe call, working metering with guest isolation, and a product to land in.

So this is not a repair job. It is one new surface plus one new backend chain, with the payment plumbing already proven underneath.

## Questions for the expert

1. **Settled 2026-09-08 — the purchase page is a public route in the existing React app**, beside `/login`, served by the same host and deploy lane. No separate site, no Power Pages. Confirm this holds once the rest of the chain is designed, and say so if any later requirement breaks it.
2. **Which Stripe embedded product?** Stripe offers a pricing table, embedded Checkout, and Payment Links — each with a different amount of client-side code and a different amount of control over the email field and post-payment redirect. Which one, and what does it imply for capturing the email as the account identity?
3. **What exactly happens on the payment webhook, in order?** Our billing service is the single server-side Stripe writer and must stay so. Enumerate every step from `checkout.session.completed` to a usable account: Entra guest invitation, licence assignment in our tenant, Power Apps environment creation, Dataverse `SystemUser`, security role, and the welcome message telling them their email is now the account.
4. **Can that chain run unattended?** If any step needs a human administrator, self-serve is not achievable as described, and the plan must say so plainly rather than discovering it at launch.
5. **What does "temporary" licensed guest access mean mechanically?** Duration, what reclaims the licence, what happens to their environment on lapse or cancellation, and how that interacts with the `pac solution export` promise that they own their production tenant.
6. **One environment per customer — is that the intent, and what does it cost?** Environment count, capacity and licence per paying customer at 10, 50 and 200 customers, against $39 per seat per month.
7. **What does the buyer see between paying and being able to sign in?** Environment provisioning is not instantaneous. Define the waiting experience and the failure experience, because a silent gap after taking someone's money is the worst possible first impression.

## Constraints that hold regardless

- Everything is Stripe **test mode**; production guest billing additionally waits on seven test-mode closes.
- The billing service matches meter events on `event_name`, not meter id — fixed 2026-09-08 in PR #90; do not regress it.
- Guest observation meters stay isolated from task meters via `GuestObservationOnlyPriceIds`. That isolation is proven and must not regress.
- The billing service remains the only writer of server-side Stripe calls. A purchase page must never hold a secret key.
- `powerplatform-dev` is `org9130dfc5.crm.dynamics.com`; `powerplatform-uat` is `orgff9dcfec.crm.dynamics.com`. Both are bare Dataverse — the QA gate scores the TRMS profile near 26 there purely from drift, so a thin app in those orgs is expected, not a regression.
