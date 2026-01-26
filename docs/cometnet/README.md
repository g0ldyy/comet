# CometNet Documentation

Welcome to the CometNet documentation. CometNet is a decentralized peer-to-peer network integrated into Comet that automatically shares torrent metadata between instances.

## Documentation

| Document | Description |
|----------|-------------|
| [Quick Start](QUICKSTART.md) | Get CometNet running in 5 minutes |
| [Full Documentation](COMETNET.md) | Complete reference with all settings and features |
| [Docker Deployment](DOCKER.md) | Docker-specific configurations and examples |

## Overview

CometNet enables Comet instances to share discovered torrent **metadata** with each other automatically. When your instance finds a new torrent, its **metadata** (hash, title, size) is propagated to other nodes in the network - and you receive metadata discovered by others. No actual files are shared.

### Key Features

- **Peer-to-peer**: No central server, fully distributed
- **Cryptographically signed**: All contributions are verified
- **Trust Pools**: Create private communities of trusted contributors
- **Contribution modes**: Control what you share and receive
- **Reputation system**: Bad actors are automatically filtered

## Need Help?

- Check the [Troubleshooting](COMETNET.md#troubleshooting) section
- Join the [Comet Discord](https://discord.com/invite/UJEqpT42nb)
