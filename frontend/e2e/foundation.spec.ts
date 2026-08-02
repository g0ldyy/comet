import { expect, type Page, test } from "@playwright/test";

const envelope = (data: unknown) => ({
  data,
  meta: { request_id: "playwright-request" },
});

interface ActivityBucket {
  active: number;
  bytes_transferred: number;
  completed: number;
  failed: number;
  interrupted: number;
  peak_active: number | null;
  started_at: number;
}

function activityBuckets(
  startedAt: number,
  endedAt: number,
  bucketSeconds: number,
  values: Readonly<Record<number, Partial<Omit<ActivityBucket, "started_at">>>>,
): ActivityBucket[] {
  return Array.from({ length: (endedAt - startedAt) / bucketSeconds }, (_, index) => ({
    active: 0,
    bytes_transferred: 0,
    completed: 0,
    failed: 0,
    interrupted: 0,
    peak_active: null,
    started_at: startedAt + index * bucketSeconds,
    ...values[index],
  }));
}

const operationalEvent = {
  category: "SCRAPER",
  connection_id: null,
  created_at: 1_785_000_000,
  details: { result_count: 12 },
  error_code: null,
  event: "search.completed",
  id: 41,
  instance_id: "a".repeat(32),
  level: "INFO",
  media_type: "movie",
  message: "Search completed with 12 results",
  outcome: "ok",
  process_id: 42,
  provider_name: "torrentio",
  request_id: "b".repeat(32),
  role: "web_worker",
  run_id: null,
};

async function activityMarkBounds(page: Page, mode: string, selector: string) {
  const activity = page.locator(".stream-activity");
  await activity.getByRole("button", { name: mode }).click();
  const mark = activity.locator(selector).first();
  await expect(mark).toBeVisible();
  return activity.evaluate((element, markSelector) => {
    const axis = element.querySelector<SVGLineElement>(
      ".recharts-yAxis .recharts-cartesian-axis-line",
    );
    const chartMark = element.querySelector<SVGGraphicsElement>(markSelector);
    if (axis === null || chartMark === null) {
      throw new Error("Activity chart geometry is unavailable");
    }
    return [axis.x1.baseVal.value, chartMark.getBBox().x] as const;
  }, selector);
}

