<h1 align="center" id="title">☄️ Comet</h1>
<p align="center"><img src="https://socialify.git.ci/g0ldyy/comet/image?description=1&font=Raleway&forks=1&issues=1&language=1&logo=https%3A%2F%2Fi.imgur.com%2FGj0KQwB.png&name=1&owner=1&pattern=Solid&pulls=1&stargazers=1&theme=Dark" alt="comet" width="640" height="320" /></p>

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
      -e JACKETT_URL=http://127.0.0.1:9117 \
      -e JACKETT_KEY=XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX \
      -e JACKETT_INDEXERS=INDEXER_NAME_CHANGETHIS1,INDEXER_NAME_CHANGETHIS2 \
      -e JACKETT_TIMEOUT=30 \
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
