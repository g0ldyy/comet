<p align="center"><img src="https://i.imgur.com/mkpkD6K.png" /></p>
<h1 align="center" id="title">☄️ Comet - <a href="https://discord.gg/rivenmedia">Discord</a></h1>
<p align="center"><img src="https://socialify.git.ci/g0ldyy/comet/image?description=1&font=Inter&forks=1&language=1&name=1&owner=1&pattern=Solid&stargazers=1&theme=Dark" /></p>
<p align="center">
  <a href="https://ko-fi.com/E1E7ZVMAD">
    <img src="https://ko-fi.com/img/githubbutton_sm.svg">
  </a>
</p>

# Features
- The only Stremio addon that can Proxy Debrid Streams to allow use of the Debrid Service on multiple IPs at the same time on the same account!
- IP-Based Max Connection Limit and Dashboard for Debrid Stream Proxier
- Jackett and Prowlarr support (change the `INDEXER_MANAGER_TYPE` environment variable to `jackett` or `prowlarr`)
- [Zilean](https://github.com/iPromKnight/zilean) ([DMM](https://hashlists.debridmediamanager.com/) Scraper) support for even more results
- [Torrentio](https://torrentio.strem.fun/) Scraper
- Caching system ft. SQLite / PostgreSQL
- Smart Torrent Ranking powered by [RTN](https://github.com/dreulavelle/rank-torrent-name)
- Proxy support to bypass debrid restrictions
- Real-Debrid, All-Debrid, Premiumize, TorBox and Debrid-Link supported
- Direct Torrent supported (do not specify a Debrid API Key on the configuration page (webui) to activate it - it will use the cached results of other users using debrid service)
- [Kitsu](https://kitsu.io/) support (anime)
- Adult Content Filter

# Installation
To customize your Comet experience to suit your needs, please first take a look at all the [environment variables](https://github.com/g0ldyy/comet/blob/main/.env-sample)!
## ElfHosted
A free, public Comet instance is available at https://comet.elfhosted.com

[ElfHosted](https://elfhosted.com) is a geeky [open-source](https://elfhosted.com/open/) PaaS which provides all the "plumbing" (*hosting, security, updates, etc*) for your self-hosted apps. 

ElfHosted offer "one-click" [private Comet instances](https://elfhosted.com/app/comet/), allowing you to customize your indexers, and enabling "Proxy Stream" mode, to permit streaming from multiple source IPs with the same RD token!

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
    pip install poetry
    poetry install
    ````
- Start Comet
    ```sh
    poetry run python -m comet.main
    ````

### With Docker
- Simply run the Docker image after modifying the environment variables
  ```sh
  docker run --name comet -p 8000:8000 -d \
      -e FASTAPI_HOST=0.0.0.0 \
      -e FASTAPI_PORT=8000 \
      -e FASTAPI_WORKERS=1 \
      -e CACHE_TTL=86400 \
      -e DEBRID_PROXY_URL=http://127.0.0.1:1080 \
      -e INDEXER_MANAGER_TYPE=jackett \
      -e INDEXER_MANAGER_URL=http://127.0.0.1:9117 \
      -e INDEXER_MANAGER_API_KEY=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX \
      -e INDEXER_MANAGER_INDEXERS='["EXAMPLE1_CHANGETHIS", "EXAMPLE2_CHANGETHIS"]' \
      -e INDEXER_MANAGER_TIMEOUT=30 \
      -e GET_TORRENT_TIMEOUT=5 \
      g0ldyy/comet
  ```
    - To update your container

        - Find your existing container name
      ```sh
      docker ps
      ```

        - Stop your existing container
      ```sh
      docker stop <CONTAINER_ID>
      ```

        - Remove your existing container
      ```sh
      docker rm <CONTAINER_ID>
      ```

        - Pull the latest version from docker hub
      ```sh
      docker pull g0ldyy/comet
      ```

    - Finally, re-run the docker run command
 
### With Docker Compose
- Copy *compose.yaml* in a directory
- Copy *env-sample* to *.env* in the same directory
- Pull the latest version from docker hub
    ```sh
      docker compose pull
    ```
- Run
    ```sh
      docker compose up -d
    ```

## Debrid IP Blacklist
To bypass Real-Debrid's (or AllDebrid) IP blacklist, start a cloudflare-warp container: https://github.com/cmj2002/warp-docker

## Web UI Showcase
<img src="https://i.imgur.com/SaD365F.png" />