test.beforeEach(async ({ page }, testInfo) => {
  let adminAuthenticated = true;
  let metricCollection = 1_785_000_000;
  let settingLimit = 50;
  let settingPrometheus = false;
  let settingScrapeMode: boolean | string = false;
  let settingRevision = 4;
  const auditItems = testInfo.title.includes("settings audit")
    ? [
        {
          action: "update",
          changed_at: 1_785_000_000,
          changed_by: "admin",
          id: "audit-1",
          key: "HTTP_CLIENT_LIMIT",
          next_source: "database",
          previous_source: "default",
          revision: 5,
        },
      ]
    : [];
  const configuration = {
    cachedOnly: false,
    debridServices: [],
    debridStreamProxyPassword: "",
    enableTorrent: true,
    languages: { allowed: [], exclude: [], preferred: [], required: [] },
    maxResultsPerResolution: 0,
    maxSize: 0,
    options: {
      allow_english_in_languages: false,
      remove_unknown_languages: false,
    },
    removeTrash: true,
    resolutions: {},
    resultFormat: ["all"],
    schemaVersion: 1,
    scrapeDebridAccountTorrents: false,
  };
  await page.route("**/api/v1/auth/session", (route) =>
    route.fulfill(
      adminAuthenticated
        ? {
            contentType: "application/json",
            body: JSON.stringify(
              envelope({ authenticated: true, csrf_token: "csrf", expires_in: 3600 }),
            ),
          }
        : {
            contentType: "application/json",
            status: 401,
            body: JSON.stringify({
              error: {
                code: "unauthorized",
                message: "Authentication required",
                request_id: "playwright-request",
              },
            }),
          },
    ),
  );
  await page.route("**/api/v1/auth/logout", (route) => {
    adminAuthenticated = false;
    return route.fulfill({ contentType: "application/json", body: JSON.stringify(envelope(null)) });
  });
  await page.route("**/api/v1/auth/configure/session", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          protected: false,
          authenticated: true,
          csrf_token: null,
          expires_in: null,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/configure/bootstrap", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          capabilities: {
            native_usenet: true,
            proxy_debrid_stream: true,
            stremio_api_prefix: "",
            torrent_streams: true,
            usenet: true,
          },
          debrid_services: ["realdebrid", "torbox"],
          default_configuration: configuration,
          languages: { en: "🇬🇧", fr: "🇫🇷" },
          native_usenet_sources: ["instance_pool", "personal_servers"],
          resolutions: ["r2160p", "r1080p", "unknown"],
          result_formats: ["title", "video_info", "size", "languages"],
          usenet_provider_kinds: ["torbox_usenet", "stremio_nntp", "comet_native_usenet"],
          usenet_source_kinds: ["newznab"],
        }),
      ),
    }),
  );
  await page.route("**/api/v1/configure/validate", async (route) => {
    const request = route.request().postDataJSON() as {
      configuration: {
        accounts?: Record<string, { apiKey?: string; kind?: string }>;
        debridServices?: Array<{ apiKey?: string }>;
      };
    };
    const missingDebridKey =
      request.configuration.debridServices?.some((entry) => !entry.apiKey) ||
      Object.values(request.configuration.accounts ?? {}).some(
        (account) => ["realdebrid", "torbox"].includes(account.kind ?? "") && !account.apiKey,
      );
    if (missingDebridKey) {
      await route.fulfill({
        contentType: "application/json",
        status: 422,
        body: JSON.stringify({
          error: {
            code: "validation_failed",
            details: [
              {
                location: ["configuration", "debridServices", "0", "apiKey"],
                message: "debrid apiKey is required",
                type: "value_error",
              },
            ],
            message: "The request did not pass validation.",
            request_id: "playwright-request",
          },
        }),
      });
      return;
    }
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(envelope(request.configuration)),
    });
  });
  await page.route("**/configure/capabilities/test**", async (route) => {
    const configurationId = new URL(route.request().url()).searchParams.get("configuration_id");
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        bindings: configurationId
          ? [
              {
                configuration_id: configurationId,
                degraded: false,
                display_name: configurationId,
                eligible: true,
                error_code: null,
                retry_after: null,
                state: "ready",
              },
            ]
          : [],
        ok: true,
        version: 1,
      }),
    });
  });
  await page.route("**/api/v1/admin/metrics/current", (route) => {
    metricCollection += 5;
    const counter = metricCollection - 1_785_000_000;
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: metricCollection,
          history_available: false,
          history_ranges: ["15m", "1h", "6h", "24h", "7d", "30d"],
          samples: [
            { labels: { status: "200" }, name: "comet_http_requests_total", value: counter * 4 },
            {
              labels: { status: "200" },
              name: "comet_http_requests_created",
              value: 1_784_999_900,
            },
            {
              labels: { le: "0.1" },
              name: "comet_http_request_duration_seconds_bucket",
              value: counter,
            },
            {
              labels: { le: "1" },
              name: "comet_http_request_duration_seconds_bucket",
              value: counter * 2,
            },
            {
              labels: { result: "hit" },
              name: "comet_torrent_cache_lookups_total",
              value: counter * 3,
            },
            {
              labels: { result: "miss" },
              name: "comet_torrent_cache_lookups_total",
              value: counter,
            },
            {
              labels: {},
              name: "comet_proxy_stream_active_connections",
              value: 2,
            },
            {
              labels: { kind: "episode" },
              name: "comet_background_scraper_queue_items",
              value: 7,
            },
            {
              labels: { outcome: "ok", scraper: "torrentio" },
              name: "comet_scraper_requests_total",
              value: counter * 2,
            },
            {
              labels: { context: "background", scraper: "torrentio" },
              name: "comet_scraper_torrents_total",
              value: counter * 5,
            },
            {
              labels: { le: "0.5", scraper: "torrentio" },
              name: "comet_scraper_request_duration_seconds_bucket",
              value: counter,
            },
            {
              labels: { le: "2", scraper: "torrentio" },
              name: "comet_scraper_request_duration_seconds_bucket",
              value: counter * 2,
            },
          ],
        }),
      ),
    });
  });
  await page.route("**/api/v1/admin/metrics/database", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: 1_785_000_000,
          torrents: {
            total: 4210,
            media_distribution: [
              { count: 2400, label: "Movies" },
              { count: 1810, label: "Series" },
            ],
            size_distribution: [
              { count: 1200, label: "1-5GB" },
              { count: 900, label: "5-10GB" },
            ],
            summary: {
              average_size: 4_200_000_000,
              maximum_size: 80_000_000_000,
              seen_24h: 132,
              seen_7d: 940,
              unique_media: 1640,
            },
          },
          searches: {
            last_24h: 91,
            last_30d: 1160,
            last_7d: 510,
            total_unique: 2000,
          },
          scrapers: { active_locks: 3 },
          debrid_cache: {
            total: 350,
            by_service: [
              {
                average_size: 3_000_000_000,
                count: 350,
                service: "realdebrid",
                total_size: 1_050_000_000_000,
              },
            ],
          },
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/system/snapshot", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          applied_revision: 4,
          current_instance_id: "a".repeat(32),
          readiness: {
            components: {
              artifact_storage: "disabled",
              database: "ready",
              schema: "current",
              usenet_engine: "disabled",
              worker: "ready",
            },
            state: "ready",
          },
          runtimes: [
            {
              alias: null,
              applied_revision: 4,
              branch: "development",
              build_date: null,
              commit_hash: null,
              hostname: "comet-1",
              instance_id: "a".repeat(32),
              last_heartbeat: 1_785_000_000,
              processes: [],
              readiness: {
                components: { database: "ready", worker: "ready" },
                state: "ready",
              },
              restart_capable: false,
              started_at: 1_784_999_000,
            },
          ],
          stored_revision: 4,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/system/details", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          build: {
            branch: "development",
            build_date: "2026-07-31T00:00:00+00:00",
            commit_hash: "abcdef0",
            container_image: true,
            native_engine_api_version: 1,
            native_engine_enabled: true,
            python_implementation: "CPython",
            python_version: "3.13.5",
          },
          database: {
            backend: "postgresql",
            primary_connected: true,
            replicas_active: 1,
            replicas_configured: 1,
            replicas_unavailable: 0,
            schema_current: true,
            schema_version: "2026080201_usenet_release_schema",
          },
          features: {
            background_scraper: true,
            cometnet: true,
            debrid_stream_proxy: true,
            native_usenet: true,
            prometheus: true,
            read_replicas: true,
            torrent_streams: true,
            usenet: true,
          },
          maintenance: {
            last_retention_at: 1_785_000_000,
            retention_enabled: true,
          },
          storage: [
            {
              capacity_bytes: 100_000_000_000,
              configured_limit_bytes: 50_000_000_000,
              free_bytes: 70_000_000_000,
              name: "usenet_artifacts",
              used_bytes: 30_000_000_000,
            },
          ],
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/system/update-check", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          checked_at: "2026-07-31T00:00:00+00:00",
          error: null,
          has_update: false,
          install_method: "redeploy_container",
          latest_commit_hash: "abcdef0",
          latest_url: null,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/system/maintenance/retention", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(envelope({ completed_at: 1_785_000_100 })),
    }),
  );
  await page.route("**/api/v1/admin/settings", async (route) => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON() as {
        updates: Record<string, unknown>;
      };
      expect(body.updates.HTTP_CLIENT_LIMIT).toBe(75);
      expect(body.updates.PROMETHEUS_ENABLED).toBe(true);
      expect(body.updates.SCRAPE_COMET).toBe("live");
      settingLimit = 75;
      settingPrometheus = true;
      settingScrapeMode = "live";
      settingRevision = 5;
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(
          envelope({
            changed_keys: ["HTTP_CLIENT_LIMIT", "PROMETHEUS_ENABLED", "SCRAPE_COMET"],
            restart_required: true,
            revision: settingRevision,
          }),
        ),
      });
    }
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          applied_revision: 4,
          pending_restart_keys:
            settingRevision === 4
              ? []
              : ["HTTP_CLIENT_LIMIT", "PROMETHEUS_ENABLED", "SCRAPE_COMET"],
          stored_revision: settingRevision,
          settings: [
            {
              catalog: {
                category: "cache_http",
                choices: [],
                default: 50,
                deployment_owned: false,
                has_default: true,
                key: "HTTP_CLIENT_LIMIT",
                nullable: false,
                restart_required: true,
                sensitive: false,
                structured_editor: null,
                unit: null,
                value_kind: "integer",
              },
              source: "default",
              value: settingLimit,
            },
            {
              catalog: {
                category: "prometheus_logging",
                choices: [],
                default: false,
                deployment_owned: false,
                has_default: true,
                input_kind: "text",
                item_kind: null,
                key: "PROMETHEUS_ENABLED",
                nullable: false,
                restart_required: true,
                sensitive: false,
                structured_editor: null,
                unit: null,
                value_kind: "boolean",
              },
              source: "default",
              value: settingPrometheus,
            },
            {
              catalog: {
                category: "scrapers_proxies",
                choices: [false, true, "live", "background"],
                default: false,
                deployment_owned: false,
                has_default: true,
                input_kind: "text",
                item_kind: null,
                key: "SCRAPE_COMET",
                nullable: false,
                restart_required: true,
                sensitive: false,
                structured_editor: null,
                unit: null,
                value_kind: "enum",
              },
              source: "default",
              value: settingScrapeMode,
            },
            {
              catalog: {
                category: "anime_metadata",
                choices: [],
                default: null,
                deployment_owned: false,
                has_default: true,
                key: "TMDB_READ_ACCESS_TOKEN",
                nullable: true,
                restart_required: true,
                sensitive: true,
                structured_editor: null,
                unit: null,
                value_kind: "string",
              },
              source: "environment",
              value: "tmdb-test-token",
            },
            {
              catalog: {
                category: "database",
                choices: [],
                default: null,
                deployment_owned: true,
                has_default: true,
                input_kind: "text",
                item_kind: "string",
                key: "DATABASE_READ_REPLICA_URLS",
                nullable: false,
                restart_required: true,
                sensitive: true,
                structured_editor: "read_replicas",
                unit: null,
                value_kind: "list",
              },
              source: "environment",
              value: ["postgresql://replica.example/comet"],
            },
          ],
        }),
      ),
    });
  });
  await page.route("**/api/v1/admin/settings/audit?*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(envelope({ items: auditItems, next_cursor: null })),
    }),
  );
  await page.route("**/api/v1/admin/proxy/snapshot", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: 1_785_000_000,
          enabled: true,
          summary: {
            active_connections: 1,
            current_speed: 8_000_000,
            session_bytes: 120_000_000,
            all_time_bytes: 9_000_000_000,
            completed_7d: 84,
            failed_7d: 2,
            bytes_7d: 18_000_000_000,
            average_duration_7d: 900,
          },
          active: [
            {
              id: "8af5d66a-1dd2-4c35-8f5e-8b5138f33c40",
              ip: "192.0.2.8",
              content: "Example release",
              service: "realdebrid",
              instance_id: "a".repeat(32),
              process_id: 42,
              started_at: 1_784_999_900,
              updated_at: 1_785_000_000,
              duration: 100,
              bytes_transferred: 120_000_000,
              current_speed: 8_000_000,
              average_speed: 1_200_000,
              peak_speed: 10_000_000,
              cancellation_pending: false,
            },
          ],
          history: [
            {
              started_at: 1_784_996_400,
              connections: 12,
              bytes_transferred: 2_000_000_000,
              failed: 1,
              peak_concurrent: 4,
            },
            {
              started_at: 1_785_000_000,
              connections: 8,
              bytes_transferred: 1_000_000_000,
              failed: 0,
              peak_concurrent: 3,
            },
          ],
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/proxy/history?*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          items: [
            {
              id: "history",
              ip: "192.0.2.9",
              content: "Previous release",
              service: "alldebrid",
              instance_id: "b".repeat(32),
              process_id: 43,
              started_at: 1_784_999_000,
              finished_at: 1_784_999_900,
              duration: 900,
              bytes_transferred: 2_000_000_000,
              average_speed: 2_222_222,
              peak_speed: 5_000_000,
              outcome: "completed",
              error_code: null,
            },
          ],
          next_cursor: null,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/proxy/activity?*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: 1_785_000_000,
          selection: new URL(route.request().url()).searchParams.get("range") ?? "auto",
          activity_started_at: 1_784_996_400,
          window_started_at: 1_784_996_400,
          window_ended_at: 1_785_000_000,
          bucket_seconds: 60,
          buckets: activityBuckets(1_784_996_400, 1_785_000_000, 60, {
            0: {
              bytes_transferred: 2_000_000_000,
              completed: 12,
              failed: 1,
              peak_active: 4,
            },
            59: {
              bytes_transferred: 1_120_000_000,
              completed: 8,
              interrupted: 1,
              active: 1,
              peak_active: 3,
            },
          }),
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/proxy/connections/*/cancel", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          resource_id: "8af5d66a-1dd2-4c35-8f5e-8b5138f33c40",
          outcome: "cancelled",
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/scraping/snapshot", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: 1_785_000_000,
          runtimes: [
            {
              instance_id: "a".repeat(32),
              process_id: 42,
              state: "running",
              draining: false,
              run_id: "11111111-1111-4111-8111-111111111111",
              started_at: 1_784_999_900,
              processed: 12,
              success: 10,
              failed: 2,
              torrents_found: 41,
              discovered_items: 20,
              errors: 1,
              last_heartbeat: 1_785_000_000,
            },
          ],
          queue: {
            items: 34,
            episodes: 55,
            ready: 28,
            running: 2,
            deferred: 8,
            failed: 4,
            dead: 3,
            oldest_ready_at: 1_784_996_400,
            low_watermark: 20,
            high_watermark: 100,
            hard_cap: 200,
          },
          runs_24h: 12,
          processed_24h: 240,
          failed_24h: 8,
          torrents_found_24h: 620,
          latest_run: null,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/scraping/queue/*?*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          items: [
            {
              kind: "item",
              id: "tt123",
              parent_id: null,
              media_type: "series",
              title: "Example series",
              year: 2024,
              season: null,
              episode: null,
              priority_score: 12,
              status: "failed",
              consecutive_failures: 2,
              last_scraped_at: 1_784_999_000,
              last_success_at: null,
              last_failure_at: 1_784_999_000,
              next_retry_at: 1_785_000_300,
              total_torrents_found: 5,
              created_at: 1_784_000_000,
              updated_at: 1_784_999_000,
            },
          ],
          next_cursor: null,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/scraping/runs?*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          items: [
            {
              run_id: "11111111-1111-4111-8111-111111111111",
              started_at: 1_784_999_000,
              finished_at: 1_784_999_100,
              status: "completed",
              processed: 30,
              success: 28,
              failed: 2,
              torrents_found: 90,
              duration_ms: 100_000,
              worker_count: 4,
              error_code: null,
            },
          ],
          next_cursor: null,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/scraping/queue/*/*/*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          kind: "item",
          resource_id: "tt123",
          action: "retry",
          affected: 1,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/scraping/control/*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(envelope({ action: "pause", owners: 1, outcome: "succeeded" })),
    }),
  );
  await page.route("**/api/v1/admin/usenet/snapshot", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: 1_785_000_000,
          enabled: true,
          runtimes: [
            {
              instance_id: "a".repeat(32),
              process_id: 42,
              healthy: true,
              mode: "native",
              collected_at: 1_785_000_000,
              stats: {
                draining: false,
                sessions: 2,
                nntp_connections_open: 4,
                nntp_connections_active: 2,
                nntp_pools: 2,
                nntp_queue_interactive: 1,
                nntp_queue_preparation: 0,
                nntp_queue_background: 1,
                archive_jobs_active: 1,
                repair_jobs_active: 0,
                nntp_provider_failovers_total: 3,
                nntp_circuits_auth_open: 0,
                nntp_circuits_transient_open: 1,
                nntp_circuits_half_open: 0,
                segment_cache_bytes: 67_108_864,
                disk_cache_bytes: 536_870_912,
              },
            },
          ],
          active: [
            {
              id: "b".repeat(32),
              instance_id: "a".repeat(32),
              process_id: 42,
              client_ip: "192.0.2.10",
              content_id: "tt123",
              title: "Example Usenet release",
              member_path: "Example.Release.mkv",
              source_kind: "session",
              started_at: 1_784_999_970,
              updated_at: 1_785_000_000,
              duration: 30,
              total_bytes: 8_000_000_000,
              bytes_transferred: 2_000_000_000,
              cancellation_pending: false,
            },
          ],
          preparations: [
            {
              id: "11111111-1111-4111-8111-111111111111",
              provider_kind: "comet_native_usenet",
              media_id: "tt456",
              title: "Archive preparation",
              state: "submitted",
              created_at: 1_784_999_980,
              updated_at: 1_784_999_990,
            },
          ],
          inventory: {
            artifacts: 12,
            nzb_bytes: 4_000_000,
            materialized_bytes: 12_000_000_000,
            active_readers: 2,
            eligible_for_prune: 1,
          },
          history: {
            streams_7d: 18,
            failed_7d: 1,
            bytes_7d: 48_000_000_000,
          },
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/usenet/history?*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          items: [
            {
              id: "c".repeat(32),
              instance_id: "a".repeat(32),
              process_id: 42,
              client_ip: "192.0.2.11",
              content_id: "tt789",
              title: "Completed Usenet release",
              member_path: "Completed.mkv",
              source_kind: "raw_composite",
              started_at: 1_784_999_900,
              finished_at: 1_784_999_960,
              duration: 60,
              total_bytes: 4_000_000_000,
              bytes_transferred: 4_000_000_000,
              outcome: "completed",
              error_code: null,
            },
          ],
          next_cursor: null,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/usenet/activity?*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: 1_785_000_000,
          selection: new URL(route.request().url()).searchParams.get("range") ?? "auto",
          activity_started_at: 1_784_999_900,
          window_started_at: 1_784_999_880,
          window_ended_at: 1_785_000_000,
          bucket_seconds: 15,
          buckets: activityBuckets(1_784_999_880, 1_785_000_000, 15, {
            4: {
              bytes_transferred: 4_000_000_000,
              completed: 1,
            },
            7: {
              bytes_transferred: 2_000_000_000,
              active: 1,
            },
          }),
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/usenet/artifacts?*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          items: [
            {
              artifact_sha256: "d".repeat(64),
              storage_kind: "materialized_asset",
              publication_state: "published",
              byte_size: 4_000_000_000,
              logical_length: 4_000_000_000,
              refcount: 0,
              active_readers: 0,
              created_at: 1_784_000_000,
              last_used_at: 1_784_900_000,
              eligible_for_prune: true,
            },
          ],
          next_cursor: null,
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/usenet/operations/*/cancel", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(envelope({ resource_id: "b".repeat(32), outcome: "cancelled" })),
    }),
  );
  await page.route("**/api/v1/admin/usenet/runtimes/*/*/*", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({ action: "drain", instance_id: "a".repeat(32), outcome: "succeeded" }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/usenet/artifacts/*/prune", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(envelope({ artifact_sha256: "d".repeat(64), pruned: true })),
    }),
  );
  await page.route("**/api/v1/admin/cometnet/snapshot", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: 1_785_000_000,
          node: {
            enabled: true,
            healthy: true,
            node_id: "comet-node-1",
            mode: "local",
            uptime_seconds: 3_600,
            contribution_mode: "full",
            connected_peers: 1,
            inbound_peers: 0,
            outbound_peers: 1,
            average_latency_ms: 18,
            bytes_sent: 12_000_000,
            bytes_received: 24_000_000,
            messages_sent: 120,
            messages_received: 240,
            torrents_sent: 30,
            torrents_received: 54,
            invalid_messages: 1,
          },
          peers: [
            {
              node_id: "peer-node-1",
              alias: "Paris peer",
              connected_at: 1_784_999_000,
              last_activity: 1_785_000_000,
              outbound: true,
              latency_ms: 18,
              reputation: 0.96,
              trust_level: "trusted",
              torrents_received: 54,
              invalid_contributions: 1,
              bytes_sent: 12_000_000,
              bytes_received: 24_000_000,
            },
          ],
          pools: [
            {
              pool_id: "trusted",
              display_name: "Trusted indexers",
              description: "Primary metadata pool",
              member_count: 2,
              version: 4,
              updated_at: 1_785_000_000,
              membership: true,
              subscribed: true,
            },
          ],
          events: [
            {
              ...operationalEvent,
              category: "COMETNET",
              event: "cometnet.peer.connected",
              message: "CometNet peer connected",
            },
          ],
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/cometnet/pools/trusted", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          pool_id: "trusted",
          display_name: "Trusted indexers",
          description: "Primary metadata pool",
          creator_key: "1".repeat(64),
          join_mode: "invite",
          version: 4,
          created_at: 1_784_000_000,
          updated_at: 1_785_000_000,
          is_admin: true,
          is_member: true,
          subscribed: true,
          members: [
            {
              public_key: "1".repeat(64),
              node_id: "creator-node",
              role: "creator",
              added_at: 1_784_000_000,
              contribution_count: 80,
              last_seen: 1_785_000_000,
              is_self: true,
            },
            {
              public_key: "2".repeat(64),
              node_id: "member-node",
              role: "member",
              added_at: 1_784_500_000,
              contribution_count: 32,
              last_seen: 1_785_000_000,
              is_self: false,
            },
          ],
          invites: [],
        }),
      ),
    }),
  );
  await page.route("**/api/v1/admin/logs**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/stream")) {
      expect(url.searchParams.get("cursor")).toBe("41");
      return route.fulfill({
        contentType: "text/event-stream",
        body: `id: 42\nevent: operational_event\ndata: ${JSON.stringify({
          ...operationalEvent,
          event: "playback.started",
          id: 42,
          message: "Playback started",
        })}\n\n`,
      });
    }
    return route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({ dropped_events: 0, items: [operationalEvent], next_cursor: null }),
      ),
    });
  });
});

