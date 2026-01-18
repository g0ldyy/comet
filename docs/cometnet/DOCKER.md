# CometNet Docker Deployment

Complete Docker configurations for CometNet.

---

## Integrated Mode (Single Instance)

For simple deployments with a single Comet instance.

### docker-compose.yml

```yaml
services:
  comet:
    container_name: comet
    image: g0ldyy/comet
    restart: unless-stopped
    ports:
      - "8000:8000"
      - "8765:8765"  # CometNet P2P port
    environment:
      DATABASE_TYPE: postgresql
      DATABASE_URL: comet:comet@postgres:5432/comet
      COMETNET_ENABLED: "True"
      FASTAPI_WORKERS: "1"
    env_file:
      - .env
    volumes:
      - comet_data:/app/data
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    container_name: comet-postgres
    image: postgres:18-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: comet
      POSTGRES_PASSWORD: comet
      POSTGRES_DB: comet
    volumes:
      - postgres_data:/var/lib/postgresql/
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U comet -d comet"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  comet_data:
  postgres_data:
```

### .env

```env
# CometNet Configuration
COMETNET_BOOTSTRAP_NODES=["wss://bootstrap.example.com:8765"]
COMETNET_ADVERTISE_URL=wss://comet.yourdomain.com:8765

# Optional: For home connections
COMETNET_UPNP_ENABLED=True
```

---

## Relay Mode (Multi-Worker / Cluster)

For production deployments with multiple Comet workers or replicas.

### docker-compose.yml

```yaml
services:
  comet:
    container_name: comet
    image: g0ldyy/comet
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      DATABASE_TYPE: postgresql
      DATABASE_URL: comet:comet@postgres:5432/comet
      COMETNET_RELAY_URL: http://cometnet:8766
      COMETNET_API_KEY: ${COMETNET_API_KEY} # Secure the relay connection
      FASTAPI_WORKERS: "4"  # Can use multiple workers
    env_file:
      - .env
    volumes:
      - comet_data:/app/data
    depends_on:
      postgres:
        condition: service_healthy
      cometnet:
        condition: service_started

  cometnet:
    container_name: cometnet
    image: g0ldyy/comet
    restart: unless-stopped
    command: ["uv", "run", "python", "-m", "comet.cometnet.standalone"]
    ports:
      - "8765:8765"   # P2P WebSocket
      # - "8766:8766" # HTTP API (optional, only if needed externally)
    environment:
      DATABASE_TYPE: postgresql
      DATABASE_URL: comet:comet@postgres:5432/comet
      COMETNET_LISTEN_PORT: "8765"
      COMETNET_HTTP_PORT: "8766"
      COMETNET_API_KEY: ${COMETNET_API_KEY}
    env_file:
      - .env-cometnet
    volumes:
      - cometnet_data:/app/data
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:8766/health"]
      interval: 10s
      timeout: 5s
      retries: 3

  postgres:
    container_name: comet-postgres
    image: postgres:18-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: comet
      POSTGRES_PASSWORD: comet
      POSTGRES_DB: comet
    volumes:
      - postgres_data:/var/lib/postgresql/
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U comet -d comet"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  comet_data:
  cometnet_data:
  postgres_data:
```

### .env-cometnet

Create a separate environment file for the CometNet standalone service:

```env
# Network Discovery
COMETNET_BOOTSTRAP_NODES=["wss://bootstrap.example.com:8765"]
COMETNET_MANUAL_PEERS=[]

# Public URL (required for others to connect)
COMETNET_ADVERTISE_URL=wss://comet.yourdomain.com:8765

# Peer Limits
COMETNET_MAX_PEERS=50
COMETNET_MIN_PEERS=3

# Contribution Mode
COMETNET_CONTRIBUTION_MODE=full

# Optional: Trust Pools
# COMETNET_TRUSTED_POOLS=["my-community"]

# Optional: API Key for security (Recommended)
COMETNET_API_KEY=my-secret-key
```

---

## Scaling with Replicas

For high-availability deployments.

### docker-compose.yml

