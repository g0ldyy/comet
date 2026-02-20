<h1 align="center" id="title">☄️ Comet</h1>

<p align="center">
  <a href="https://discord.com/invite/UJEqpT42nb"><img src="https://img.shields.io/badge/Discord-Join%20Us-5865F2?style=flat-square&logo=discord&logoColor=white" /></a>
  <a href="https://stremio-addons.net/addons/comet"><img src="https://img.shields.io/badge/Stremio-Addon-7B3FE4?style=flat-square&logo=stremio&logoColor=white" /></a>
  <a href="kodi/README.md"><img src="https://img.shields.io/badge/Kodi-Addon-17B2E7?style=flat-square&logo=kodi&logoColor=white" /></a>
</p>

<p align="center"><img src="https://socialify.git.ci/g0ldyy/comet/image?description=1&font=Inter&forks=1&language=1&name=1&owner=1&pattern=Solid&stargazers=1&theme=Dark" /></p>

# Features
- **CometNet**: Decentralized P2P network for automatic torrent metadata sharing ([documentation](docs/cometnet/README.md))
- **Kodi Support**: Dedicated official add-on with automatic updates ([documentation](kodi/README.md))
- Proxy Debrid Streams to allow simultaneous use on multiple IPs!
- IP-Based Max Connection Limit
- Administration Dashboard with Bandwidth Manager, Metrics and more...
- Supported Scrapers: Jackett, Prowlarr, Torrentio, Zilean, MediaFusion, Debridio, StremThru, AIOStreams, Comet, Jackettio, TorBox, Nyaa, BitMagnet, TorrentsDB, Peerflix, DMM and SeaDex
- Caching system ft. SQLite / PostgreSQL
- Blazing Fast Background Scraper
- Debrid Account Scraper: Scrape torrents directly from your debrid account library
- [DMM](https://github.com/debridmediamanager/hashlists) Ingester: Automatically download and index Debrid Media Manager hashlists
- Smart Torrent Ranking powered by [RTN](https://github.com/dreulavelle/rank-torrent-name)
- Proxy support to bypass debrid restrictions
- Real-Debrid, All-Debrid, Premiumize, TorBox, Debrid-Link, Debrider, EasyDebrid, OffCloud and PikPak supported
- Direct Torrent supported
- [Kitsu](https://kitsu.io/) support (anime)
- Adult Content Filter
- ChillLink Protocol support

# Installation
To customize your Comet experience to suit your needs, please first take a look at all the [environment variables](https://github.com/g0ldyy/comet/blob/main/.env-sample)!

## ElfHosted

A free, public Comet instance is available at https://comet.elfhosted.com, but if you need custom indexers, higher-rate-limits, or proxystreaming in a "turn-key" fashion, consider ElfHosted...

[ElfHosted](https://elfhosted.com) is a geeky [open-source](https://elfhosted.com/open/) PaaS which provides all the "plumbing" (*hosting, security, updates, etc*) for your self-hosted apps.

ElfHosted offer "one-click" [private Comet instances](https://elfhosted.com/app/comet/) bundled with Jackett and 64Mbps proxystreaming, allowing you to customize your indexers and streaming from multiple source IPs with the same RD token, without risking an account ban! (bandwidth boosters are available)

> [!IMPORTANT]
> Comet is a top-tier app in the [ElfHosted app catalogue](https://elfhosted.com/apps/). 30% of your subscription goes to the app developer :heart:

(*[ElfHosted Discord](https://discord.elfhosted.com)*)

## Self Hosted
### From source (developers)
- Clone the repository and enter the folder
    ```sh
    git clone https://github.com/g0ldyy/comet
    cd comet
    ```
- Install dependencies
    ```sh
    pip install uv
    uv sync
    ````
- Start Comet
    ```sh
    uv run python -m comet.main
    ````

### Docker / production-style setup

Use the dedicated documentation:

- Beginner step-by-step: [docs/beginner/01-get-started-docker.md](docs/beginner/01-get-started-docker.md)
- Full documentation index: [docs/README.md](docs/README.md)

# CometNet (P2P Network)
Comet transforms your Comet instance from an isolated scraper into a participant in a collaborative network. Instead of each instance independently discovering the same torrents, CometNet allows instances to share their discovered **metadata** (hashes, titles, etc.) with each other in a decentralized way. **No actual files are shared.**

Key benefits:
- **Improved Coverage**: Receive torrent metadata discovered by other nodes.
- **Reduced Load**: Less redundant scraping across the network.
- **Trust Pools**: Optional closed groups for trusted metadata sharing.

For more information on how to setup and configure CometNet, please refer to the [CometNet Documentation](docs/cometnet/README.md).

## Support the Project
Comet is a community-driven project, and your support helps it grow! 🚀

- ❤️ **Donate** via [GitHub Sponsors](https://github.com/sponsors/g0ldyy) or [Ko-fi](https://ko-fi.com/g0ldyy) to support development
- ⭐ **Star the repository** here on GitHub
- ⭐ **Star the add-on** on [stremio-addons.net](https://stremio-addons.net/addons/comet)
- 🐛 **Contribute** by reporting issues, suggesting features, or submitting PRs

## Web UI Showcase
<img src="https://i.imgur.com/7xY5AEi.png" />
<img src="https://i.imgur.com/Dzs4wax.png" />
<img src="https://i.imgur.com/L3RkfO8.jpeg" />