test("public and admin route families render independently", async ({ page }) => {
  await page.goto("/configure");
  await expect(page.getByRole("button", { name: "Copy link" })).toBeVisible();

  await page.goto("/admin/overview");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
});

test("configurator copies the public manifest when defaults are untouched", async ({ page }) => {
  await page.goto("/configure");
  const origin = new URL(page.url()).origin;
  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value: string) => sessionStorage.setItem("manifest", value) },
    });
  });

  await page.getByRole("button", { name: "Copy link" }).click();

  await expect
    .poll(() => page.evaluate(() => sessionStorage.getItem("manifest")))
    .toBe(`${origin}/manifest.json`);
});

test("configurator copies a validated v1 manifest link", async ({ page }) => {
  await page.goto("/configure");
  await page.getByRole("button", { name: "Add service" }).click();
  await page.getByLabel("API key").fill("playwright-secret");

  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value: string) => sessionStorage.setItem("manifest", value) },
    });
  });
  await page.getByRole("button", { name: "Copy link" }).click();
  await expect(page.getByText("Link copied")).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => sessionStorage.getItem("manifest")))
    .toContain("/manifest.json");
});

test("switching a torrent row to debrid keeps a valid account after toggling Usenet", async ({
  page,
}) => {
  await page.goto("/configure");
  const rows = page.locator(".debrid-row");
  const serviceSelect = rows.first().getByRole("combobox", { name: "Service" });
  await serviceSelect.click();
  await page.getByRole("option", { name: "Real-Debrid" }).click();
  await expect(serviceSelect).not.toBeFocused();
  await expect(serviceSelect).toHaveCSS("outline-style", "none");
  await rows.first().getByLabel("API key").fill("playwright-secret");
  await page.getByRole("button", { name: "Add service" }).click();
  await rows.nth(1).getByRole("combobox", { name: "Service" }).click();
  await page.getByRole("option", { name: "Torrent (P2P)" }).click();

  const usenetToggle = page.getByRole("switch", { name: "Enable Usenet" });
  await usenetToggle.click();
  await usenetToggle.click();
  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async () => undefined },
    });
  });
  const validation = page.waitForRequest((request) =>
    request.url().includes("/api/v1/configure/validate"),
  );
  await page.getByRole("button", { name: "Copy link" }).click();
  const body = (await validation).postDataJSON() as {
    configuration: {
      playbackProviders: Array<{ accountId?: string; kind: string }>;
    };
  };
  const provider = body.configuration.playbackProviders.find(
    (entry) => entry.kind === "realdebrid",
  );

  expect(provider?.accountId).toMatch(/^[0-9a-f-]{36}$/);
  await expect(page.getByText("Link copied")).toBeVisible();
});

