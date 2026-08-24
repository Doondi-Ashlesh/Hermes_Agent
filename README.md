# Support Agent (Hermes Agent)

A self-improving customer support agent built on **Hermes Agent**. It handles recurring support workflows and gets better at your product's specific issues over time.

## What it does
- Triages incoming support tickets and drafts/sends responses for common issues
- Builds a persistent profile per customer (history, preferences, past complaints)
- Converts repeated troubleshooting steps into reusable skills over time
- Escalates unresolved or high-risk tickets to a human agent

## Use cases
- Tier-1 ticket triage (password resets, billing questions, known-bug workarounds)
- Personalized account support using customer history
- Recurring diagnostic workflows (e.g., daily error report checks, monitoring alerts)

## Requirements
- Hermes Agent runtime (self-hosted, Python, MIT licensed)
- Access to your support inbox/ticketing system (email, Zendesk, etc.)
- Model access via Nous Portal or your own configured LLM provider

## Setup
1. Install and run the Hermes Agent runtime.
2. Connect your ticketing/support channel as an input source.
3. Seed the agent with your existing FAQs, macros, or past resolved tickets so it has a starting skill set.
4. Configure escalation rules for tickets it shouldn't resolve autonomously (refunds above a threshold, legal/security issues, angry customers, etc.).
5. Run it in shadow mode first (drafts only, human approves) before enabling autosend.

## How it improves over time
After each resolved ticket, the agent evaluates the interaction, extracts a reusable pattern, and stores it as a skill. Over weeks of use, response quality and resolution speed should compound rather than reset each session.

## Project docs
- [Implementation plan](docs/PLAN.md) — architecture, phased build, risks, open decisions
- [ADR 0001](docs/adr/0001-runtime-nemoclaw-hermes.md) — why Hermes Agent runs under the NVIDIA NemoClaw blueprint

## Notes
- Not intended to fully replace human support — best for high-volume, low-complexity, repeatable tickets.
- Review the skill library periodically to catch bad patterns before they compound.
