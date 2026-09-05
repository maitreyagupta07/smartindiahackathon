# Design System — Sovereign On-Premise Agentic AI Workbench

## Product context (source of truth: MASTER_BUILD_GUIDE.md at repo root, one level up)

This is a frontend for an air-gapped, on-premise AI assistant built for an industrial org (a refinery/PSU). Nothing ever leaves the building — that sovereignty story is a core product feature, not a footnote.

**Backend contract (Person B, port 8000 — the ONLY API the frontend talks to):**
- `POST /api/submit-task` — `{user_id, prompt, file_base64, file_name, file_mime_type}` → `{task_id, status:"queued"}` (returns immediately, never blocks)
- `GET /api/task-status/{task_id}` — polled ~1s — `{task_id, status: queued|processing|completed|failed, model_used, started_at, completed_at, result:{type: text|file, text, file_url, file_name}, error}`
- `GET /api/audit-log` — `{entries:[{task_id, user_id, task_type, model_used, timestamp, file_uploaded}]}`
- Errors: `{error: "message"}` with 400/500.
- The frontend NEVER calls the model server, tools service, or agent executor directly — only these three Person-B endpoints. Do not invent parallel APIs.

**What genuinely exists to display (do not fabricate beyond this — verified against the actual `backend/main.py` implementation, not just the doc):**
- `GET /api/task-status/{id}` returns exactly: `task_id, status, model_used, started_at, completed_at, result, error`. **`model_used` is `null` while `status:"processing"` and is only populated once the executor responds** (i.e. at completion/failure) — the real backend has NO live per-step trace, no in-flight model/tool identity, no token-level reasoning. `task_type` is NOT in task-status at all; it only exists in `GET /api/audit-log` entries, written after a task finishes.
- Two models auto-selected per task: text/code (`qwen2.5:1.5b-instruct`) vs vision (`moondream`), plus the `approval-note-lora` adapter for approval-note-style writing — all surfaced only as the final `model_used` string once a task completes.
- Consequence for the Activity Map: while `status:"processing"`, the UI may show a generic, honestly-labeled progression (classification → routing → tool/knowledge activity → validation → completing) as an interpretation of "the task is in flight," but must NOT claim to know which concrete route/model was chosen until the poll returns `completed`/`failed` with a real `model_used` + `result`. At that point, reconcile the graph's terminal nodes with the real data (which model lit up, whether `result.type` is `text` or `file`, whether `error` is set) rather than guessing. Duration is honestly computable as `completed_at - started_at` once both exist. Build the activity UI as a generalized, reusable presentation layer/state machine so it can light up richer real step data the moment the backend exposes it, without a redesign.
- Deliverables: `result.type === "file"` gives `file_url` + `file_name` (docx/xlsx/pptx). Treat as a first-class artifact card, not a chat-dumped link.
- Audit log: task_id, user_id, task_type, model_used, timestamp, file_uploaded — real fields for an admin table/timeline.
- Approval-note / LoRA: a named adapter (`approval-note-lora`) is used for approval-note-style text generation, referenced today only as a value that could appear in `model_used`/`task_type`. Present LoRA context as a labeled routing/config concept, not live inference telemetry that isn't exposed.
- On-prem/sovereignty: architecturally true (only port 8000 ever leaves localhost; everything else — inference, tools, executor — is localhost-only), but there is no live network-traffic API. The security/sovereignty view communicates the *architecture* (what talks to what, what never leaves the building) as a static/explainer system diagram, not invented live bandwidth numbers.

## Product experience direction (from product brief — treat as binding)

**User workspace:** a professional industrial AI workbench, not a chatbot skin. Central chat/task interface: submit prompt, attach file, watch it work, get a result, revisit history, reach uploaded files/deliverables. Sidebar is restrained: workspace switcher, task history, files/deliverables — nothing backend/infra-shaped leaks through.