test("configurator explains a missing debrid API key", async ({ page }) => {
  await page.goto("/configure");
  await page.getByRole("button", { name: "Add service" }).click();
  await page.getByRole("button", { name: "Copy link" }).click();

  await expect(page.getByRole("alert")).toContainText(
    "Invalid configuration: debrid apiKey is required",
  );
});

test("configurator uses compact controls without hiding inactive Usenet fields", async ({
  page,
}) => {
  await page.route("**/configure", async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace(
      "<!--COMET_CUSTOM_HEADER-->",
      '<div data-testid="custom-header" style="display:flex;flex-direction:column;align-items:center;margin-top:20px"><h2 style="margin:0;font-size:1.8em"><span style="color:rgb(0,184,148)">Supercharge</span> Your Comet Experience</h2><a href="#" style="background:rgb(0,184,148);color:white;padding:14px 32px;border-radius:10px;text-decoration:none">Purchase TorBox</a></div>',
    );
    await route.fulfill({ body, response });
  });
  await page.goto("/configure");

  await expect(page.getByRole("heading", { name: "Configure Comet" })).toHaveCount(0);
  await expect(page.locator(".configure-intro").getByText("Comet", { exact: true })).toBeVisible();
  const customHeader = page.getByTestId("custom-header");
  await expect(customHeader).toBeVisible();
  await expect(page.locator(".configure-tagline")).toHaveCSS("font-size", "13.12px");
  await expect(customHeader.locator("span")).toHaveCSS("color", "rgb(0, 184, 148)");
  await expect(customHeader.getByRole("link")).toHaveCSS("padding", "14px 32px");
  await expect(page.getByRole("link", { name: "Administration" })).toHaveAttribute(
    "href",
    "/admin/overview",
  );
  await expect(page.locator(".configuration-section__index")).toHaveCount(0);
  await expect(page.locator(".configuration-stage__header .eyebrow")).toHaveCount(0);
  await expect(page.locator(".configuration-actions")).toHaveCSS("border-top-width", "0px");
  await expect(page.locator(".configure-intro .brand__mark")).toHaveCSS("transform", "none");
  await page.locator(".language-select .select-trigger__leading-icon").click();
  await expect(page.getByRole("option", { name: "French" })).toBeVisible();
  await page.keyboard.press("Escape");
  const brandBox = await page.locator(".configure-intro > .brand").boundingBox();
  const customHeaderBox = await customHeader.boundingBox();
  expect(brandBox).not.toBeNull();
  expect(customHeaderBox).not.toBeNull();
  expect(customHeaderBox?.y).toBeGreaterThan((brandBox?.y ?? 0) + (brandBox?.height ?? 0));
  await expect(page.locator('a[href="https://github.com/sponsors/g0ldyy"] svg')).toBeVisible();
  await expect(page.locator('a[href="https://discord.com/invite/UJEqpT42nb"] svg')).toBeVisible();
  await expect(page.getByRole("switch", { name: "Torrent" })).toBeChecked();
  const p2pRow = page.locator(".debrid-row").filter({ hasText: "Torrent (P2P)" });
  await expect(p2pRow).toHaveCount(1);
  await expect(p2pRow.getByLabel("API key")).toHaveCount(0);
  await page.getByRole("button", { name: "Add service" }).click();
  await page.getByLabel("API key").fill("playwright-secret");
  await expect(page.getByLabel("API key")).toHaveCSS("outline-style", "none");
  await expect(page.getByRole("switch", { name: "Torrent" }).locator("span")).toHaveCSS(
    "background-color",
    "rgb(244, 241, 236)",
  );
  const usenetToggle = page.getByRole("switch", { name: "Enable Usenet" });
  await usenetToggle.click();
  await expect(usenetToggle).toBeChecked();
  await page.getByText("Usenet", { exact: true }).click();
  await expect(page.getByLabel("Access token")).toHaveCount(0);
  await page.getByRole("button", { name: "Add provider" }).click();
  await expect(page.getByLabel("Access token")).toBeVisible();
  await page.getByRole("button", { name: "Add provider" }).click();
  await page.getByRole("combobox", { name: "Playback provider" }).last().click();
  await page.getByRole("option", { name: "Stremio NNTP" }).click();
  await expect(page.getByText("Send NNTP credentials to Stremio")).toHaveCount(0);
  await expect(page.getByText("Allow plaintext NNTP")).toHaveCount(0);
  await page.getByRole("combobox", { name: "Server preset" }).click();
  await expect(page.getByRole("option", { name: "TorBox News Server" })).toHaveCount(0);
  await page.getByRole("option", { name: "Newshosting" }).click();
  await expect(page.getByLabel("Host")).toHaveValue("news.newshosting.com");
  await expect(page.locator(".server-editor__row-header").getByRole("button")).toBeVisible();
  await page.getByRole("button", { name: "Add source" }).click();
  await page.getByRole("combobox", { name: "Indexer preset" }).click();
  await page.getByRole("option", { name: "NzbNest" }).click();
  await expect(page.getByLabel("Endpoint")).toHaveValue("https://nzbnest.com/api");
  await expect(page.getByRole("button", { name: /Stealth/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page.getByLabel("Query User-Agent")).toHaveCount(0);
  await page.getByRole("button", { name: /This browser/ }).click();
  await expect(page.locator(".indexer-identity__captured")).toHaveText(
    await page.evaluate(() => navigator.userAgent),
  );

  await page.getByText("Results", { exact: true }).click();
  const resultFields = page.locator(".multi-select-field").filter({ hasText: "Result fields" });
  await expect(resultFields.locator(".multi-select__badge")).toHaveCount(4);
  const resolutions = page.getByRole("button", { name: "Resolutions", exact: true });
  await resolutions.focus();
  await resolutions.press("Enter");
  await expect(page.getByLabel("Search resolutions")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "2160p", exact: true })).toHaveAttribute(
    "aria-pressed",
    "true",
  );

  await resolutions.press("Escape");
  await page
    .locator(".configuration-section__title")
    .getByText("Languages", { exact: true })
    .click();
  await expect(page.locator(".language-groups .multi-select-field")).toHaveCount(4);

  await page.getByText("Usenet", { exact: true }).click();
  await page
    .locator(".binding-card")
    .filter({ hasText: "Stremio NNTP" })
    .getByRole("button", { name: "Test connection" })
    .click();
  await expect(page.getByText("Connection available").first()).toBeVisible();

  let submitted: {
    discoverySources?: unknown[];
    enabledTransports?: string[];
    playbackProviders?: Array<{ kind: string }>;
  } = {};
  await page.unroute("**/api/v1/configure/validate");
  await page.route("**/api/v1/configure/validate", async (route) => {
    const request = route.request().postDataJSON() as { configuration: typeof submitted };
    submitted = request.configuration;
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(envelope(request.configuration)),
    });
  });
  await usenetToggle.click();
  await page.evaluate(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async () => undefined },
    });
  });
  await page.getByRole("button", { name: "Copy link" }).click();
  await expect(page.getByText("Link copied")).toBeVisible();
  expect(submitted.enabledTransports).toEqual(["bittorrent"]);
  expect(submitted.discoverySources).toEqual([]);
  expect(submitted.playbackProviders?.some(({ kind }) => kind === "stremio_nntp")).toBe(false);

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("configurator keeps every tablet tab readable", async ({ page, isMobile }) => {
  test.skip(isMobile, "The mobile project covers the compact scrolling navigation");
  await page.setViewportSize({ height: 900, width: 1006 });
  await page.goto("/configure");

  const navigation = page.locator(".configuration-sections");
  await expect(navigation).toBeVisible();
  await expect(navigation.locator(".configuration-section__title")).toHaveText([
    "Torrent",
    "Usenet",
    "Results",
    "Languages",
  ]);
  expect(
    await navigation
      .locator(".configuration-section__title")
      .evaluateAll((titles) => titles.every((title) => title.scrollWidth <= title.clientWidth + 1)),
  ).toBeTruthy();
  expect(
    await navigation.evaluate((element) => element.scrollWidth - element.clientWidth),
  ).toBeLessThanOrEqual(1);
});

