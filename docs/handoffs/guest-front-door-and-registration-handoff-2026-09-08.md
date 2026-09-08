# Handoff: the guest front door — registration, licensing and where the charge happens

## Handoff header

| Field | Value |
|---|---|
| Written | 2026-09-08 by the overseer session `5321285c-35e2-459a-9dae-92ea8811669f` |
| For | The expert who will dig up the specs; a harness task implements the outcome |
| Question in one line | A paying guest arrives with no relationship to our tenant. What identity do they get, which surface do they buy through, and which Microsoft licence — if any — must we hold for them? |
| Repos | `cognizioware-powerplatform` (front door, sessions, environments), `mcp-cognizioware` (billing service, Stripe, meters, entitlement) |
| Prior art in-repo | `design/grant-guest-b2b-read-only-access-d365-ce.md`, `design/plans/cognizioware-guest-launch-plan.md`, `design/plans/guest-launch/DECISION-REGISTER.md` |
| Status | Blocked on a decision, not on engineering effort |

## Why this exists

Decision **D02** in the register reads: *"Start test-mode vertical slice in the existing React SPA; target **Power Pages + Stripe hosted Checkout for the paid pilot**"*, marked TOP PRIORITY - GO. The test-mode slice was partly built. The Power Pages front door was never built. On 2026-09-08 an attempt to run the purchase journey end to end found there is nowhere to buy in either surface, and the reason turns out to be three separate defects plus one unresolved architectural question.

The architectural question is the reason for this handoff. The defects are already queued as work.

## What is actually built, verified 2026-09-08

| Piece | State | Evidence |
|---|---|---|
| Stripe guest product, prices, meter | **Live, test mode** | dev lane env: guest product `prod_VDG4R7cjnzMBxF`, base price `price_1UCpYaQVmqxwMkjoBX4ATB3L`, metered `price_1UCpYbQVmqxwMkjoqmvb8wYT`, meter `cognizioware_guest_tokens` |
| Guest usage metering, end to end | **Proven** | a guest usage event reached Stripe; the observation meter incremented while the task meter stayed flat — the isolation that matters |
| Billing service guest routes | **Live on dev** | `sha-bfd65ec`, deployed by the lane |
| SPA Stripe return pages | **Built and routed** | `/checkout/success`, `/checkout/cancel` in `packages/frontend/src/App.tsx` |
| SPA checkout **button** | **Built, never mounted** | `packages/frontend/src/checkout/CheckoutButton.tsx` exists; nothing imports or renders it. There is literally no way to start a purchase |
| Guest charging switch | **Off everywhere** | dev `Billing__GuestChargingEnabled=false` (execution true); uat both false |
| Power Pages guest site | **Does not exist** | the launch plan records it as "not found in the inspected application scope"; the repo contains no Power Pages site definition |
| Environment discovery | **Broken** | see below — this is what stops a signed-in user reaching any environment at all |

### The discovery defect, because it masks everything else

Signing in as `ai-dev01@cognizio.company` against powerplatform-dev shows "No active environments", and **Discover Environments** appears to do nothing. It returns HTTP 403.

The chain: `packages/frontend/src/auth/MsalProvider.tsx` acquires **one** token, scoped to a single Dataverse environment (`runtimeConfig.msal.dataverseScope`). `EnvironmentList.tsx` sends that token to `POST /api/environments/discover`. The backend passes it straight to Microsoft's Global Discovery Service, which requires a token whose audience is Global Discovery itself, and refuses. The service throws `AppError('E101')`, and E101 maps to HTTP 403 with the machine name `stale_environment` — so a token-audience failure is reported as a permissions problem about a stale environment. The frontend never checks `res.ok`, so the 403 renders as the empty state with no error at all.

This is why the `/api/environments/seed` endpoint exists; its own comment says discovery fails "when the caller's token audience is a single env". **Fix discovery and the seeding workaround stops being needed for the environment list.** Queued as harness task 51 with an explicit instruction not to add any allow-list: Microsoft decides what a user can see, exactly as XrmToolBox does.

## The architectural question — this is what the expert must settle

`design/grant-guest-b2b-read-only-access-d365-ce.md` establishes, with Microsoft citations, that a B2B guest invited into our tenant who uses a **model-driven app UI directly**:

