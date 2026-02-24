# PR Review Dataset — API Service

Dashboard server for the [online code review benchmark](../README.md). Axum-based Rust API that serves the review dashboard. Loads PR analysis data from PostgreSQL into memory at startup and serves it via JSON endpoints with a built-in HTML dashboard.

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection URL | *(required)* |
| `BIND_ADDR` | Address to bind the HTTP server | `0.0.0.0:3000` |
| `RUST_LOG` | Log level filter | `info` |

## Build & Run

```bash
cd api_service

# Development
cargo run

# Release
cargo build --release
./target/release/pr-review-api
```

### Docker

```bash
cd api_service
docker build -t pr-review-api .
docker run -e DATABASE_URL=postgresql://... -p 3000:3000 pr-review-api
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | HTML dashboard (embedded) |
| `GET` | `/up` | Health check |
| `GET` | `/api/options` | Available filter options (chatbots, languages, domains, etc.) |
| `GET` | `/api/daily-metrics` | Daily time-series metrics with filtering |
| `GET` | `/api/leaderboard` | Chatbot leaderboard with filtering |

### Filter Parameters (for `/api/daily-metrics` and `/api/leaderboard`)

| Parameter | Description |
|---|---|
| `start_date` | Start date (`YYYY-MM-DD`) |
| `end_date` | End date (`YYYY-MM-DD`) |
| `chatbot` | Comma-separated chatbot names |
| `language` | Comma-separated languages |
| `domain` | Comma-separated domains |
| `pr_type` | Comma-separated PR types |
| `severity` | Comma-separated severities |
| `diff_lines_min` | Minimum diff lines |
| `diff_lines_max` | Maximum diff lines |
| `beta` | F-beta score beta parameter (default: 1.0) |
| `min_prs_per_day` | Minimum PRs per day to include in series |