test("torrent services can be reordered from the drag handle", async ({ page, isMobile }) => {
  test.skip(isMobile, "Pointer ordering is covered at desktop precision");
  await page.goto("/configure");
  await page.getByRole("button", { name: "Add service" }).click();
  await page.getByRole("button", { name: "Add service" }).click();

  const rows = page.locator(".debrid-row");
  const torBoxHandle = rows.nth(2).getByRole("button", { name: "Reorder service" });
  const firstRow = await rows.nth(0).boundingBox();
  if (firstRow === null) throw new Error("First service row geometry is unavailable");
  await torBoxHandle.hover();
  await page.mouse.down();
  await page.mouse.move(firstRow.x + firstRow.width / 2, firstRow.y + firstRow.height / 2, {
    steps: 4,
  });
  await page.mouse.up();

  await expect(rows.nth(0).getByRole("combobox", { name: "Service" })).toHaveText("TorBox");
});

test("logout immediately returns to the login screen", async ({ page }) => {
  await page.goto("/admin/overview");
  await page.getByRole("button", { name: "Log out" }).click();

  await expect(page.getByRole("heading", { name: "Sign in to Comet" })).toBeVisible();
  await expect(page.getByText("Overview data is incomplete")).toHaveCount(0);
});

test("the login card stays centered after switching to RTL", async ({ page }) => {
  await page.unroute("**/api/v1/auth/session");
  await page.route("**/api/v1/auth/session", (route) =>
    route.fulfill({
      contentType: "application/json",
      status: 401,
      body: JSON.stringify({
        error: {
          code: "unauthorized",
          message: "Authentication required",
          request_id: "playwright-request",
        },
      }),
    }),
  );
  await page.goto("/admin/overview");

  const language = page.getByRole("combobox", { name: "Language" });
  await language.click();
  await page.getByRole("option", { name: "Arabic" }).click();

  const card = await page.locator(".auth-card").boundingBox();
  const viewport = page.viewportSize();
  expect(card).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(
    Math.abs((card?.x ?? 0) + (card?.width ?? 0) / 2 - (viewport?.width ?? 0) / 2),
  ).toBeLessThan(1);
});