- must hold a licence **assigned in our tenant** — a home-tenant licence does not count for customer engagement apps;
- has no read-only SKU available: the realistic floor is Dynamics 365 Team Members at roughly $8 per user per month (restricted to three first-party apps, now technically enforced) or Power Apps Premium at $20 per user per month (custom apps only, and not over restricted tables);
- is blocked by default on new environments until guest Dataverse access is explicitly enabled;
- reaches the app only through a deep link.

Crucially, the same document records that Microsoft's external-user exemption — the one that lets genuine third parties in without a per-user licence — applies to access **via Power Pages / Portals**, and explicitly does *not* extend to employees, contractors, vendors or agents using the app interface directly.

That is the crux. Stated plainly:

- **If a paying guest is a B2B user in our tenant using the model-driven app**, we owe Microsoft a per-guest licence of $8–$20 per month before they have paid us anything. Self-serve economics have to survive that, and the sign-up flow has to provision an Entra invitation, a licence assignment, a Dataverse `SystemUser` and a security role before first use.
- **If a paying guest arrives through Power Pages**, the external-user exemption is the intended path, and the cost shape changes to Power Pages capacity — a Tier 1 authenticated pack around $200 per website per month for 100 users, or pay-as-you-go around $4 per active authenticated user per website per month, break-even near 50 users.

Both readings are defensible from the material we hold. They imply very different builds, and we should not write either until it is settled.

## What we believe the flow should be, stated so it can be corrected

1. A prospect arrives anonymously at a public Power Pages site.
2. They buy through Stripe hosted Checkout in subscription mode; the billing service owns every server-side Stripe call and remains the single writer.
3. On payment, they are registered as a guest identity in our domain, and entitlement is written server-side.
4. Only then do they reach the product surface, at a level of access their entitlement allows.

Step 3 is where the licensing question bites, and step 3 is what Paxton has identified as the next thing to test.

## Questions for the expert

1. **Does the external-user exemption actually cover our guests?** They are paying customers of ours, not our contractors — but they use our product to operate on Dataverse data. Which side of Microsoft's line does that fall on, and does the answer change if the surface is Power Pages rather than a model-driven app?
2. **What is the minimum compliant identity for a paying guest** who must reach Dataverse data — B2B guest with a host-tenant licence, a Power Pages authenticated user, or something else — and what does each cost per guest per month at 10, 50 and 200 guests?
3. **Can guest registration be fully automated** — Entra invitation, licence assignment, `SystemUser` creation, security role — from a Stripe webhook, or does any step require human admin action? If any step is manual, self-serve is not achievable and the plan must say so.
4. **Does enabling guest Dataverse access on an environment** (`restrictGuestUserAccess = false`) widen exposure beyond the guests we intend, and what compensating controls does the security role and environment design need?
5. **Where does the charge belong** given the answers above — Power Pages native payments, Stripe hosted Checkout from Power Pages, or Stripe hosted Checkout from the SPA — and what does that imply for the enhanced data model, Key Vault and table-permission prerequisites Microsoft documents for Power Pages payments?
6. **Is the SPA test-mode slice still worth mounting** to prove the Stripe and email path this week, or does it create a second front door we will have to retire?

## What is queued regardless of the answer

These do not depend on the decision and are already moving:

- **Task 51** — environment discovery via a Global Discovery scoped token, honest error mapping, and surfacing the failure instead of an empty list. Queued first.
- **Task 50** — the missing EF migration behind `mcp-cognizioware` PR #91, which otherwise 500s the first time a kill switch is flipped.
- Enabling `Billing__GuestChargingEnabled` on dev and pointing the Stripe test customer at `ai-dev01@cognizio.company`, so the charge and the receipt email can be observed as soon as there is a way to buy. Approved by Paxton 2026-09-08; not yet applied.

## Evidence and constraints worth carrying forward

- Everything is Stripe **test mode**. Prod guest billing additionally waits on seven test-mode closes.
- The billing service matches meter events on `event_name`, not meter id — a defect found and fixed on 2026-09-08 (PR #90).
- Guest observation meters are isolated from task meters by `GuestObservationOnlyPriceIds`; that isolation is proven and must not regress.
- The dev environment is `powerplatform-dev` / `org9130dfc5.crm.dynamics.com`; UAT is `powerplatform-uat` / `orgff9dcfec.crm.dynamics.com`. Both are bare Dataverse — the QA gate config scores the TRMS profile at about 26 there purely from drift, so do not read a thin app in those orgs as a regression.
