<h1 align="center" id="title">☄️ Comet - <a href="https://discord.gg/rivenmedia">Discord</a></h1>
<p align="center"><img src="https://socialify.git.ci/g0ldyy/comet/image?description=1&font=Inter&forks=1&language=1&name=1&owner=1&pattern=Solid&stargazers=1&theme=Dark" /></p>
<p align="center">
  <a href="https://ko-fi.com/E1E7ZVMAD">
    <img src="https://ko-fi.com/img/githubbutton_sm.svg">
  </a>
</p>

# Features
- The first Stremio addon to Proxy Debrid Streams to allow use of the Debrid Service on multiple IPs at the same time on the same account!
- IP-Based Max Connection Limit and Dashboard for Debrid Stream Proxier
- Jackett and Prowlarr support (change the `INDEXER_MANAGER_TYPE` environment variable to `jackett` or `prowlarr`)
- [Zilean](https://github.com/iPromKnight/zilean) ([DMM](https://hashlists.debridmediamanager.com/) Scraper) support for even more results
- [Torrentio](https://torrentio.strem.fun/) Scraper
- [MediaFusion](https://mediafusion.elfhosted.com/) Scraper
- Caching system ft. SQLite / PostgreSQL
- Smart Torrent Ranking powered by [RTN](https://github.com/dreulavelle/rank-torrent-name)
- Proxy support to bypass debrid restrictions
- Real-Debrid, All-Debrid, Premiumize, TorBox and Debrid-Link supported
- Direct Torrent supported
- [Kitsu](https://kitsu.io/) support (anime)
- Adult Content Filter
- [StremThru](https://github.com/MunifTanjim/stremthru) support

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

## Web UI Showcase
<img src="https://i.imgur.com/SaD365F.png" />