test("admin navigation works at the active viewport", async ({ page, isMobile }) => {
  if (isMobile) await page.setViewportSize({ height: 720, width: 320 });
  await page.goto("/admin/overview");
  await expect(
    page.locator(".topbar .language-select .select-trigger__leading-icon"),
  ).toBeVisible();
  const languagePicker = await page.locator(".topbar .language-select").boundingBox();
  const languageChevron = await page
    .locator(".topbar .language-select .select-trigger__icon")
    .boundingBox();
  expect(languagePicker).not.toBeNull();
  expect(languageChevron).not.toBeNull();
  expect((languageChevron?.x ?? 0) + (languageChevron?.width ?? 0)).toBeLessThanOrEqual(
    (languagePicker?.x ?? 0) + (languagePicker?.width ?? 0),
  );

  if (isMobile) {
    await page.getByRole("button", { name: "Open navigation" }).click();
  }

  await expect(page.getByRole("link", { name: "GitHub Sponsors" })).toHaveAttribute(
    "href",
    "https://github.com/sponsors/g0ldyy",
  );
  await expect(page.getByRole("link", { name: "Ko-fi" })).toHaveAttribute(
    "href",
    "https://ko-fi.com/g0ldyy",
  );
  await expect(page.getByRole("link", { name: "Discord" })).toHaveAttribute(
    "href",
    "https://discord.com/invite/UJEqpT42nb",
  );
  if (isMobile) {
    const communityLinks = page.locator(".drawer__content .community-links--sidebar a");
    await expect(communityLinks).toHaveCount(3);
    for (const link of await communityLinks.all()) {
      await expect(link).toHaveCSS("display", "flex");
      const icon = await link.locator("svg").boundingBox();
      const label = await link.locator("span").boundingBox();
      expect(icon).not.toBeNull();
      expect(label).not.toBeNull();
      expect(
        Math.abs(
          (icon?.y ?? 0) + (icon?.height ?? 0) / 2 - ((label?.y ?? 0) + (label?.height ?? 0) / 2),
        ),
      ).toBeLessThan(1);
    }
  }
  await page.getByRole("link", { name: "Analytics" }).click();
  await expect(page).toHaveURL(/\/admin\/analytics$/);
  await expect(page.getByRole("heading", { name: "Analytics" })).toBeVisible();
});

test("admin quick navigation opens from the keyboard", async ({ page }) => {
  await page.goto("/admin/overview");
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
  await page.keyboard.press("Control+k");

  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  const requestCard = page.locator(".metric-card").filter({ hasText: "HTTP requests" }).first();
  const requestCardBackground = await requestCard.evaluate(
    (element) => getComputedStyle(element).backgroundColor,
  );
  await dialog.getByRole("textbox", { name: "Search" }).fill("HTTP requests");
  await dialog.getByRole("option", { name: "HTTP requests Overview" }).click();
  await expect(dialog).toBeHidden();
  await expect(requestCard).toHaveCSS("background-color", requestCardBackground);

  await page.keyboard.press("Control+k");
  const searchInput = dialog.getByRole("textbox", { name: "Search" });
  await searchInput.fill("System");
  await searchInput.press("Enter");
  await expect(page).toHaveURL(/\/admin\/system$/);
  await expect(page.getByRole("button", { name: "Search", exact: true })).not.toBeFocused();

  await page.keyboard.press("Control+k");
  await expect(searchInput).toHaveValue("System");
  await expect
    .poll(() =>
      searchInput.evaluate((element) => {
        const input = element as HTMLInputElement;
        return [input.selectionStart, input.selectionEnd];
      }),
    )
    .toEqual([0, "System".length]);
  await searchInput.pressSequentially("DATABASE_READ_REPLICA_URLS");
  await expect(searchInput).toHaveValue("DATABASE_READ_REPLICA_URLS");
  await dialog.getByRole("option", { name: /DATABASE_READ_REPLICA_URLS Settings/ }).click();
  await expect(page).toHaveURL(/\/admin\/settings$/);
  const settingTarget = page.locator('[data-search-target="DATABASE_READ_REPLICA_URLS"]');
  await expect(settingTarget).toBeFocused();
  await expect(settingTarget).toBeInViewport();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
});

test("every admin workspace stays inside the active viewport", async ({ page }) => {
  const workspaces = [
    ["/admin/overview", "Overview"],
    ["/admin/logs", "Logs"],
    ["/admin/analytics", "Analytics"],
    ["/admin/usenet", "Usenet"],
    ["/admin/proxy", "Debrid Stream Proxy"],
    ["/admin/scraping", "Background scraper"],
    ["/admin/cometnet", "CometNet"],
    ["/admin/settings", "Settings"],
    ["/admin/system", "System"],
  ] as const;

  for (const [path, heading] of workspaces) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(1);
  }
});