```yaml
services:
  comet:
    image: g0ldyy/comet
    deploy:
      replicas: 3
    environment:
      DATABASE_TYPE: postgresql
      DATABASE_URL: comet:comet@postgres:5432/comet
      COMETNET_RELAY_URL: http://cometnet:8766
      FASTAPI_WORKERS: "2"
    env_file:
      - .env
    volumes:
      - comet_data:/app/data
    depends_on:
      - postgres
      - cometnet

  cometnet:
    image: g0ldyy/comet
    command: ["uv", "run", "python", "-m", "comet.cometnet.standalone"]
    ports:
      - "8765:8765"
    environment:
      DATABASE_TYPE: postgresql
      DATABASE_URL: comet:comet@postgres:5432/comet
    env_file:
      - .env-cometnet
    volumes:
      - cometnet_data:/app/data
    depends_on:
      - postgres
    deploy:
      replicas: 1  # Only one CometNet instance needed

  load-balancer:
    image: nginx:alpine
    ports:
      - "8000:80"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
    depends_on:
      - comet

  postgres:
    image: postgres:18-alpine
    environment:
      POSTGRES_USER: comet
      POSTGRES_PASSWORD: comet
      POSTGRES_DB: comet
    volumes:
      - postgres_data:/var/lib/postgresql/

volumes:
  comet_data:
  cometnet_data:
  postgres_data:
```

---

## Private Network Deployment

For isolated CometNet networks.

### docker-compose.yml

```yaml
services:
  comet:
    container_name: comet
    image: g0ldyy/comet
    restart: unless-stopped
    ports:
      - "8000:8000"
      - "8765:8765"
    environment:
      DATABASE_TYPE: postgresql
      DATABASE_URL: comet:comet@postgres:5432/comet
      COMETNET_ENABLED: "True"
      FASTAPI_WORKERS: "1"
      COMETNET_PRIVATE_NETWORK: "True"
      COMETNET_NETWORK_ID: my-private-network
      COMETNET_NETWORK_PASSWORD: ${COMETNET_NETWORK_PASSWORD}
    env_file:
      - .env
    volumes:
      - comet_data:/app/data
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    container_name: comet-postgres
    image: postgres:18-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: comet
      POSTGRES_PASSWORD: comet
      POSTGRES_DB: comet
    volumes:
      - postgres_data:/var/lib/postgresql/
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U comet -d comet"]
      interval: 5s
      timeout: 5s
      retries: 5

volumes:
  comet_data:
  postgres_data:
```

### .env

```env
# Private Network Secret (keep this secure!)
COMETNET_NETWORK_PASSWORD=my-super-secret-password-change-me

# Add other private network members
COMETNET_MANUAL_PEERS=["wss://friend1.example.com:8765", "wss://friend2.example.com:8765"]

COMETNET_ADVERTISE_URL=wss://comet.yourdomain.com:8765
```

---

## Nginx Configuration for WSS

### With SSL termination at Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name comet.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/comet.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/comet.yourdomain.com/privkey.pem;

    # Comet HTTP API
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # CometNet WebSocket
    location /cometnet/ws {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
```

When using this configuration, set:
```env
COMETNET_ADVERTISE_URL=wss://comet.yourdomain.com/cometnet/ws
```

---

## Health Checks

### Check Comet
```bash
curl http://localhost:8000/health
```

### Check CometNet Standalone
```bash
curl http://localhost:8766/health
```

### Check CometNet Stats
```bash
curl http://localhost:8766/stats
```

### Check Connected Peers
```bash
curl http://localhost:8766/peers
```

---

## Troubleshooting

### Container fails to start

Check logs:
```bash
docker compose logs cometnet
```

### Port already in use

Change `COMETNET_LISTEN_PORT` to an available port.

### Cannot connect to relay

Ensure the cometnet service is healthy:
```bash
docker compose ps
```

### WebSocket connections failing

1. Verify firewall allows port 8765
2. Check Nginx WebSocket configuration
3. Verify `COMETNET_ADVERTISE_URL` is accessible from outside
