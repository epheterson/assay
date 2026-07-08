# assay

> An "assay" tests precious metal for purity. **`assay`** tests software releases for trust.

Watch a GitHub repo's releases. On each new release, run a battery of checks (URL inventory, committer diff, more), have an LLM judge whether the diff is consistent with the stated intent, and emit a structured verdict to **email + GitHub Issues**.

Built for the case where you depend on an auto-updating piece of software — sideloaded apps, npm packages, Homebrew formulas, container images — and want a human-in-the-loop gate against silent supply chain attacks.

## History

[**LEDGER.md**](LEDGER.md) — green/red list of every verdict assay has rendered, newest first, with links to each release.

## The verdict

Every release gets one of:

| Verdict | Meaning | Issue state |
|---|---|---|
| ✅ **clean** | Diff matches stated intent. No anomalies worth your time. | Auto-closed |
| ⚠️ **review** | Anomalies worth a human look — new committer, new URL, etc. | Stays open |
| 🚨 **blocked** | High-confidence problem. Don't auto-update. | Stays open |

State lives in the assay repo's own GitHub Issues. Each verdict opens an issue labeled `assay:{owner}/{repo}` + `verdict:{class}`. The most recent labeled issue *is* the current state — atomic, auditable, conflict-free, and natively pushed to your phone via the GitHub mobile app.

## How it works

```
                  hourly cron
                          │
                          ▼
              ┌─────────────────────────┐
              │  poll upstream releases │  cheap; no LLM, no download
              └────────────┬────────────┘
                           │ new tag?
                           ▼
      ┌────────────────────────────────────────┐
      │   run check modules (committer_diff,   │  expensive; only fires
      │   url_inventory, …)                    │  on new release
      └────────────────────┬───────────────────┘
                           ▼
      ┌────────────────────────────────────────┐
      │   judge: GitHub Models LLM call,       │
      │   produces structured verdict          │
      │   ★ rule-based override:               │
      │     hard_flag from any module          │
      │     forces verdict ≥ review            │
      └────────────────────┬───────────────────┘
                           ▼
      ┌────────────────────────────────────────┐
      │   write GH issue (state + audit log    │
      │   + native push notification)          │
      │   send email                           │
      │   commit verdict JSON to repo          │
      └────────────────────────────────────────┘
```

## Modules

| Module | What it checks | Hard-flag condition |
|---|---|---|
| `committer_diff` | New authors landing in this window | Any first-time committer |
| `url_inventory` | Hostnames added/removed in binary or source | New host not on allowlist |

(Roadmap: `dependency_diff`, `file_tree_diff`, `entitlement_diff`, `framework_diff`, `secret_scan_diff`, `osv_scan`.)

## Judge

Calls **GitHub Models** at `https://models.github.ai/inference/chat/completions` — free for public repos, no API key needed beyond the workflow's built-in `GITHUB_TOKEN` with `models: read` permission. Default model: `openai/gpt-4o-mini`. Uses `response_format=json_object` for structured output.

Falls back to a heuristic verdict if the LLM call fails or `USE_FAKE_JUDGE=1` is set. **Any module's hard_flag forces verdict ≥ review** — the LLM's job is to *explain*, not to *gate*.

## Reference use case: Apollo for Reddit

Apollo for Reddit was discontinued in June 2023. A community fork ([`JeffreyCA/Apollo-ImprovedCustomApi`](https://github.com/JeffreyCA/Apollo-ImprovedCustomApi)) keeps it usable; [`Balackburn/Apollo`](https://github.com/Balackburn/Apollo) builds and distributes the IPAs through an AltStore source.

That's 5+ trust nodes (tweak author, distributor, AltStore source, AltServer, your dev cert). Apple's App Store review is gone by design. `assay` sits in front of this chain.

See [`examples/apollo/`](examples/apollo) and [`.github/workflows/assay-apollo.yml`](.github/workflows/assay-apollo.yml).

## Adding a new upstream

1. Drop a workflow in `.github/workflows/assay-{name}.yml` modeled on `assay-apollo.yml`.
2. Optionally drop an `examples/{name}/allowlist.json` listing expected hosts.
3. Push. Cron picks it up; first run writes a baseline issue. Second new release writes a real verdict.

## Notifications

| Channel | Setup |
|---|---|
| **GitHub native push** | Subscribe to your fork of `assay` on github.com. Each verdict opens an issue → instant push via the GitHub mobile app. Closed issues = clean verdicts; open issues = needs attention. |
| **Email** | Add `GMAIL_USERNAME` + `GMAIL_APP_PASSWORD` repo secrets. See below. |
| **Webhook** | Roadmap. |

### Setting up Gmail email (2-minute setup)

1. Go to https://myaccount.google.com/apppasswords (requires 2FA enabled on the account)
2. Generate an app password labeled "assay" — copy the 16-character string
3. In the repo: Settings → Secrets and variables → Actions → New repository secret
4. Add `GMAIL_USERNAME` = your-gmail-address@gmail.com
5. Add `GMAIL_APP_PASSWORD` = the 16-character app password (no spaces)
6. Next verdict triggers an email to the `to:` address in `.github/workflows/assay-apollo.yml`

## Why not just use {tool}?

| Existing tool | What it covers | What `assay` adds |
|---|---|---|
| OpenSSF Scorecard | Repo hygiene metadata | Per-release behavioral diff |
| Dependabot / OSV-Scanner | Known CVEs in declared deps | Novel changes; binaries |
| Trufflehog / Gitleaks | Leaked secrets | Behavior diff with AI judge |
| Socket.dev | npm/pypi supply chain | Ecosystem-agnostic |
| Sigstore / SLSA | Build provenance | "What does the binary do?" judgment |

`assay`'s niche: **"watch this upstream's releases, AI-judge the diff against stated intent, gate downstream consumption on the verdict."**

## License

MIT. Have fun.