test("collapsed admin navigation keeps its toggle clear", async ({ page, isMobile }) => {
  test.skip(isMobile, "The mobile drawer does not use the collapsed sidebar");
  await page.goto("/admin/overview");

  const toggle = page.getByRole("button", { name: "Collapse navigation" });
  await toggle.click();
  await expect(page.locator(".sidebar__brand .brand")).toBeVisible();

  const sidebar = await page.locator(".sidebar").boundingBox();
  const toggleBox = await page.getByRole("button", { name: "Open navigation" }).boundingBox();
  expect(sidebar).not.toBeNull();
  expect(toggleBox).not.toBeNull();
  expect(toggleBox?.x).toBeGreaterThanOrEqual(sidebar?.x ?? 0);
  expect((toggleBox?.x ?? 0) + (toggleBox?.width ?? 0)).toBeLessThanOrEqual(
    (sidebar?.x ?? 0) + (sidebar?.width ?? 0),
  );
});

test("overview and analytics expose useful live and inventory signals", async ({ page }) => {
  await page.goto("/admin/overview");
  await expect(page.getByText("Ready", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Active proxy streams")).toBeVisible();
  const compactMetric = page.locator(".compact-metrics .metric-card").first();
  const divider = await compactMetric.evaluate(
    (element) => getComputedStyle(element).borderRightColor,
  );
  await compactMetric.hover();
  await expect(compactMetric).toHaveCSS("border-right-color", divider);
  await expect(compactMetric).not.toHaveCSS("box-shadow", "none");

  await page.goto("/admin/analytics");
  await expect(page.getByText("4,210").first()).toBeVisible();
  await expect(page.getByText("Torrent candidates")).toBeVisible();
  await expect(page.getByText("realdebrid")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Torrent cache" })).toBeVisible();
  const liveChart = page.locator(".metric-chart .recharts-wrapper");
  await expect(liveChart).toBeVisible({ timeout: 7_000 });
  await liveChart.hover({ position: { x: 160, y: 140 } });
  await expect(page.locator(".metric-chart .recharts-active-dot")).toBeVisible();
  const tooltip = page.locator(".metric-chart__tooltip");
  await expect(tooltip).toBeVisible();
  await expect(page.locator(".metric-chart .recharts-tooltip-wrapper")).toHaveCSS(
    "transition-duration",
    "0s",
  );
  await expect(page.locator(".metric-chart .recharts-tooltip-cursor")).toHaveCount(0);
  const axisTicks = page.locator(".metric-chart__axis-tick");
  await expect(axisTicks.first()).toHaveAttribute("text-anchor", "start");
  await expect(axisTicks.last()).toHaveAttribute("text-anchor", "end");
  await liveChart.click({ position: { x: 320, y: 120 } });
  await expect(page.locator(".metric-chart .recharts-wrapper > .recharts-surface")).toHaveCSS(
    "outline-style",
    "none",
  );
  await expect(page.locator(".metric-chart .recharts-tooltip-cursor")).toHaveCount(0);
  expect(await liveChart.evaluate((chart) => chart.contains(document.activeElement))).toBeFalsy();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("metric surfaces render from the first snapshot without unrelated waits", async ({ page }) => {
  await page.route(/\/api\/v1\/admin\/(system\/snapshot|metrics\/database)$/, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_200));
    await route.fallback();
  });
  const currentMetrics = page.waitForResponse("**/api/v1/admin/metrics/current");

  await page.goto("/admin/overview");
  await currentMetrics;
  const requestValue = page
    .locator(".metric-card")
    .filter({ hasText: "HTTP requests" })
    .first()
    .locator(".metric-card__value");
  await expect(requestValue).not.toHaveText("—", { timeout: 700 });

  await page.goto("/admin/analytics");
  await expect(page.getByRole("heading", { name: "Analytics", level: 1 })).toBeVisible();
  await expect(page.locator(".metric-chart .recharts-wrapper")).toBeVisible({ timeout: 700 });
});

test("system workspace exposes safe deployment diagnostics and bounded maintenance", async ({
  page,
}) => {
  await page.goto("/admin/system");
  await expect(page.getByRole("heading", { name: "System" })).toBeVisible();
  await expect(page.getByText("CPython 3.13.5")).toBeVisible();
  await expect(page.getByText("2026080201_usenet_release_schema")).toBeVisible();
  await expect(page.getByText("Usenet artifacts")).toBeVisible();
  await expect(page.getByText("Remote restart is not enabled for this runtime.")).toBeVisible();
  await page.getByRole("button", { name: "Check for updates" }).click();
  await expect(page.getByText("This branch is up to date.")).toBeVisible();
  await page.getByRole("button", { name: "Prune eligible data" }).click();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("settings save typed changes and receive exact secret values", async ({ page }) => {
  await page.goto("/admin/settings");
  await expect(page.getByText("Recent setting changes")).toHaveCount(0);
  await expect(page.getByText("Default").first()).toBeVisible();
  const exported = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export" }).click();
  await expect((await exported).suggestedFilename()).toMatch(
    /^comet-settings-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z-r4\.json$/,
  );
  await page.getByLabel("HTTP_CLIENT_LIMIT").fill("75");
  await page.getByRole("switch", { name: "PROMETHEUS_ENABLED" }).click();
  await page.getByRole("combobox", { name: "SCRAPE_COMET" }).click();
  await page.getByRole("option", { name: "Live requests" }).click();
  await page.getByRole("button", { name: "Save changes (3)" }).click();
  await expect(page.getByText("Revision 5 was stored and requires a restart.")).toBeVisible();
  const token = page.getByLabel("TMDB_READ_ACCESS_TOKEN");
  await expect(token).toHaveValue("tmdb-test-token");
  await expect(token).toHaveAttribute("type", "password");
  await token.locator("xpath=..").getByRole("button", { name: "Show value" }).click();
  await expect(token).toHaveAttribute("type", "text");
  await token.locator("xpath=..").getByRole("button", { name: "Hide value" }).click();
  await expect(token).toHaveAttribute("type", "password");
});

test("settings audit is an explicit disclosure when changes exist", async ({ page }) => {
  await page.goto("/admin/settings");

  const disclosure = page.getByText("Recent setting changes");
  await expect(disclosure).toBeVisible();
  await expect(page.locator(".settings-audit__chevron")).toBeVisible();
  await disclosure.click();
  await expect(page.locator(".audit-list").getByText("HTTP_CLIENT_LIMIT")).toBeVisible();
});

test("logs combine history and live events", async ({ page }) => {
  await page.goto("/admin/logs");

  await expect(page.getByRole("heading", { name: "Logs" })).toBeVisible();
  await expect(page.getByText("playback.started")).toBeVisible();
  await expect(page.getByText("search.completed")).toBeVisible();
  const searched = page.waitForRequest((request) =>
    request.url().includes("/api/v1/admin/logs?search=playback"),
  );
  await page.getByLabel("Search visible events").fill("playback");
  await searched;
  await page.getByRole("button", { name: "Export" }).click();
  await expect(page.getByRole("link", { name: "Export JSONL" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Export text" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Load older events" })).toHaveCount(0);
  await page.getByText("playback.started").click();
  await expect(page.getByText("Details", { exact: true })).toBeVisible();
  await expect(page.getByText("Related events")).toBeVisible();
});

test("logs paginate only from the log section scroll boundary", async ({ page }) => {
  await page.unroute("**/api/v1/admin/logs**");
  const requestedCursors: Array<string | null> = [];
  const pageOfEvents = (highestId: number) =>
    Array.from({ length: 20 }, (_, index) => ({
      ...operationalEvent,
      event: `log.${highestId - index}`,
      id: highestId - index,
      message: `Historical event ${highestId - index}`,
    }));

  await page.route("**/api/v1/admin/logs**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/stream")) {
      await route.fulfill({ body: "", contentType: "text/event-stream" });
      return;
    }
    const cursor = url.searchParams.get("cursor");
    requestedCursors.push(cursor);
    const highestId = cursor === null ? 100 : Number(cursor);
    await new Promise((resolve) => setTimeout(resolve, cursor === null ? 0 : 100));
    await route.fulfill({
      body: JSON.stringify(
        envelope({
          dropped_events: 0,
          items: pageOfEvents(highestId),
          next_cursor: highestId > 60 ? highestId - 20 : null,
        }),
      ),
      contentType: "application/json",
    });
  });

  await page.goto("/admin/logs");
  const list = page.locator(".event-list--logs");
  await expect(list.locator(".event-row")).toHaveCount(20);
  await page.getByRole("button", { name: "Pause" }).click();
  await expect(list.locator(".event-load-sentinel")).toHaveCount(1);
  await expect(list).toHaveCSS("overflow-y", "auto");

  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))),
  );
  expect(requestedCursors).toEqual([null]);

  const secondPage = page.waitForRequest(
    (request) => new URL(request.url()).searchParams.get("cursor") === "80",
  );
  await list.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
    for (let index = 0; index < 5; index += 1) {
      element.dispatchEvent(new Event("scroll", { bubbles: true }));
    }
  });
  await secondPage;
  await expect(list.locator(".event-row")).toHaveCount(40);
  expect(requestedCursors).toEqual([null, "80"]);

  const thirdPage = page.waitForRequest(
    (request) => new URL(request.url()).searchParams.get("cursor") === "60",
  );
  await list.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await thirdPage;
  await expect(list.locator(".event-row")).toHaveCount(60);
  await expect(list.locator(".event-load-sentinel")).toHaveCount(0);
  expect(requestedCursors).toEqual([null, "80", "60"]);
});

