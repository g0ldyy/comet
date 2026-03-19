# Get Started with Docker

This guide is for complete beginners.

By the end, you will have Comet running and reachable from your browser.

## What You Need

- A machine with Docker and Docker Compose installed.
- A terminal.
- A text editor.

## Step 1: Create a Working Directory

```bash
mkdir comet-deploy
cd comet-deploy
```

## Step 2: Copy the Docker Compose File

Copy `deployment/docker-compose.yml` from this repository into your working directory as `docker-compose.yml`.

This compose file starts:

- `comet` on port `8000`
- `postgres` as the database

## Step 3: Create a Minimal `.env`

Create a `.env` file in the same directory.

Example:

```env
# Change this before exposing Comet publicly
ADMIN_DASHBOARD_PASSWORD=change-me-now
```

Notes:

- Comet runtime defaults come from `AppSettings` in `comet/core/models.py`.
- `.env-sample` is a reference template of available options.

## Step 4: Start the Stack

```bash
docker compose up -d
```

## Step 5: Verify It Is Running

```bash
docker compose ps
docker compose logs -f comet
```

In logs, confirm startup information appears.

You can also check health:

- `http://<your-host>:8000/health` should return `{"status":"ok"}`.

## Step 6: Open Comet

Open:

- `http://<your-host>:8000/configure` for configuration
- `http://<your-host>:8000/admin` for admin dashboard login

If `ADMIN_DASHBOARD_PASSWORD` is not set, Comet generates one at startup and logs it.

## Step 7 (Recommended): Add a Reverse Proxy

For beginner self-hosting, a reverse proxy is the simplest path to use a domain name and HTTPS.

Requirements:
* An A record on your domain pointing towards your IP.
* Ports 443 and 80 open.

> [!TIP]
> You can also use services such as DuckDNS as a free alternative to buying a domain.

### Nginx

Comet includes a minimal nginx example in `deployment/nginx.conf`:

```nginx
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

Beginner checklist:

1. Replace `example.com` with your domain.
2. Ensure `proxy_pass` points to your Comet service.
3. Add HTTPS/TLS on the proxy before using Stremio from another device/network.

### Traefik

Add the following example from below into your compose file:

```traefik
  traefik:
    image: traefik:v3
    container_name: traefik
    restart: unless-stopped
    environment:
      - TZ=Etc/UTC # change this if needed
    ports:
      - 443:443
    command:
      - '--ping=true'
      - '--api=true'
      - '--api.dashboard=false'
      - '--api.insecure=false'
      - '--global.sendAnonymousUsage=false'
      - '--global.checkNewVersion=false'
      - '--log=true'
      - '--log.level=DEBUG'
      - "--providers.docker=true"
      - "--providers.docker.exposedbydefault=false"
      - "--entryPoints.websecure.address=:443"
      - "--certificatesresolvers.letsencrypt.acme.tlschallenge=true"
      - "--certificatesresolvers.letsencrypt.acme.email=youremail@example.com"
      - "--certificatesresolvers.letsencrypt.acme.storage=/config/acme.json"
    volumes:
      - "/var/run/docker.sock:/var/run/docker.sock"
      - "./traefik:/config"
    healthcheck:
      test: ["CMD", "traefik", "healthcheck", "--ping"]
      interval: 10s
      timeout: 5s
      retries: 3
```

 * Replace "youremail@example.com" with your email (used for Let's Encrypt)

 Then, add this to the labels of your comet:

 ```cometlabels
     labels:
      - "traefik.enable=true"
      - "traefik.http.routers.comet.rule=Host(`comet.example.com`)"
      - "traefik.http.routers.comet.entrypoints=websecure"
      - "traefik.http.routers.comet.tls.certresolver=letsencrypt"
```

Example:

```cometlabelexample
services:
  comet:
    container_name: comet
    image: g0ldyy/comet
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      DATABASE_TYPE: ${DATABASE_TYPE:-postgresql}
      DATABASE_URL: ${DATABASE_URL:-comet:comet@postgres:5432/comet}
    env_file:
      - .env
    volumes:
      - comet_data:/app/data
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:8000/health"]
      interval: 5s
      timeout: 5s
      retries: 5
      start_period: 10s
    depends_on:
      postgres:
        condition: service_healthy
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.comet.rule=Host(`comet.example.com`)"
      - "traefik.http.routers.comet.entrypoints=websecure"
      - "traefik.http.routers.comet.tls.certresolver=letsencrypt"
```

 * Replace `comet.example.com` with your hostname.

Now run this command:
```
sudo docker compose up -d
```


## Next

Continue with [Configure and Install in Stremio](02-configure-and-install-stremio.md).
