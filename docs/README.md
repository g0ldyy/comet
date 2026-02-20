# Comet Documentation

This documentation covers the full Comet project, with two reading paths:

- **Beginner path**: for first-time self-hosters who want guided, step-by-step setup.
- **Advanced path**: for operators who want runtime internals, tuning, and operational details.

CometNet remains documented in a dedicated section because its setup and operations are significantly more complex than the base Comet deployment.

## Beginner Path

1. [Get Started with Docker](beginner/01-get-started-docker.md)
2. [Configure and Install in Stremio](beginner/02-configure-and-install-stremio.md)
3. [Use the Admin Dashboard](beginner/03-admin-dashboard.md)
4. [Set Up Kodi](../kodi/README.md)

If you plan to use Stremio from another device/network, complete the reverse-proxy + HTTPS step in the Docker beginner guide before installing the add-on.

## Advanced Path

1. [Runtime and Architecture](advanced/01-runtime-architecture.md)
2. [Configuration Model and Environment Variables](advanced/02-configuration-model.md)
3. [Streaming, Playback, and Debrid Flow](advanced/03-streaming-and-debrid-flow.md)
4. [Scrapers, Background Scraper, and DMM](advanced/04-scrapers-background-and-dmm.md)
5. [Database and Operations](advanced/05-database-and-operations.md)
6. [HTTP API Reference](advanced/06-http-api-reference.md)

## Dedicated CometNet Docs

- [CometNet Documentation Index](cometnet/README.md)
- [CometNet Quick Start](cometnet/quickstart.md)
- [CometNet Full Reference](cometnet/cometnet.md)
- [CometNet Docker Deployment](cometnet/docker.md)

## Troubleshooting

- [Troubleshooting Guide](troubleshooting.md)

## Documentation Rules Used Here

- Runtime behavior and defaults are documented from the actual code paths (especially `comet/core/models.py` and endpoint/service implementations).
- `.env-sample` is treated as a user-facing template, but behavior is confirmed against runtime code.