test("proxy workspace exposes owned live connections and targeted cancellation", async ({
  page,
}) => {
  await page.goto("/admin/proxy");
  await expect(page.getByRole("heading", { name: "Debrid Stream Proxy" })).toBeVisible();
  await expect(page.getByText("Example release")).toBeVisible();
  await expect(page.getByText("192.0.2.8")).toBeVisible();
  const [outcomeAxis, outcomeBar] = await activityMarkBounds(
    page,
    "Recent connections",
    ".recharts-bar-rectangle .recharts-rectangle",
  );
  expect(outcomeBar).toBeGreaterThanOrEqual(outcomeAxis - 1);
  const [concurrencyAxis, concurrencyLine] = await activityMarkBounds(
    page,
    "Peak concurrent",
    ".recharts-line-curve",
  );
  expect(Math.abs(concurrencyLine - concurrencyAxis)).toBeLessThanOrEqual(1);
  await page.getByRole("button", { name: "Cancel proxy connection for Example release" }).click();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("disabled proxy presents its configured state", async ({ page }) => {
  await page.unroute("**/api/v1/admin/proxy/snapshot");
  await page.route("**/api/v1/admin/proxy/snapshot", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: 1_785_000_000,
          enabled: false,
          summary: {
            active_connections: 0,
            current_speed: 0,
            session_bytes: 0,
            all_time_bytes: 0,
            completed_7d: 0,
            failed_7d: 0,
            bytes_7d: 0,
            average_duration_7d: 0,
          },
          active: [],
          history: [],
        }),
      ),
    }),
  );

  await page.goto("/admin/proxy");
  const disabledStatus = page.getByRole("status");
  await expect(disabledStatus.getByText("Debrid stream proxy is disabled")).toBeVisible();
  await expect(
    disabledStatus.getByText(
      "Enable the debrid stream proxy in Settings to accept new proxied streams.",
    ),
  ).toBeVisible();
});

test("scraping workspace exposes live scraper health, queue and controls", async ({ page }) => {
  await page.goto("/admin/scraping");
  await expect(page.getByRole("heading", { name: "Background scraper" })).toBeVisible();
  await expect(page.getByText("torrentio")).toBeVisible();
  await expect(page.getByText("Example series")).toBeVisible();
  await page.getByRole("button", { name: "Retry" }).click();
  await page.getByRole("button", { name: "Pause" }).click();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("Usenet workspace exposes native runtime, NNTP, inventory and actions", async ({ page }) => {
  await page.goto("/admin/usenet");
  await expect(page.getByRole("heading", { name: "Usenet" })).toBeVisible();
  await expect(page.getByText("Example Usenet release")).toBeVisible();
  await expect(page.getByText("Archive preparation")).toBeVisible();
  await expect(page.getByText("materialized_asset")).toBeVisible();
  const [axis, bar] = await activityMarkBounds(
    page,
    "Recent streams",
    ".recharts-bar-rectangle .recharts-rectangle",
  );
  expect(bar).toBeGreaterThanOrEqual(axis - 1);
  await page.getByRole("button", { name: "Cancel" }).click();
  await page.getByRole("button", { name: "Drain admission" }).click();
  await page.getByRole("button", { name: "Prune" }).click();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("CometNet workspace exposes peer telemetry and inline pool management", async ({ page }) => {
  await page.goto("/admin/cometnet");
  await expect(page.getByRole("heading", { name: "CometNet" })).toBeVisible();
  await expect(page.getByText("Paris peer")).toBeVisible();
  await page.getByRole("button", { name: /Trusted indexers/ }).click();
  await expect(page.getByText("creator-node")).toBeVisible();
  await expect(page.getByRole("button", { name: "Create invite" })).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(1);
});

test("disabled CometNet presents a stable disabled pool state", async ({ page }) => {
  await page.unroute("**/api/v1/admin/cometnet/snapshot");
  await page.route("**/api/v1/admin/cometnet/snapshot", (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(
        envelope({
          collected_at: 1_785_000_000,
          events: [],
          node: {
            average_latency_ms: 0,
            bytes_received: 0,
            bytes_sent: 0,
            connected_peers: 0,
            contribution_mode: null,
            enabled: false,
            healthy: false,
            inbound_peers: 0,
            invalid_messages: 0,
            messages_received: 0,
            messages_sent: 0,
            mode: "disabled",
            node_id: null,
            outbound_peers: 0,
            torrents_received: 0,
            torrents_sent: 0,
            uptime_seconds: 0,
          },
          peers: [],
          pools: [],
        }),
      ),
    }),
  );

  await page.goto("/admin/cometnet");
  await expect(page.getByRole("heading", { name: "CometNet is disabled" })).toBeVisible();
  await expect(page.getByRole("status", { name: "Loading pool details" })).toHaveCount(0);
  await expect(
    page.getByText("Enable a local node or relay to publish network state."),
  ).toHaveCount(2);
});
