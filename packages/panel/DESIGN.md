# ProxyHub management workspace

The panel turns the approved single-node operations into an everyday workspace. The service relationship is the central visual: a website and its certificate support the REALITY node, which is published through a subscription. Each resource opens the corresponding management page. Actual service state, deployment revision and task results supply all production data.

## References and deliberate choices

The [developer-portfolios collection](https://github.com/emmabostian/developer-portfolios) provides the starting point. [Brittany Chiang](https://brittanychiang.com/) informs persistent orientation and the clear relationship between navigation and content. [Lee Robinson](https://leerob.com/) informs compact, economical grouping. These are compositional references; no portfolio source, branding, text or animation is copied.

An early direction using repeated metric cards would have suggested live traffic accounting that the installed product does not provide. The design instead spends its visual emphasis on the service topology. Quiet details, forms and task records surround it. The portfolio-style display treatment is limited to the sign-in page; the authenticated workspace prioritizes concrete operations.

## Tokens

| Token | Light | Dark | Role |
| --- | --- | --- | --- |
| Mist | `#F5F7FA` | `#101B2B` | Workspace background |
| Paper | `#FFFFFF` | `#162438` | Resource and form surfaces |
| Ink | `#182536` | `#E7EDF5` | Primary text |
| Slate | `#536477` | `#ACBACD` | Secondary text |
| Action blue | `#215AC8` | `#84ACFF` | Action, focus and selected resource |
| Warning | `#8E4F09` | `#EFB56C` | Warning text; darkened from approved `#B86A12` for small-text contrast |

IBM Plex Sans provides Latin text; system Chinese sans-serif is the deliberate fallback. IBM Plex Mono is reserved for commands, fingerprints and configuration. The fonts are self-hosted through exact `@fontsource` dependencies; their SIL Open Font License notices ship in `public/licenses`. No external font or asset request is made.

The light tertiary text token uses `#607187`: measured contrast is 4.65:1 against the mist background and 4.99:1 against paper. This includes small metadata and input placeholders; decorative lines do not substitute for text labels.

Headings use 30/18/16 px; normal controls use 13/14 px. Desktop navigation occupies 226 px, becoming a compact page selector below 700 px. The topology changes from a three-column relationship to a compact two-by-two layout. At 360 px, forms use one column and the change drawer fills the viewport. Focus is visible; native modal dialogs trap focus and restore it on close. Motion is limited to selected/hover state and pending operation feedback, with reduced motion respected.

## Interaction contract

- Login credentials and CSRF state stay in memory. Only the theme name is saved to local storage.
- The common operation endpoint is the only source of runtime data. There is no fixture or demo fallback in the production bundle.
- Changes are prepared first, then reviewed in a drawer showing before/after, impact and warnings. Closing the drawer never applies the plan.
- Task polling reports queued, running, succeeded, failed and interrupted states. A polling timeout directs the user to existing task records instead of silently resubmitting.
- SSH paths come from `ssh.inspect` and are submitted by opaque ID. Public keys may be pasted or read from a local `.pub` file. The UI never claims that writing a key verifies a new SSH login.
- Certificate import clears the private-key input immediately after submission. The key is never reflected in previews or stored by the UI.
- Client overrides are a restricted JSON object, checked by the sole server-side generator. YAML input editing is intentionally not introduced in this increment.
- Subscription links and the final YAML are revealed only by explicit actions. They clear on navigation/unmount, session loss or a revision change. Copy operations are explicit.
- `Ctrl/⌘ K` opens keyboard-accessible navigation. Each page copies its actual CLI help command. All settings, theme, refresh, reveal and form controls are wired to a real action.
- Capability limits are explained in help. HY2, rules, multi-site lifecycle and automatic accounting have no nonfunctional action buttons.

## Build and CSP

`npm run build` builds the existing converter and then the panel. `npm run build:panel` uses its own TypeScript configuration and Vite; output is `packages/panel/dist`. Deployment copies this static output; it does not compile on the VPS. Dependency versions were checked against the official npm registry on 2026-10-14 and are exact in the root lockfile.

The panel is compatible with a strict self-only CSP: scripts, styles and fonts are external same-origin files; no inline style, inline script, CDN, eval or embedded production fixture is required. Inline SVG paths are decorative and have accessible text labels on their parent controls.