**Agent Activity is the signature feature.** Every task shows a compact, alive-while-running activity strip inline in the conversation (progression through classification → model selection → tool/knowledge activity → validation → completion, using the system's real vocabulary). It expands/maximizes into a dedicated Activity Map workspace: connected steps, active/completed states, model/tool identity, expandable per-step detail, subtle motion (no chain-of-thought, no invented telemetry).

**Model routing visibility, not model management.** Make it legible at a glance that the system picked a model (text/code vs vision, or the approval-note LoRA) — an identity chip/badge, not a settings screen.

**Deliverables are first-class.** Request → Agent working → Result → Deliverable. A generated file gets an artifact card with file identity (name, type, icon), status, and actions (open/download) — never a bare link in a chat bubble.

**Admin = a genuinely different product: "Sovereign AI Operations / Control Center."** Information-dense but calm: users, task/activity history, model usage, agent activity, audit trail (hash-chained), knowledge base, approval-note/LoRA config, system/security state, sovereignty/network architecture, resource info where available. Organized IA (sectioned nav), not disconnected cards.

**Sovereignty is a core feature, not a footnote.** A dedicated admin area visualizes the real architecture: user-facing access (port 8000, LAN) vs internal agent execution (executor, localhost) vs local inference (Ollama, localhost) vs local tools/knowledge (localhost) vs external network (none, by design) — as an honest system diagram grounded in §2.1/§2.2 of the build guide, not fake live metrics.

## Visual identity

**Primary style reference:** a premium dark "workspace" product (greeting hero + floating command input + restrained icon-led sidebar + top-right status cluster) reinterpreted for an industrial/technical register — NOT copied literally, no decorative art/imagery, no whimsical copy. Secondary structural cues (extracted, not copied) from technical dashboard/activity-map patterns: connected vertical step cards with icon + title + type/status affordance, a slide-in contextual detail panel, chronological activity feeds with vertical connectors, dense data tables, small monospace status/metadata badges, a thin global status bar.

**Palette — sourced directly from the primary reference screenshot's warm dark atmosphere (hard constraint — do not deviate). Superseded from an earlier cyan/teal direction; this is now the one true palette for both themes below.**

### Dark mode (default)
- Canvas / deepest background: `#0a0908`
- Base surface (app shell, sidebar): `#111010`
- Elevated surface (cards, panels): `#1a1816`
- Raised/hover surface: `#221f1c`
- Hairline borders: `rgba(255,255,255,0.08)`; emphasized border: `rgba(255,255,255,0.14)`
- Text primary: `#f2ede6`; text secondary: `#a8a29a`; text tertiary/muted: `#726c64`
- **Atmospheric gradient (the signature touch, used sparingly — one wash per screen, never per-card):** a soft radial/linear wash from warm amber-ember `#7a3a1a` at ~14% opacity through deep umber `#3a1f12` at ~8% down to transparent, sitting behind hero/header zones only (e.g. the greeting/composer area, a page's top ~320px, or a maximized panel's header) — always fading to the plain base surface beneath actual content so text stays perfectly legible. Never a full-bleed gradient behind lists, tables, or cards.
- Accent (the ONE brand color — warm amber/gold, restrained, never neon): `#e0a34a` for primary active states, focus rings, the single pulsing "live" dot, and small identity accents (matches the gold badge in the reference screenshot); `#c98a34` as its pressed/darker step; accent wash `rgba(224,163,74,0.12)` for subtle active-row/selected backgrounds
- Primary call-to-action chips (send button, primary buttons): high-contrast monochrome — light/off-white fill (`#f2ede6`) with near-black icon/text, exactly like the reference screenshot's send/attach controls — NOT accent-colored. The amber accent is reserved for state/identity, not for every button.
- Status colors (used sparingly, only for state, never decoration): success/completed `#5fb87a`, processing/active `#e0a34a` (shares the brand accent — routing and "in progress" read as the same warm identity), queued/pending `#c9a227`, error/failed `#e2685f`
- Never introduce: purple/violet, pink/magenta, cyan/teal/blue, neon green, glassmorphism blur-heavy panels, or a gradient anywhere but the single restrained atmospheric wash above.

### Light mode (a true first-class theme — equal polish, not an inverted afterthought)
- Canvas / deepest background: `#f7f3ee`
- Base surface (app shell, sidebar): `#fdfbf8`
- Elevated surface (cards, panels): `#ffffff`
- Raised/hover surface: `#f2ede5`
- Hairline borders: `rgba(20,16,12,0.09)`; emphasized border: `rgba(20,16,12,0.16)`
- Text primary: `#1c1815`; text secondary: `#5c564e`; text tertiary/muted: `#8a8378`
- **Atmospheric gradient:** the same amber-ember family, inverted for light — a soft wash from `#f0c98a` at ~22% opacity through `#e8b06a` at ~10% down to transparent, same placement rule (hero/header zones only, never behind dense content).
- Accent: `#b3701f` (a deepened version of the dark-mode amber, tuned for AA contrast on light surfaces) for active states/focus rings/live-dot; `#8f5817` pressed step; accent wash `rgba(179,112,31,0.10)`.
- Primary CTA chips: near-black fill (`#1c1815`) with off-white icon/text — the mirror of dark mode's light-on-dark chip, so both themes share the exact same *shape* language, just inverted.
- Status colors: success `#2f8f52`, processing/active `#b3701f`, queued/pending `#a6791c`, error `#c94f3f` — same hues as dark mode, deepened for light-surface contrast, never a different hue family.
- Shadows read more softly in light mode: halve the alpha of every dark-mode shadow value below rather than reusing the same rgba black at full strength (a shadow tuned for near-black surfaces looks muddy/heavy on cream surfaces if left unchanged).

**Theme mechanics:** implement as real CSS custom properties on `:root` (dark values) and a `[data-theme="light"]` override block (light values), with a small sun/moon toggle control in the top bar that flips the attribute — do not ship two disconnected static comps; both themes must be the same markup, same spacing, same component set, only tokens swapped. Default to dark mode.

**Typography:**
- UI/sans: `Inter` (400 body, 500 labels/buttons, 600 headings/page titles)
- Technical/mono (IDs, timestamps, model names, file names, status codes, log-like text): `JetBrains Mono` (400/500)
- Scale: 24/20px page titles, 15px section headings, 13.5px body, 12px secondary/meta, 11px uppercase tracked-out micro-labels (letter-spacing ~0.06em) for eyebrows/category headers.

**Depth & shape:**
- Border radius: 10px cards/panels, 8px inputs/buttons/badges, 999px pills/avatars/status dots — never the oversized "everything is a rounded blob" look.
- Shadow: reserved for floating/overlay elements only (command bar, slide-in panel, popovers) — `0 8px 32px rgba(0,0,0,0.45)`. Flat cards use a hairline border instead of a shadow.
- No heavy glassmorphism. A light `backdrop-filter: blur(6px)` is permitted only on floating overlays (command palette, modal scrims), never on base cards.

**Motion (restrained, purposeful only):**
- Standard easing `cubic-bezier(0.16,1,0.3,1)`, 150–220ms for hovers/state changes, 240–320ms for panel slide-ins.
- Live/processing state: a single small pulsing dot (6–8px, amber accent color, 1.6s ease-in-out opacity pulse) — the only "alive" animation motif in the whole system. No spinners-everywhere, no shimmer walls, no glowing borders.
- Activity step transition (queued → active → complete): connector line fills/brightens, icon swaps from outline to filled + a brief 200ms checkmark draw — not a bounce or confetti moment.

**Iconography:** simple outline icons (1.5px stroke), never filled/duotone/3D, never decorative robot/AI mascot imagery. A model identity is a small square/rounded chip with an abbreviation or minimal glyph + label (e.g. "Text · qwen2.5-1.5b", "Vision · moondream", "Approval-Note LoRA"), not a cartoon avatar.

**Empty / loading / error states:** every list/panel gets a designed empty state (icon + one-line copy + primary action where relevant) and a designed error state (status-red accent, human-readable message mirroring the backend's `{error}` shape) — never a bare blank div.

## Signature components — refinement pass (supersedes the original "Agent Activity card" description above)

### A. Minimal live agent state strip (User task/chat view)
Sits directly above the composer, NOT inside the conversation as a card. A single slim row (~28-32px tall), no border/panel chrome of its own — just inline text+icons floating on the base surface: `Classifying · Routing · Processing · Validating · Completing`, each a small label with a tiny state glyph before it (done = filled amber check, active = 6px pulsing amber dot, pending = dim 4px hollow ring), separated by short connector dashes that brighten as the active step passes them. Entire strip dims to ~40% opacity and hides automatically once a task is idle/no task in flight. This is the ONLY place the live pulsing-dot motif appears at this small scale — restrained, single-line, never wraps to a second line, never grows into a card.

### B. Maximized Activity Map — an execution GRAPH, not a vertical list
This is the signature visual centerpiece of the whole product. Reference: an interactive stage-flow / mind-map tool (branching node-graph, dashed connectors, radiating satellite pills, click-to-open detail drawer) — extract the INTERACTION AND FLOW LANGUAGE only, reinterpreted fully in this system's warm amber-on-graphite palette (never that reference's own blue/red hues).

- **Backbone:** a horizontal (or gently zig-zagging) chain of primary stage cards — `Task → Classification → Routing → Knowledge/Tool Activity → Validation → Deliverable` — each an elevated-surface rounded card (icon + stage label + one-line real description), connected by dashed horizontal connector lines. A completed segment's dash brightens to solid amber and gets a slow single traveling highlight (a short bright segment animating left-to-right along the dash once, ~900ms, cubic-bezier ease) the moment that stage completes — this is the "traversing the pipeline" feeling, kept subtle and non-looping (fires once per transition, not a constant marquee). A pending segment stays a faint dim dash with no motion.
- **Branching at Routing:** the Routing stage card radiates 2-3 satellite pill nodes outward via short dotted connector lines (exactly the radiating-pill pattern from the reference) — `Text → qwen2.5-1.5b`, `Vision → moondream`, `Code → qwen2.5 + sandbox` (and `Approval-Note LoRA` as a 4th when relevant). Only the pill matching the task's real eventual route lights up solid/amber once known; the untaken branches stay visibly present but dimmed/muted — the graph shows the decision space, not just the one path, exactly like the reference's always-visible-but-color-coded satellite nodes.
- **Branching at Knowledge/Tool Activity:** same radiating-pill treatment for whichever of `Execute Code`, `Search Documents`, `Generate File` is relevant; untaken ones stay dim.
- **Node states:** completed = solid amber-outlined card/pill + filled icon; active/current = amber-outlined card with the pulsing dot motif + subtly emphasized (slightly larger shadow/scale, ~1.02x) so the eye finds "where we are now" instantly; pending = low-opacity outline, muted icon+text, no motion at all.
- **Deliverable terminus:** the final stage card, when reached, expands slightly to show a compact inline preview of the artifact card (file icon + mono filename), matching the task view's deliverable card language.
- **Adaptivity:** do not hardcode one universal graph — a text-only task never shows the Vision/Code satellites as "took this path" (they stay present-but-dim, since routing is still a real decision point being shown), and a task with no tool call skips the tool-activity branch entirely rather than showing a fake dim node for a stage that fundamentally didn't apply. Only Routing's model-choice branch is always shown (it's always a real decision); tool-activity branching only appears for tasks whose task_type implies it.

### C. Activity node detail drawer
Clicking any stage card or satellite pill slides in a right-side panel (~420-480px, elevated surface, `0 8px 32px` shadow, 260ms slide) — same drawer shell language as the reference's structured detail panel. Header: small icon + stage/pill name + a status pill (Completed/Active/Pending/Error). Body: a compact key-value list, fields adapted to node type and populated ONLY from real fields once available (never fabricated mid-flight):
- Classification/Routing nodes: task type, which model was selected and why (a short honest label, e.g. "vision model — image attached"), status.
- Tool/knowledge nodes: which tool, and once completed, a short real result summary (e.g. file name generated, or "search returned N passages" if that shape is ever exposed) — while still in-flight, show "In progress" rather than a placeholder value.
- Deliverable node: file name (mono), file type, Open/Download actions.
- Duration: `completed_at - started_at` once both exist, else "—".
- Error state: if `error` is set, the WHOLE relevant node (not just the drawer) switches to the error status color and the drawer shows the human-readable `{error}` message.
While a task is in-flight, pending nodes' drawers show a clearly-labeled "Not yet reached" empty state rather than blank/broken content.

## Information architecture

**User app:**
- Left rail (collapsible, icon+label): Workspace / New Task, Task History, Files & Deliverables, (bottom) account.
- Main: greeting + command composer (prompt + attach) when idle; task conversation view when a task is selected — messages + inline compact Agent Activity strip per task-in-flight, expandable to the full-screen Activity Map overlay.
- Deliverable artifact card component: file-type icon, file name (mono), status pill, Open/Download actions.

**Admin app (separate shell, NOT the user shell + a tab):**
- Left rail sections: Overview, Users, Task Activity, Model & Routing (incl. LoRA/approval-note config), Knowledge Base, Audit Log, Security & Sovereignty, System Resources.
- Denser tables/timelines, status-badge-driven, same tokens as user app but tighter spacing (row height ~40px vs ~56px in user app).
- **Polish pass specifics:** a real filter/search row above every table (search input + 1-2 dropdown filters, e.g. status/task type), sortable column headers (subtle sort-direction glyph, no heavy chrome), zebra-free rows separated by hairline borders only, right-aligned numeric/timestamp columns, status as small colored-dot+label (not oversized pill badges), grouped sections with an 11px uppercase tracked label above each table/panel rather than a boxed "card" for every single metric. Stat tiles (when used) are a single slim row of 3-4 compact number+label blocks with a hairline divider between them, not separate large shadowed cards each. Empty state: centered icon + one-line copy + (if applicable) a subtle action. Loading state: a slim indeterminate top-of-table bar, not a full-panel spinner.
- Security & Sovereignty page: an architecture diagram (LAN boundary → Backend :8000 → localhost-only Executor :8002 → localhost-only Inference :11434 / Tools :8001), each hop labeled with what does/doesn't cross the boundary — an explainer diagram, not a live traffic monitor, unless/until the backend exposes real metrics.

## Constraint reminder for every generation

Use ONLY the fonts, colors (warm amber/ember + graphite/cream neutrals per the Dark mode / Light mode palettes above), spacing, and component styles defined above. The atmospheric gradient is a single restrained wash behind hero/header zones only — never full-bleed, never per-card, never a second competing gradient direction. Both themes (dark default, light toggle) must reach equal visual polish — light mode is not a quick CSS invert, it has its own tuned tokens above. Do not introduce any fonts, colors, or visual styles not in this design system. Do not fabricate backend data/telemetry beyond the fields listed in "What genuinely exists to display" — where a capability is planned but not yet backend-exposed, design the slot/placeholder intelligently (e.g. a "coming soon" or clearly-labeled static/illustrative state) rather than inventing live numbers.
