# CometNet Documentation

CometNet is a decentralized peer-to-peer network built into Comet that enables automatic sharing of torrent metadata between instances. When you discover a torrent on your instance, it can be propagated to other nodes in the network and vice versa - dramatically improving content coverage for all participants.

This documentation covers everything you need to set up and configure CometNet for your deployment.

---

## Table of Contents

1. [Overview](#overview)
2. [How It Works](#how-it-works)
3. [Deployment Modes](#deployment-modes)
   - [Integrated Mode](#integrated-mode)
   - [Relay Mode](#relay-mode)
4. [Quick Start](QUICKSTART.md)
5. [Configuration Reference](#configuration-reference)
   - [Core Settings](#core-settings)
   - [Network Discovery](#network-discovery)
   - [Peer Management](#peer-management)
   - [Identity & Security](#identity--security)
   - [Contribution Modes](#contribution-modes)
   - [Trust Pools](#trust-pools)
   - [Private Networks](#private-networks)
   - [Advanced Tuning](#advanced-tuning)
6. [Trust Pools](#trust-pools-1)
   - [Creating a Pool](#creating-a-pool)
   - [Joining a Pool](#joining-a-pool)
   - [Managing Members](#managing-members)
7. [Network Architecture](#network-architecture)
8. [Security Considerations](#security-considerations)
9. [Troubleshooting](#troubleshooting)

---

## Overview

CometNet transforms your Comet instance from an isolated scraper into a participant in a collaborative network. Instead of each instance independently discovering the same torrents, CometNet allows instances to share their discoveries with each other.

**Key Benefits:**

- **Improved Coverage**: Receive torrents discovered by other nodes, even from sources you don't scrape directly.
- **Reduced Load**: Less redundant scraping across the network since discoveries are shared.
- **Faster Updates**: New releases propagate quickly through the network.
- **Community Trust**: Trust Pools allow you to create closed groups with trusted contributors.

**Important Notes:**

- CometNet is an **experimental feature** and may have bugs or breaking changes.
- CometNet shares **metadata only** (titles, hashes, sizes) - not actual torrent files or content.
- All propagated data is cryptographically signed to ensure authenticity.

---

## How It Works

CometNet uses a gossip-based protocol to propagate torrent metadata across the network:

1. **Discovery**: When your Comet instance discovers a new torrent (from any scraper), it is signed with your node's private key.

2. **Gossip**: The signed torrent is sent to a random subset of your connected peers (fanout).

3. **Propagation**: Each peer validates the signature, stores the torrent, and forwards it to their own peers.

4. **Deduplication**: Messages are deduplicated to prevent flooding - each torrent announcement is only processed once.

5. **Reputation**: Nodes build reputation based on the quality of their contributions. Bad actors are automatically deprioritized.

The network uses WebSocket connections for peer-to-peer communication, with optional encryption via `wss://` (TLS).

---

## Deployment Modes

CometNet offers two deployment modes to fit different infrastructure setups.

### Integrated Mode

**Best for**: Simple setups with a single Comet instance.

In Integrated Mode, CometNet runs directly within your Comet process. This is the simplest setup but has a limitation: it only works with a single worker (`FASTAPI_WORKERS=1`) because multiple workers cannot share the same P2P port.

**Configuration:**
```env
COMETNET_ENABLED=True
FASTAPI_WORKERS=1
```

### Relay Mode

**Best for**: Production deployments with multiple workers or replicas.

In Relay Mode, you run a standalone CometNet service (a separate process) alongside your Comet instances. Your Comet workers send torrents to this standalone service via HTTP, and the standalone service handles all P2P networking.

**Configuration on Comet instances:**
```env
COMETNET_RELAY_URL=http://cometnet:8766
```

When `COMETNET_RELAY_URL` is set, the `COMETNET_ENABLED` setting is ignored - Comet will use the relay instead.

**Running the standalone service:**
```bash
uv run python -m comet.cometnet.standalone
```

The standalone service exposes:
- **WebSocket port** (default `8765`) for P2P connections
- **HTTP port** (default `8766`) for the relay API

---

## Configuration Reference

### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_ENABLED` | `False` | Enable Integrated Mode. Set to `True` for single-instance deployments. |
| `COMETNET_LISTEN_PORT` | `8765` | WebSocket port for incoming P2P connections. |
| `COMETNET_HTTP_PORT` | `8766` | HTTP API port (standalone service only). |
| `COMETNET_RELAY_URL` | *(empty)* | URL of standalone CometNet service. When set, Integrated Mode is disabled. |
| `COMETNET_API_KEY` | *(empty)* | Optional API key for standalone service authentication. When set, the relay and standalone service require this key for API access. |

### Network Discovery

CometNet uses two methods to find peers:

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_BOOTSTRAP_NODES` | `[]` | JSON array of public entry points. Format: `'["wss://node1:8765", "wss://node2:8765"]'` |
| `COMETNET_MANUAL_PEERS` | `[]` | JSON array of trusted peers to always connect to. Format: `'["ws://friend:8765"]'` |

**Bootstrap Nodes** are public servers that help new nodes discover peers. They're optional but recommended if you don't have manual peers configured.

**Manual Peers** are nodes you explicitly trust and want to stay connected to. They're prioritized over discovered peers.

### Peer Management

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_MAX_PEERS` | `50` | Maximum simultaneous connections. More peers = more bandwidth. |
| `COMETNET_MIN_PEERS` | `3` | Minimum desired peers. CometNet actively discovers if below this. |

### Identity & Security

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_KEYS_DIR` | `data/cometnet` | Directory to store your node's identity keys. |
| `COMETNET_ADVERTISE_URL` | *(empty)* | **Required if behind a reverse proxy.** Your public WebSocket URL (e.g., `wss://comet.example.com/cometnet/ws`). |
| `COMETNET_KEY_PASSWORD` | *(empty)* | Optional password to encrypt your private key on disk. |

**Identity Persistence:**

CometNet generates a unique Ed25519 keypair when first started. This keypair:
- Identifies your node on the network (your node ID is derived from your public key)
- Signs all your contributions (other nodes verify your signatures)
- Is stored in `COMETNET_KEYS_DIR`

If you lose your keys, you'll appear as a new node and lose any built-up reputation.

### Contribution Modes

Control what your node shares and receives:

| Mode | Shares Own Torrents | Receives | Repropagates | Use Case |
|------|---------------------|----------|--------------|----------|
| `full` | Yes | Yes | Yes | Default. Full network participation. |
| `consumer` | No | Yes | Yes | Receive and help propagate, but don't share your discoveries. |
| `source` | Yes | No | No | Dedicated scraper that only contributes. |
| `leech` | No | Yes | No | Selfish mode. Receives but doesn't help the network. |

**Configuration:**
```env
COMETNET_CONTRIBUTION_MODE=full
```

### Trust Pools

Trust Pools allow you to create private groups where only members can contribute torrents.

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_TRUSTED_POOLS` | `[]` | JSON array of pool IDs to accept torrents from. Empty = accept from everyone (open mode). |
| `COMETNET_POOLS_DIR` | `data/cometnet/pools` | Storage directory for pool data. |

**Example:**
```env
COMETNET_TRUSTED_POOLS='["my-community", "french-scene"]'
```

When `COMETNET_TRUSTED_POOLS` is set, your node will only accept torrents from members of the specified pools.

### Private Networks

Create completely isolated CometNet networks:

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_PRIVATE_NETWORK` | `False` | Enable private network mode. |
| `COMETNET_NETWORK_ID` | *(empty)* | Unique identifier for your private network. Required if private mode is enabled. |
| `COMETNET_NETWORK_PASSWORD` | *(empty)* | Shared secret to join the network. Required if private mode is enabled. |
| `COMETNET_INGEST_POOLS` | `[]` | Pool IDs to ingest from public network even in private mode. |

Private networks are completely separate from the public CometNet network. All nodes in a private network must share the same `NETWORK_ID` and `NETWORK_PASSWORD`.

### Advanced Tuning

#### Gossip Protocol

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_GOSSIP_FANOUT` | `3` | Number of peers to forward each message to. Higher = faster propagation, more bandwidth. |
| `COMETNET_GOSSIP_INTERVAL` | `1.0` | Seconds between gossip rounds. |
| `COMETNET_GOSSIP_MESSAGE_TTL` | `5` | Maximum hops a message can travel. |
| `COMETNET_GOSSIP_MAX_TORRENTS_PER_MESSAGE` | `1000` | Maximum torrents per gossip message. |
| `COMETNET_GOSSIP_CACHE_TTL` | `300` | Seconds to remember seen messages (deduplication). |
| `COMETNET_GOSSIP_CACHE_SIZE` | `10000` | Maximum number of seen messages to cache. |

#### Validation

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_GOSSIP_VALIDATION_FUTURE_TOLERANCE` | `60` | Seconds tolerance for future timestamps (clock drift). |
| `COMETNET_GOSSIP_VALIDATION_PAST_TOLERANCE` | `300` | Seconds tolerance for past timestamps. |
| `COMETNET_GOSSIP_TORRENT_MAX_AGE` | `604800` | Maximum age (7 days) for accepting torrent updates. |

#### Peer Discovery

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_PEX_BATCH_SIZE` | `20` | Number of peers shared in Peer Exchange responses. |
| `COMETNET_PEER_CONNECT_BACKOFF_MAX` | `300` | Maximum seconds before reconnecting to a failed peer. |
| `COMETNET_PEER_MAX_FAILURES` | `5` | Failures before temporarily banning a peer. |
| `COMETNET_PEER_CLEANUP_AGE` | `604800` | Seconds (7 days) to keep inactive peers. |
| `COMETNET_ALLOW_PRIVATE_PEX` | `False` | Allow private/internal IPs via Peer Exchange. Enable for LAN setups. |

#### Transport

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_TRANSPORT_MAX_MESSAGE_SIZE` | `10485760` | Maximum WebSocket message size (10MB). |
| `COMETNET_TRANSPORT_MAX_CONNECTIONS_PER_IP` | `3` | Maximum connections from a single IP (anti-Sybil). |
| `COMETNET_TRANSPORT_PING_INTERVAL` | `30.0` | Seconds between keepalive pings. |
| `COMETNET_TRANSPORT_CONNECTION_TIMEOUT` | `120.0` | Seconds before dropping a silent connection. |

#### NAT Traversal

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_UPNP_ENABLED` | `False` | Enable UPnP to automatically open ports on your router. |
| `COMETNET_UPNP_LEASE_DURATION` | `3600` | UPnP port mapping lease duration (seconds). |

#### Reputation System

The reputation system tracks peer quality and filters bad actors:

| Variable | Default | Description |
|----------|---------|-------------|
| `COMETNET_REPUTATION_INITIAL` | `100.0` | Starting reputation for new peers. |
| `COMETNET_REPUTATION_MIN` | `0.0` | Minimum reputation score. |
| `COMETNET_REPUTATION_MAX` | `10000.0` | Maximum reputation score. |
| `COMETNET_REPUTATION_THRESHOLD_TRUSTED` | `1000.0` | Score needed to be considered "trusted". |
| `COMETNET_REPUTATION_THRESHOLD_UNTRUSTED` | `50.0` | Score below which a peer is ignored. |
| `COMETNET_REPUTATION_BONUS_VALID_CONTRIBUTION` | `0.001` | Bonus per valid torrent contributed. |
| `COMETNET_REPUTATION_BONUS_PER_DAY_ANCIENNETY` | `10.0` | Daily bonus for long-running peers. |
| `COMETNET_REPUTATION_PENALTY_INVALID_CONTRIBUTION` | `50.0` | Penalty for sending bad data. |
| `COMETNET_REPUTATION_PENALTY_SPAM_DETECTED` | `100.0` | Penalty for spamming. |
| `COMETNET_REPUTATION_PENALTY_INVALID_SIGNATURE` | `500.0` | Penalty for invalid signatures. |

---

## Trust Pools

Trust Pools allow you to create communities of trusted contributors. Only pool members can contribute torrents that other members will accept.

### Creating a Pool

1. Navigate to the Admin Dashboard → CometNet tab.
2. Click "Create Pool".
3. Enter:
   - **Pool ID**: Unique identifier (lowercase, dashes allowed)
   - **Display Name**: Human-readable name
   - **Description**: What this pool is for
4. Click "Create".

You become the pool creator and administrator.

### Joining a Pool

**Via Invite Link:**

1. Get an invite link from a pool administrator.
2. In the Admin Dashboard → CometNet tab, click "Join Pool".
3. Paste the invite link.
4. Click "Join".

The invite link format is:
```
cometnet://join?pool=pool-id&code=invite-code&node=wss://admin-node:8765
```

### Managing Members

Pool creators and admins can:

- **View Members**: See all pool members and their roles.
- **Create Invites**: Generate invite links with optional expiration or usage limits.
- **Promote to Admin**: Give members administrative privileges.
- **Demote Admins**: Remove admin privileges.
- **Kick Members**: Remove members from the pool.
- **Delete Pool**: Permanently remove the pool (creator only).

**Member Roles:**

| Role | Can Invite | Can Kick | Can Promote/Demote | Can Delete Pool |
|------|------------|----------|-------------------|-----------------|
| Creator | Yes | Yes | Yes | Yes |
| Admin | Yes | Yes | Yes (except creator) | No |
| Member | No | No | No | No |

---

## Network Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        CometNet Network                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐     WebSocket      ┌──────────────┐          │
│  │   Node A     │◄──────────────────►│   Node B     │          │
│  │  (Scraper)   │                    │  (Scraper)   │          │
│  └──────┬───────┘                    └──────┬───────┘          │
│         │                                   │                   │
│         │ Gossip                            │ Gossip            │
│         ▼                                   ▼                   │
│  ┌──────────────┐                    ┌──────────────┐          │
│  │   Node C     │                    │   Node D     │          │
│  │  (Consumer)  │◄──────────────────►│  (Consumer)  │          │
│  └──────────────┘                    └──────────────┘          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Components:**

1. **Identity (Crypto)**: Ed25519 keypair for signing and node identification.
2. **Transport**: WebSocket connections with keepalive and automatic reconnection.
3. **Discovery**: Finds peers via bootstrap nodes, manual peers, and Peer Exchange (PEX).
4. **Gossip Engine**: Propagates torrents with signature verification and deduplication.
5. **Reputation Store**: Tracks peer quality and filters bad actors.
6. **Pool Store**: Manages Trust Pools and memberships.

---

## Security Considerations

### Cryptographic Signing

- Every torrent contribution is signed with the contributor's Ed25519 private key.
- Signatures are verified before accepting any data.
- Invalid signatures result in reputation penalties and potential disconnection.

### Anti-Abuse Measures

- **Timestamp Validation**: Old or future-dated messages are rejected.
- **Sybil Resistance**: Connections per IP are limited.
- **Deduplication**: Prevents message flooding.

### Recommendations

1. **Use TLS**: Configure `wss://` instead of `ws://` for encrypted connections.
2. **Encrypt Keys**: Set `COMETNET_KEY_PASSWORD` to encrypt your private key on disk.
3. **Trust Pools**: In production, use Trust Pools to limit who can contribute.
4. **Firewall**: Only expose CometNet ports to trusted networks or the internet if necessary.
5. **API Key for Standalone**: When running the standalone service, set `COMETNET_API_KEY` to protect the HTTP API from unauthorized access.

### What CometNet Does NOT Protect Against

- A malicious majority in a pool (if >50% of trusted contributors are malicious)
- Denial of service (flooding with valid but useless torrents)
- Metadata quality (CometNet doesn't validate torrent content, only signatures)

---

## Troubleshooting

### No Peers Connecting

1. **Check firewall**: Ensure `COMETNET_LISTEN_PORT` (default 8765) is accessible.
2. **Behind NAT?** Enable `COMETNET_UPNP_ENABLED=True` or manually forward the port.
3. **Verify bootstrap nodes**: Ensure they're online and using correct addresses.
4. **Check logs**: Look for "CometNet started" and connection attempts.

### Not Receiving Torrents

1. **Contribution mode**: Ensure you're not in `source` mode (which doesn't receive).
2. **Trust Pools**: If `COMETNET_TRUSTED_POOLS` is set, ensure you're subscribed to active pools.
3. **Peer count**: Check the Admin Dashboard for connected peers.

### Not Sharing Torrents

1. **Contribution mode**: Ensure you're in `full` or `source` mode.
2. **Scrapers enabled**: CometNet shares what your scrapers find - if nothing is scraped, nothing is shared.

### Pool Sync Issues

1. **Version mismatch**: Pool manifests sync automatically. Wait a few minutes.
2. **Creator offline**: The pool creator's node must be reachable for new join requests.

### High Memory/CPU

1. Reduce `COMETNET_MAX_PEERS` (fewer connections = less overhead).
2. Reduce `COMETNET_GOSSIP_CACHE_SIZE` (smaller deduplication cache).
3. Increase `COMETNET_GOSSIP_INTERVAL` (less frequent gossiping).

### Logs and Debugging

CometNet logs under the `COMETNET` tag. Key events to watch for:

```
CometNet started - Node ID: abc123...
Discovery service started with 2 known peers
Connected to peer def456...
Received 10 torrents from peer abc123...
```

---

## Support

For issues specific to CometNet, please include:

1. Your deployment mode (Integrated or Relay)
2. Relevant settings (contribution mode, pools, etc.)
3. CometNet-specific log entries

Join the Comet Discord for community support and discussion.
