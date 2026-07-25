# Torznab Client Integration

Comet exposes a Torznab feed for Movies and TV. The complete endpoint is:

```text
https://comet.example.com[/s/<token>]/torznab/api
```

Replace the example host and optional protected prefix with values reachable
from the client. Inside Docker, use the Comet service name and container port,
for example `http://comet:8000/torznab/api`.

Check connectivity before configuring a client. For an unprotected instance:

```sh
curl --fail-with-body \
  "https://comet.example.com/torznab/api?t=caps"
```

For an instance using a protected prefix:

```sh
curl --fail-with-body \
  "https://comet.example.com/s/<token>/torznab/api?t=caps"
```

The response must contain the `Movies` (`2000`) and `TV` (`5000`) categories.
On a new Comet instance, run one successful media search before the first
indexer test.

## Prowlarr

Use Prowlarr's built-in **Generic Torznab** indexer. It consumes Comet directly
and should be preferred over a custom YAML definition.

| Setting | Value |
| --- | --- |
| Name | `Comet` |
| URL | `https://comet.example.com[/s/<token>]/torznab` |
| API Path | `/api` |
| API Key | Empty |
| Minimum Seeders | `0` |

Test and save the indexer, then let Prowlarr sync it to Sonarr and Radarr. Do
not also add Comet directly to those applications, otherwise the same releases
can be queried twice.

## Sonarr and Radarr

Configure Comet directly only when Prowlarr is not managing indexers. Add a
**Torznab** indexer with the same URL, API Path, and empty API Key used above.

| Application | Categories | Minimum Seeders |
| --- | --- | --- |
| Sonarr | `5000 - TV` | `0` |
| Radarr | `2000 - Movies` | `0` |

Keep RSS, automatic search, and interactive search enabled. Each application
will limit itself to the search modes and identifiers declared by Comet's
capabilities response.

## Jackett

Jackett needs the bundled Cardigann definition:

[`integrations/torznab/comet.yml`](../../integrations/torznab/comet.yml)

1. Copy `comet.yml` into a writable Jackett definitions directory. Jackett
   prints the active definition directories in its startup log.
2. Restart Jackett.
3. Add the **Comet** indexer.
4. Set **Comet Torznab endpoint** to the complete endpoint, including `/api`
   and the protected prefix when one is configured.
5. Test and save the indexer.

The definition forwards text, IMDb, season, episode, category, and recent-feed
queries while preserving Comet's magnets, hashes, sizes, dates, and seeders.
Use a direct Torznab connection or Prowlarr when possible; Jackett adds an
extra XML parsing and serialization layer.

## Troubleshooting

- A connection failure usually means the client container cannot resolve or
  reach Comet. Test the endpoint from inside that container.
- A `Function not available` response to `t=get` is expected because Comet
  does not implement that function. For search requests, check `t=caps`:
  `available="no"` means `DISABLE_TORRENT_STREAMS` is enabled.
- An empty test on a fresh instance usually means Comet has not completed its
  first successful media search.
- Keep `/api` in exactly one place: either use the complete endpoint, or split
  it between the URL and API Path fields as shown above.
