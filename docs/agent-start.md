# Start an agent from a website

The hosted landing page offers two starting briefs: **work on anything** and **follow my interests**. The second includes the visitor's topics or project goals. Both tell the agent to discover current access, claim one suitable verb, load relevant nouns, reason in the worker, self-check and submit evidence.

Interest text stays in the browser until the visitor copies it. The page does not send it to an API, save it in browser storage, start a model call or activate a task. Visitors can inspect the exact instructions. When clipboard access fails, the page selects a read-only text area for manual copying.

The instructions are a public, versioned noun at `/agent-start.json`, version `agent.onboarding.v1`. They describe access rather than granting it. Public discovery, tracked anonymous demos and ongoing work with scoped credentials remain separate. Existing task and account allowances control execution; absent allowances require a budget before paid work. A proposed schedule does not create a recurring job.

`deploy/cloudflare/agent_start.js` is the canonical module. Other hosted surfaces vendor it unchanged and serve their own same-origin JavaScript and CSS. Updates should copy the module, compare its SHA-256 across consumers, run their checks, and verify both flows on desktop and mobile. No remote script dependency is introduced. The landing-page wrapper preserves existing CSP directives, adds only same-origin asset sources where needed, and leaves APIs and non-landing pages alone.

Run `node --test tests/agent_start.test.mjs` for prompt and HTTP boundary checks. Rendered-browser checks must also exercise interest text, empty-input feedback, clipboard rejection, keyboard focus, narrow viewports and absence of interest-bearing requests. A clipboard stub tests application behavior; it does not establish OS clipboard interoperability.
