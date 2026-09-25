# Handoff: the $39 self-serve purchase and guest provisioning flow

## Handoff header

| Field | Value |
|---|---|
| Written | 2026-09-08 by the overseer session `5321285c-35e2-459a-9dae-92ea8811669f`, corrected the same day after Paxton rejected an earlier framing |
| For | The expert who will dig up the specs; harness tasks implement the outcome |
| Question in one line | A stranger pays $39 with nothing but an email. What has to happen, in order, for that email to become a working account inside its own Power Apps environment? |
| Repos | `cognizioware-powerplatform` (the product they land in), `mcp-cognizioware` (billing service, Stripe, meters, entitlement), the purchase page is a **public route in `cognizioware-powerplatform`**, decided 2026-09-08 |
| Prior art in-repo | `design/plans/guest-launch/DECISION-REGISTER.md` (D02), `design/plans/cognizioware-guest-launch-plan.md`, `design/grant-guest-b2b-read-only-access-d365-ce.md` |

## The flow, as the owner describes it

Stated by Paxton on 2026-09-08, and this supersedes any earlier reading in this repo:

1. A prospect lands on a **public purchase page**. Not the signed-in product. They have no account and no tenant relationship yet.
2. They pay **$39 per month** through **Stripe's own embedded snippets** — Stripe-hosted client-side elements, not a checkout flow we hand-build.
3. The **email they pay with is the identity**. There is no separate sign-up form to fill in.
4. On a successful transaction they are told, in plain words, that this email is now the account they use to reach `powerplatform.easybutt0n.ai`.
5. That email is provisioned as a **guest user in a new Power Apps environment**.
6. Everything past step 5 is being planned with other experts and is out of scope here.

**CORRECTION, 2026-09-08 evening.** An earlier version of this handoff said no purchase page existed. That was wrong, and the error was mine. `design/handoffs/powerplatform-expert-pickup-handoff.md` (2026-08-09) documents an "Easy Button / $39-subscription" flow described as *complete and verified in dev*, and its QA runner drives a **live Power Pages portal at `https://easybutt0n.powerappsportals.com`**, which answers 200 today. `SignupController.cs` and `PartnerBillingController.cs` are on `mcp-cognizioware` `develop`. So the Power Pages front door was built, not skipped.

A harness task to add a public purchase route to the React app was queued and then **aborted** once this came to light, because it would have stood up a second storefront beside a working one.

## What corrects the record

Two things I had wrong earlier, written down so the next reader does not repeat them:

- **Licensing is not the blocking question.** The offering is $39 per seat per month, and a temporarily licensed guest inside our own tenant is the deliberate design — a cost of sale, not an obstacle to route around. The Power Pages external-user exemption analysis in `grant-guest-b2b-read-only-access-d365-ce.md` remains useful background but is not the deciding factor.
- **The purchase does not belong in the signed-in React app.** A buyer has no account at the moment of purchase, so any control placed inside the authenticated product is visible only to people who have already bought. A checkout button exists at `packages/frontend/src/checkout/CheckoutButton.tsx` and is mounted nowhere; leaving it unmounted is correct under this flow.

## What exists today, verified 2026-09-08

| Piece | State | Evidence |
|---|---|---|
| Purchase portal | **Live** | `https://easybutt0n.powerappsportals.com` answers 200; it is the Power Pages site the Easy Button QA runner targets |
| Self-serve signup + partner billing | **On `develop`** | `SignupController.cs`, `PartnerBillingController.cs`, plus `GuestSubscriptionController.cs` and `GuestWebhookController.cs` |
| Easy Button A1–A8 QA matrix | **Written, never merged** | branches `codex/easybutton-a1-runner` (12 ahead, **293 behind** `develop`) and `codex/easybutton-qa-checkpoints`; A1 reported 3/3 in ~68s in August |
| Stripe guest product, prices, meter | **Live, test mode** | dev: product `prod_VDG4R7cjnzMBxF`, base `price_1UCpYaQVmqxwMkjoBX4ATB3L`, metered `price_1UCpYbQVmqxwMkjoqmvb8wYT`, meter `cognizioware_guest_tokens` |
| Guest usage metering | **Proven end to end** | a guest usage event reached Stripe; the observation meter moved while the task meter stayed flat |
| Guest execution + charging switches | **On, dev and UAT** | enabled 2026-09-08 on both billing lanes |
| Billing service guest routes | **Live** | dev `sha-bfd65ec`, UAT `sha-b9b06b7` |
| Stripe return pages | **Built** | `/checkout/success`, `/checkout/cancel` in the SPA — usable as return targets from any purchase page |
| Guest identity provisioning | **Unverified** | not traced end to end in this session; the August handoff asserts the dev flow was verified, but against the Easy Button price, not the September guest prices |
| Per-customer Power Apps environment provisioning | **Not built** | no code provisions a new environment on purchase |
| Environment discovery in the product | **Broken, fix queued** | the app asks Microsoft Global Discovery with a token scoped to a single environment; Microsoft refuses; the error is mis-mapped to a 403 reading "stale environment" and the UI swallows it entirely. Harness task 51 |

## The gap, restated after the correction

Far less is missing than it appeared. The portal is live, signup and billing controllers are merged, Stripe products and prices exist, metering works with guest isolation proven, and there is a product to land in.

**Settled 2026-09-08 by Paxton: the September guest launch track is canonical; the older design is deprecated.**

| | Canonical | Deprecated |
|---|---|---|
| Base price | `price_1UCpYaQVmqxwMkjoBX4ATB3L` | `price_1TsBP9CXyLTnTERyMFHhpOn7` |
| Metered price | `price_1UCpYbQVmqxwMkjoqmvb8wYT` | — |
| Meter | `cognizioware_guest_tokens` | — |
| Controllers | `GuestSubscriptionController`, `GuestWebhookController` | `SignupController`, `PartnerBillingController` |
| Front door | public route in the React app, Stripe embedded snippet | Power Pages portal `easybutt0n.powerappsportals.com` |
| Switches | guest execution + charging on, dev and UAT, 2026-09-08 | — |

Nothing new may be wired to the deprecated price, controllers or portal. Retiring them — redirecting or taking down the portal, removing the dead Stripe objects, deciding the fate of the unmerged A1–A8 matrix — is separate work and should be scheduled deliberately rather than done incidentally.

**One consequence to be explicit about:** the A1–A8 scenario matrix on `codex/easybutton-a1-runner` tests the deprecated journey against the deprecated price and the deprecated portal. It is 293 commits behind `develop`. It is therefore **not** the fast route to confidence I suggested earlier — rebasing it would validate a flow we are retiring. The guest journey needs its own end-to-end test, and that is a gap.

## Questions for the expert

1. **How and when does the deprecated Power Pages portal come down?** It is live at `easybutt0n.powerappsportals.com` and still charges the deprecated price. Decide whether it redirects to the new route, shows a notice, or is taken down, and who owns that change — an abandoned storefront that still takes money is the risk here.
2. **What end-to-end test covers the guest journey?** The only written matrix tests the deprecated flow. Define the equivalent for the guest track before launch, not after.
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

## Two things worth knowing before you read the August handoff

- It contains **live credentials in plain text** — a QA gateway token and a billing API key — in a committed file, while its own secrets section says not to commit them. Treat anything quoted there as exposed.
- Its verification checklist was never completed, and it flags a stale `APP_IMAGE_TAG` on the powerplatform prod lane risking a downgrade on restart. That same class of stale pin was found and corrected on both billing lanes on 2026-09-08, so it is worth re-checking on the powerplatform lane too.
