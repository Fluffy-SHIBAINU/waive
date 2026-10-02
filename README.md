# Waive

Free or discounted hospital care you're owed, from a photo of the bill.

Status: in development for the Nebius x NVIDIA Global AI Hackathon (Personal AI track).
Design: `docs/superpowers/specs/2026-10-02-waive-design.md`.

## Setup

1. `brew install uv && uv python install 3.12`
2. `uv sync`
3. `cp .env.example .env` and fill in your keys (never commit `.env`).
4. `uv run waive doctor --live`
