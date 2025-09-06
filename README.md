<h1 align="center" id="title">☄️ Comet - <a href="https://discord.com/invite/UJEqpT42nb">New Discord</a></h1>
<p align="center"><img src="https://socialify.git.ci/g0ldyy/comet/image?description=1&font=Inter&forks=1&language=1&name=1&owner=1&pattern=Solid&stargazers=1&theme=Dark" /></p>

# Features
- The first Stremio addon to Proxy Debrid Streams to allow use of the Debrid Service on multiple IPs at the same time on the same account!
- IP-Based Max Connection Limit
- Administration Dashboard with Bandwidth Manager, Metrics and more...
- Supported Scrapers: Jackett, Prowlarr, Torrentio, Zilean, MediaFusion, Debridio, StremThru, AIOStreams, Comet, Jackettio and TorBox
- Caching system ft. SQLite / PostgreSQL
- Blazing Fast Background Scraper
- Smart Torrent Ranking powered by [RTN](https://github.com/dreulavelle/rank-torrent-name)
- Proxy support to bypass debrid restrictions
- Real-Debrid, All-Debrid, Premiumize, TorBox, Debrid-Link, Debrider, EasyDebrid, OffCloud and PikPak supported
- Direct Torrent supported
- [Kitsu](https://kitsu.io/) support (anime)
- Adult Content Filter

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
### From source
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

### With Docker Compose
- Copy *deployment/docker-compose.yml* in a directory
- Copy *.env-sample* to *.env* in the same directory and keep only the variables you wish to modify, also remove all comments
- Pull the latest version from docker hub
    ```sh
      docker compose pull
    ```
- Run
    ```sh
      docker compose up -d
    ```

### Nginx Reverse Proxy
If you want to serve Comet via a Nginx Reverse Proxy, here's the configuration you should use.
```
server {
    server_name example.com;

    location / {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Support the Project
Comet is a community-driven project, and your support helps it grow! 🚀

- ⭐ **Star the repo** here on GitHub
- ⭐ **Star the addon** on [stremio-addons.net](https://stremio-addons.net/addons/comet)  
- 🐛 **Contribute** by reporting issues, suggesting features, or submitting PRs  
- ❤️ **Donate** via [Ko-fi](https://ko-fi.com/g0ldyy) to support development

## Web UI Showcase
<img src="https://i.imgur.com/7xY5AEi.png" />
<img src="https://i.imgur.com/Dzs4wax.png" />
<img src="https://i.imgur.com/L3RkfO8.jpeg" />
