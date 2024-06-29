<h1 align="center" id="title">☄️ Comet - <a href="https://discord.gg/rivenmedia">Discord</a></h1>
<p align="center"><img src="https://socialify.git.ci/g0ldyy/comet/image?description=1&font=Raleway&forks=1&issues=1&language=1&logo=https%3A%2F%2Fi.imgur.com%2FGj0KQwB.png&name=1&owner=1&pattern=Solid&pulls=1&stargazers=1&theme=Dark" /></p>
<p align="center">
  <a href="https://ko-fi.com/E1E7ZVMAD">
    <img src="https://ko-fi.com/img/githubbutton_sm.svg">
  </a>
</p>

# Features
- Jackett and Prowlarr support (change the `INDEXER_MANAGER_TYPE` environment variable to `jackett` or `prowlarr`)
- Caching system ft. SQLite
- Proxy support to bypass debrid restrictions
- Only Real-Debrid supported right now *(if you want other debrid services, please provide an account)*

# Installation
## With Docker
- Simply run the Docker image after modifying the environment variables
  ```
  docker run -p 8000:8000 -d \
      --name comet \
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

# Real-Debrid IP Blacklist
To bypass Real-Debrid's IP blacklist, start a cloudflare-warp container: https://github.com/cmj2002/warp-docker

# Web UI Showcase
<img src="https://i.imgur.com/SaD365F.png" />
