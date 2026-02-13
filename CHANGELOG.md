# Changelog

<!-- markdownlint-disable MD024 -->

## [2.51.0](https://github.com/g0ldyy/comet/compare/v2.50.0...v2.51.0) (2026-02-13)


### Features

* implement Cinemeta as a fallback for IMDB metadata retrieval and centralize year parsing into a new utility module ([29d79c1](https://github.com/g0ldyy/comet/commit/29d79c100bc16213be23a0fd3c4cf2b1f1588948))

## [2.50.0](https://github.com/g0ldyy/comet/compare/v2.49.0...v2.50.0) (2026-02-11)


### Features

* add `parsed_matches_target` utility and use it to centralize season/episode filtering logic ([47b79ee](https://github.com/g0ldyy/comet/commit/47b79ee7fb0e4131ebe595da37c54be0a0f753a8))
* add `tzdata` package and set `TZ` environment variable to `UTC` ([29ac052](https://github.com/g0ldyy/comet/commit/29ac0525a55943612a4e3521acd843ddb7e00f71))
* add COMET_CLEAN_TRACKER setting to control tracker list display in Comet scraper ([a35327b](https://github.com/g0ldyy/comet/commit/a35327b111b8459926c7c8e3751e2388e16bf0e0))
* add configurable reachability check settings including retries, delay, and timeout for improved connection handling ([16f5df4](https://github.com/g0ldyy/comet/commit/16f5df4bceb1d2e77286a913eb27f543b62ab25d))
* add Debrid Account Scraper feature description to README ([4f4e351](https://github.com/g0ldyy/comet/commit/4f4e3518e48b01d3d543a2750d44d7f63afbe4c5))
* add debug logging to `_validate_torrent` for various torrent validation failure conditions ([bde663f](https://github.com/g0ldyy/comet/commit/bde663f48ad406f9d3e8a3f6de1ae88744d0c157))
* add DMM to supported scrapers and introduce DMM Ingester feature ([68fa89d](https://github.com/g0ldyy/comet/commit/68fa89de2dbb4ab8c7950b8a6cd51dc4308d34e2))
* add external reachability check for COMETNET_ADVERTISE_URL and introduce COMETNET_SKIP_REACHABILITY_CHECK for local testing ([f8983c7](https://github.com/g0ldyy/comet/commit/f8983c73970eec309476eef4afd8b48bf3657d09))
* add HTTP request handling to the WebSocket server for graceful responses to non-WebSocket requests ([4b0a187](https://github.com/g0ldyy/comet/commit/4b0a18738b364550500478d379f9f393529f193b))
* add local torrent existence check to optimize gossip validation and enhance message re-propagation ([8e04047](https://github.com/g0ldyy/comet/commit/8e04047c58b80791a74e448184427582206bbbea))
* add logging for incoming connection handling in ConnectionManager to track extracted IPs ([fe23a34](https://github.com/g0ldyy/comet/commit/fe23a34511e093d959a8ff1c807bea1aba4898e5))
* add max latency configuration and handling in CometNet to disconnect peers exceeding acceptable latency, enhancing network performance ([39b5e1b](https://github.com/g0ldyy/comet/commit/39b5e1b327c593bc2453824c798a7f19e7c33074))
* add method to reset discovery hysteresis in BackgroundScraperWorker for improved task management ([2a97dda](https://github.com/g0ldyy/comet/commit/2a97ddafde9b5a9be95e8761b7b8ac0e7d80a4a9))
* add optional node alias to CometNet ([9af324f](https://github.com/g0ldyy/comet/commit/9af324fc9b44d9435f328e966febfe779e7b8f24))
* add periodic state save functionality to CometNetService with configurable interval ([a209467](https://github.com/g0ldyy/comet/commit/a2094671d90c5c5c9d4422b5d6c8fc8ebfc14634))
* add persistence for the gossip engine's statistics ([8e71ee2](https://github.com/g0ldyy/comet/commit/8e71ee2ffa4cf86e5cf10aa7a1198200c745a70a))
* add PostgreSQL-specific logic for refreshing scrape locks using `RETURNING 1` to confirm update success ([dd8f8de](https://github.com/g0ldyy/comet/commit/dd8f8de42a1c8963542d9cf35f70064c6df42ee0))
* add support for cached availability checks across multiple services ([228cba0](https://github.com/g0ldyy/comet/commit/228cba0554509fa90068a02fa764f079a8b4266f))
* add version information display and update checking functionality to the admin dashboard ([d8c7aa8](https://github.com/g0ldyy/comet/commit/d8c7aa8c1395c2f96d256d215964ed8c1a5cbd69))
* allow `_scrape_media_type` to signal early termination and stop the scraping loop ([b96fd1f](https://github.com/g0ldyy/comet/commit/b96fd1f3de710d2ba70549d1a38f28cd16b5e64b))
* allow Kodi setup codes to be 6-16 characters long and update related prompts ([1c5b963](https://github.com/g0ldyy/comet/commit/1c5b9634d8e423cc4943191baf17f20bd2b7e2bd))
* apply reputation penalty when an invalid hex signature is encountered ([e23d630](https://github.com/g0ldyy/comet/commit/e23d63031dd25baf89b0a8c6fa950cf29a5972ac))
* asynchronously validate manifests and canonicalize float values for signature stability, while improving shutdown handling and error logging ([67083f2](https://github.com/g0ldyy/comet/commit/67083f22e0214e31e4252c89c80d5192b2af24e1))
* cache public key representations, validate incoming info hashes, and generalize message sending to support raw bytes ([6d74cef](https://github.com/g0ldyy/comet/commit/6d74cef8243dd8117b5ed56bd59ed8e9bd0430b8))
* canonicalize pool manifest timestamps to integers for signing to resolve float precision issues ([67240ad](https://github.com/g0ldyy/comet/commit/67240adec810ee49afd6dcf2a3043b0aecd908c4))
* derive `node_id` from public key for `PoolMember` and update admin dashboard to display node IDs ([bcd1b14](https://github.com/g0ldyy/comet/commit/bcd1b1478ba9466465913b304962cefcc45d3d1a))
* display current branch in the admin dashboard version information and visually refresh the version details section ([4b8ad5f](https://github.com/g0ldyy/comet/commit/4b8ad5fe85a4930f5fb5ef1147f47e8ada52c1e5))
* display peer torrents received in the admin dashboard and include reputation data in peer information ([1be6218](https://github.com/g0ldyy/comet/commit/1be62185a2b24c8b40e67253a0903c4626bc6f03))
* document CometNet P2P network and remove Nginx reverse proxy configuration from README ([f66c0f2](https://github.com/g0ldyy/comet/commit/f66c0f22ea836244fc2b629c951622a769b396e5))
* enable standalone CometNet service to directly save discovered torrents to the database ([9caab14](https://github.com/g0ldyy/comet/commit/9caab14a1b4ab31c8c566a0903000d501760d8b0))
* enhance admin dashboard with background scraper next cycle display and controls ([c2ce896](https://github.com/g0ldyy/comet/commit/c2ce896cc19fa1378078f83cfcb42aa663a0408f))
* enhance client IP retrieval in CometNetService by adding support for WebSocket requests ([b9f3dfa](https://github.com/g0ldyy/comet/commit/b9f3dfa8f30ddaed9d67d2dcf187291bfaa086db))
* enhance COMETNET_ADVERTISE_URL validation with security checks for internal domains, private IPs, and port range, and improve reachability checks for public IPs ([2da0020](https://github.com/g0ldyy/comet/commit/2da00206b4eb1a52642b0c295c5a0d519e1c712d))
* enhance copyInviteLink function to use async clipboard API with fallback for insecure contexts ([cce81ec](https://github.com/g0ldyy/comet/commit/cce81ecd9f70712d949690fc84a605960fa813cf))
* enhance cryptographic verification by adding public key loading and signature verification methods ([5e0afc0](https://github.com/g0ldyy/comet/commit/5e0afc0d25411497c1276350844b07584fb5a603))
* enhance debrid service to enrich torrent metadata from availability checks ([1699808](https://github.com/g0ldyy/comet/commit/1699808b479501a7dfc5bcfc00a982452c11b992))
* enhance DebridService with file index coercion and parsed data merging ([fe63e77](https://github.com/g0ldyy/comet/commit/fe63e772d7d75cbe4ac8a497105f854991ef3c60))
* enhance formatting functions with customizable styles for components and streamline secret string handling in Kodi setup ([3799022](https://github.com/g0ldyy/comet/commit/3799022b6095fb0c3dcee9785022ec83ee2fa300))
* enhance gossip statistics by adding repropagation tracking and updating dashboard labels ([80cc495](https://github.com/g0ldyy/comet/commit/80cc4954989fc9a201a91a986cc9697352eb8653))
* enhance handshake logging in ConnectionManager for better debugging and error tracking ([454817e](https://github.com/g0ldyy/comet/commit/454817e4b4c3c7e7c8e7a6238b637bc13d97f420))
* enhance info hash normalization with error handling and new format support ([6b9b33d](https://github.com/g0ldyy/comet/commit/6b9b33d0325a13f92c5f05d5ce69ea701fe92f66))
* enhance reachability check logic in `check_advertise_url_reachability` to handle hairpin NAT scenarios and improve logging for verification results ([20226ae](https://github.com/g0ldyy/comet/commit/20226aed5acfabc9fe0b70d6c9b996abfe3f0bf0))
* enhance state loading to tolerate integrity hash mismatches ([1fe70be](https://github.com/g0ldyy/comet/commit/1fe70be76d8018be94b920f60d36b3470448841d))
* enhance system clock synchronization check to use multiple endpoints and provide detailed error reporting ([9f51226](https://github.com/g0ldyy/comet/commit/9f51226dd45dd5b09b4444cd0057272a11ada6aa))
* enhance WebSocket connection and handshake logging in ConnectionManager to improve debugging and error tracking ([35f32d6](https://github.com/g0ldyy/comet/commit/35f32d674aa4e8a8e49f9583d7f25a85d64a1020))
* enhance WebSocket connection logging in CometNetService for improved client IP tracking ([979a26e](https://github.com/g0ldyy/comet/commit/979a26edd797584b756761f255c8041dc59852bb))
* filter episodes based on the retry status of their associated series item ([0696166](https://github.com/g0ldyy/comet/commit/06961667fe9053e8755fc49ee0cbcd7c803aa99d))
* implement a custom Kodi setup dialog with dedicated error alerts and expand the Kodi add-on documentation ([526c6e2](https://github.com/g0ldyy/comet/commit/526c6e2c2d55ccfea9a18bf040475e4c499542d2))
* implement a custom Kodi setup dialog with dedicated error alerts and expand the Kodi add-on documentation ([6aaf39d](https://github.com/g0ldyy/comet/commit/6aaf39d3e637e330941b40e3bb96c47ad69b4225))
* implement a Kodi repository build system, including a new Makefile, generation script, repository definition, and GitHub Actions workflow ([cd2f6ab](https://github.com/g0ldyy/comet/commit/cd2f6ab470eea9154ad73b83549cee9f3121af1e))
* implement and integrate pool member contribution tracking within the PoolStore and record contributions during gossip processing ([f67b941](https://github.com/g0ldyy/comet/commit/f67b9419992acc6aa89fee05a3ca2cb0e708d870))
* implement concurrent lock maintenance as a separate task to ensure continuous scraping and robustly handle lock loss ([1172adf](https://github.com/g0ldyy/comet/commit/1172adf25896cda2db2157079f928b4caa266fe5))
* implement debrid account torrent scraping ([c36475b](https://github.com/g0ldyy/comet/commit/c36475bd7c135c4bf0de637ab167607ffacb0747))
* implement dedicated crypto executor management in utils and shutdown procedure in CometNetService to enhance resource handling ([a944efc](https://github.com/g0ldyy/comet/commit/a944efc9c1b7bc26b896f3d38e2b7c83b5f7d4a1))
* implement distributed locking for the background scraper and remove the `background_scraper_progress` table ([1b9d8e1](https://github.com/g0ldyy/comet/commit/1b9d8e1ac4850b47d159d77f6dbfda95919b0c3d))
* implement functionality for users to leave CometNet pools with role-based restrictions ([4081d11](https://github.com/g0ldyy/comet/commit/4081d1147736f4573984c7e5e013284f14ec97d6))
* implement mandatory and auto-generated `COMETNET_API_KEY`, and add auto-generation for `ADMIN_DASHBOARD_PASSWORD` and `PROXY_DEBRID_STREAM_PASSWORD` ([29a8a5a](https://github.com/g0ldyy/comet/commit/29a8a5a04decc3f291524140d2308425b8557b03))
* implement membership reconciliation in CometNetService and improve error logging in CometNetRelay ([6cca3f4](https://github.com/g0ldyy/comet/commit/6cca3f4ac1c38499c07df9bcbe100293770322b9))
* implement message rate limiting and refactor message security validation into a dedicated module ([0a75780](https://github.com/g0ldyy/comet/commit/0a7578009dc7bb1a864726fa9b786556531f4d23))
* implement new pools management UI with filtering and search functionality in admin dashboard ([12c4695](https://github.com/g0ldyy/comet/commit/12c46955dae25216d533089d0fdfea4e9e340d9a))
* implement private network mode with HMAC authentication, status display, and enhanced logging ([e154fc0](https://github.com/g0ldyy/comet/commit/e154fc0deb482912415a69d9114fa431ce340ec0))
* implement proactive distributed lock refreshing and add error handling for background tasks ([c170fc6](https://github.com/g0ldyy/comet/commit/c170fc68a0ae35befd7408d3149c5a7a02f3d483))
* implement queue-based backpressure and discovery pausing for the background scraper using watermarks ([af664a7](https://github.com/g0ldyy/comet/commit/af664a716beb19b7863feebc0d70c87628a85bf1))
* implement queue-based backpressure and discovery pausing for the background scraper using watermarks ([6eaff9c](https://github.com/g0ldyy/comet/commit/6eaff9c8d8ffd3166d6e87335c3634bdc09b8a9c))
* implement resolution-based selection of info hashes for torrent streaming ([1814efa](https://github.com/g0ldyy/comet/commit/1814efa84c0e54ddb93a2ef8ffce224cc398bc40))
* implement seadex scraper and fix anime media id parser ([436de99](https://github.com/g0ldyy/comet/commit/436de999f9ef25de8e804af7363e7d6fdb78a648))
* implement self-removal from pools by broadcasting a signed leave message and updating local cleanup logic ([db15cd7](https://github.com/g0ldyy/comet/commit/db15cd72cc0bf878b6da9625d8c73c9ce5ec3ee2))
* implement sharded and deduplicated filter parse cache ([fab96ae](https://github.com/g0ldyy/comet/commit/fab96ae3a48af71f2d9ab4627475586e14971f27))
* implement system clock synchronization check on CometNet startup with configurable tolerance and timeout ([51a81e4](https://github.com/g0ldyy/comet/commit/51a81e49d38d93aa2cfdd5507b908d31f0902625))
* improve caching and scraping by supporting linked Kitsu and IMDb media IDs and refining season/episode parameters for debrid services ([072dcab](https://github.com/g0ldyy/comet/commit/072dcaba45b27468467913258c9ee1c073b06d4f))
* improve client IP extraction in WebSocket connections by introducing get_client_ip_any function and updating connection handling in CometNetService and ConnectionManager ([f2f5838](https://github.com/g0ldyy/comet/commit/f2f58389ff1ce7e6f42b1bdc24fa04acb52182b4))
* improve connection handling in ConnectionManager by closing duplicate connections and reusing existing ones ([bd0a5f4](https://github.com/g0ldyy/comet/commit/bd0a5f465bee3720b10db3c9c17659bdcdabf9b0))
* improve data canonicalization by sorting visited nodes and robustly handling dictionary keys, and add debug logging for signature verification ([2c8b3f7](https://github.com/g0ldyy/comet/commit/2c8b3f716d540324e088793986dd6e998a1a88d2))
* improve error handling in torrent title parsing ([85fc456](https://github.com/g0ldyy/comet/commit/85fc4566c845ea0bd2f4e52fa9ebe3d5cad3d963))
* improve error handling in torrent title parsing ([42a93d4](https://github.com/g0ldyy/comet/commit/42a93d4c22df5926f448ff9eadea5b828a2a1418))
* improve Kodi integration with direct VideoInfoTag usage, robust setup, and refined stream info handling ([a62dc06](https://github.com/g0ldyy/comet/commit/a62dc062c8334efb5fa65a197a6c784928a42e47))
* increase default relay timeout and add specific handling for `asyncio.TimeoutError` during batch sends ([f8819b1](https://github.com/g0ldyy/comet/commit/f8819b1c1763869623b4cb7a6b38ebd9cd1dd3e3))
* init cometnet ([748249e](https://github.com/g0ldyy/comet/commit/748249e647f58c9bf37c1d069c44802d7a4d5ed7))
* introduce a dirty manifest tracking mechanism and `flush_dirty_manifests` method to batch manifest writes ([b37ec8a](https://github.com/g0ldyy/comet/commit/b37ec8a2befa921fe62107d4dc18b262d7ec7ebb))
* Introduce DMM ingester ([19001ee](https://github.com/g0ldyy/comet/commit/19001eeb6b4f6b45e14880a998a2a6ed19df5200))
* introduce Kodi addon with pairing service, setup utilities, and updated documentation ([d27239d](https://github.com/g0ldyy/comet/commit/d27239d8fbd33d5c549345d9c52ff16efb9dc924))
* introduce smart language detection by leveraging country-specific Trakt aliases ([c413428](https://github.com/g0ldyy/comet/commit/c4134289e80385b839a22df18c724b7c83369738))
* log private addresses announced by incoming peers and add a setting to prevent sharing private IPs during PEX ([7160406](https://github.com/g0ldyy/comet/commit/71604060c8ad54e9ea998ace90facb3311b7559e))
* log specific timeout warnings for scraper exceptions instead of general ratelimiting messages. ([311bfae](https://github.com/g0ldyy/comet/commit/311bfaee844a51e184d3a99f8fd0c44ab39b57b9))
* nekobt scraper ([da28fc4](https://github.com/g0ldyy/comet/commit/da28fc4e5c4c5ae5727b73db73b9e6b5bb0a8f0f))
* new background scraper ([cebb785](https://github.com/g0ldyy/comet/commit/cebb785d21058fe9cd70ceaa407add3724d42120))
* optimize queue snapshot retrieval in BackgroundScraperWorker by consolidating database queries and normalizing discovery limits ([dabbee5](https://github.com/g0ldyy/comet/commit/dabbee5c5bd749f5d0006edce852481e5d4f7216))
* record own contributions ([a36a9d1](https://github.com/g0ldyy/comet/commit/a36a9d19efff9e93e909306f999b224e5602f06e))
* refactor HTTP client management and enhance caching mechanisms ([4178cda](https://github.com/g0ldyy/comet/commit/4178cda8b538641605ef91740e7c524acead6c4e))
* refactor HTTP client management and enhance caching mechanisms ([efca5db](https://github.com/g0ldyy/comet/commit/efca5dbfded727e02a5f335ad8e6941151949f3c))
* relax connection limits and overrepresentation checks for private IP addresses ([a8cefbd](https://github.com/g0ldyy/comet/commit/a8cefbd66ec0cf79dec5f6585f8844ac1059e053))
* schedule debrid account sync tasks as background tasks when warm sync does not time out ([577a83e](https://github.com/g0ldyy/comet/commit/577a83eec9cc6a7889d7d0f87ff19c811c017e77))
* seadex anime only ([e15125a](https://github.com/g0ldyy/comet/commit/e15125a339b27a7f3c976b6dd140c5da99c41483))
* seadex anime only ([9b36762](https://github.com/g0ldyy/comet/commit/9b36762c5ac4d5582b7026631a5f175b8a5a67c0))
* standardize database conflict handling and type checks with new constants, and add cometnet torrent existence batch check callback ([f8e76e9](https://github.com/g0ldyy/comet/commit/f8e76e9b1832f23369e2905b5a7c19d52c7fed64))
* track and handle completion of debrid account sync background tasks ([041a3ac](https://github.com/g0ldyy/comet/commit/041a3acc0e658647e5bf8603979fc3901ef6a353))
* update canonicalization to preserve float precision and add signature verification debug logs ([f1e871e](https://github.com/g0ldyy/comet/commit/f1e871e8abcb2d5542ab07a5bc81d9300e1ad516))
* update Kodi integration with new configuration handling, improved manifest URL generation, and enhanced user prompts ([43964ac](https://github.com/g0ldyy/comet/commit/43964accbc8e80c570acc843bc0175b4caed0aa0))
* version and update checker ([7c9842a](https://github.com/g0ldyy/comet/commit/7c9842a937ab117c02913141bd24d9a09c211a83))


### Bug Fixes

* `_save_manifest_async` now returns a boolean, allowing `flush_dirty_manifests` to re-queue failed manifest saves ([cfb648a](https://github.com/g0ldyy/comet/commit/cfb648a06cd99c30aaaf1f3be6721ac410a7a687))
* add HTTP status error handling to AnimeTosho and correct status attribute usage in Nyaa ([b188eda](https://github.com/g0ldyy/comet/commit/b188edadd5e0d5d0cdb06ccac2a99e65abba80e7))
* adjust background scraper cycle timing to account for missed cycles ([39da5c5](https://github.com/g0ldyy/comet/commit/39da5c5d2ed3182587902f444f8a26458d61280f))
* broaden WebSocket noise error filtering ([47b4657](https://github.com/g0ldyy/comet/commit/47b465729b3922cb261082674d8e8e9804d21ca6))
* chunk `execute_many` calls for SQLite to respect its parameter limit ([2352aa6](https://github.com/g0ldyy/comet/commit/2352aa6a0f0e138b9d4c586903e1ff028fbafaa1))
* cinemata no releaseInfo ([5f1ca3e](https://github.com/g0ldyy/comet/commit/5f1ca3ea53a657567b7dd04e30fd8a5f167437cc))
* correct tracker extraction logging for SeaDex scraper ([44aa0d8](https://github.com/g0ldyy/comet/commit/44aa0d840b71b1ca5e543b3373cd3db23ec36a96))
* D ([721fa0f](https://github.com/g0ldyy/comet/commit/721fa0fcffe9c289594d4e02d2e86f4f2772edb0))
* default `name_query` parameter to an empty string instead of `None` ([49a7302](https://github.com/g0ldyy/comet/commit/49a7302066d6f470a5b5efa8a7b62d702bf07971))
* **docker:** add make to build dependencies for miniupnpc ([2fcb2ef](https://github.com/g0ldyy/comet/commit/2fcb2ef4b82d58fdf59087354124578ea9d1643f))
* **docker:** increase uv timeout to 300s to avoid network errors ([dc98727](https://github.com/g0ldyy/comet/commit/dc987272af34f939db0f11a8f2a28129907f7927))
* ensure default last_seen time is used when sorting keys in PublicKeyStore ([b7bf69e](https://github.com/g0ldyy/comet/commit/b7bf69e620d6b89bed05f7e72fe8b8bfda169dbb))
* ensure proper cancellation of in-flight tasks in BackgroundScraperWorker during cancellation ([5d4c5fd](https://github.com/g0ldyy/comet/commit/5d4c5fd70ac1f004b90e01f67ea5634230fc19c3))
* ensure proper lock refresh handling and add read timeout for DMM hashlist downloads. ([bbf2639](https://github.com/g0ldyy/comet/commit/bbf2639ca6c34aeafdd3e6b25e356c56f0aff52b))
* ensure proper shutdown and tracking of relay batch flush tasks and reorder README features ([53b9c61](https://github.com/g0ldyy/comet/commit/53b9c61b277548e095bd4a6934aee2b1119020da))
* ensure season and episode checks in `parsed_matches_target` handle None values correctly ([0a9ace9](https://github.com/g0ldyy/comet/commit/0a9ace9db0efef99823497bcbaa0a9bfd5072a75))
* ensure TorrentMetadata size is an integer and sort pool members by public key for deterministic serialization ([5fe3307](https://github.com/g0ldyy/comet/commit/5fe330795582ff37607421d89fdf57d014dc7970))
* exclude subscribed and member pools from the discover count calculation ([37e7e6f](https://github.com/g0ldyy/comet/commit/37e7e6f5c1dd07c21eea9e6b21d8dcaf4d4954c1))
* filter private IPs from PEX and warn on misconfiguration ([7ee5b20](https://github.com/g0ldyy/comet/commit/7ee5b208c61a54dd9ab43d89868f528ab8212c5b))
* first search emptying itself when torrent cache ttl is -1 ([9237b8c](https://github.com/g0ldyy/comet/commit/9237b8c7065f77500a5486311a2495ac96bb7dd7))
* fix _persist_kitsu_imdb_mapping method ([40a8fc3](https://github.com/g0ldyy/comet/commit/40a8fc387e0c5ce5200c9e0dd5f5a02965ad4bf8))
* fix various memory leaks ([3798971](https://github.com/g0ldyy/comet/commit/37989716c5388c0acdfd0dfc204e87ae0587d907))
* fix volume on official docker compose ([820ebf0](https://github.com/g0ldyy/comet/commit/820ebf0ae3d1f577add6fb8ff48df122f66d647f))
* gracefully cancel and drain in-flight scraping tasks upon lock loss instead of abruptly stopping ([2268e8f](https://github.com/g0ldyy/comet/commit/2268e8f106fb7fd2c77d4adfd34134cb01ffb7f4))
* gracefully handle UnicodeEncodeError during filename processing in DMM ingester ([6e68a93](https://github.com/g0ldyy/comet/commit/6e68a9398460fe9cedc2494148d91f67769de3d9))
* handle tracker extraction more robustly by ensuring it defaults to None when not present ([7fd92bc](https://github.com/g0ldyy/comet/commit/7fd92bc83ffdbad082ce94d0d4aaf5db1967f6b6))
* improve error handling for WebSocket connections with detailed logging ([1c536c8](https://github.com/g0ldyy/comet/commit/1c536c8b126b513762433d55618aee11e9fdf8d6))
* pin mediaflow-proxy dependency to version 2.4.1 for compatibility ([a69a1df](https://github.com/g0ldyy/comet/commit/a69a1dfefa5de052a2cfc1bee50bd90ef19e28d9))
* pool join ([c09a143](https://github.com/g0ldyy/comet/commit/c09a14374c815405d0e1f044844499398f1abb73))
* postgres ([11aef0f](https://github.com/g0ldyy/comet/commit/11aef0f98525e1f05fd22010077d16a6abc8be22))
* potential race can drop queued broadcasts during timeout shutdown ([92aadf3](https://github.com/g0ldyy/comet/commit/92aadf32daf531b6cf418ce6b1962883e3ae9f3c))
* prevent CometNet from starting with multiple FastAPI workers in non-relay mode ([a76a36a](https://github.com/g0ldyy/comet/commit/a76a36acc11d51d03917debd627073a414758c0e))
* prevent CometNet startup with private advertise URLs on public networks unless explicitly allowed ([e6c8a4c](https://github.com/g0ldyy/comet/commit/e6c8a4c57a6c2d39b3dcd47fc3725668e857ad63))
* print full traceback for exceptions occurring during the scraping cycle ([0bf3390](https://github.com/g0ldyy/comet/commit/0bf3390dfe55564e73b8bf66ab22d7c5613507d4))
* remove `OR_IGNORE` from the `first_searches` INSERT statement ([390b452](https://github.com/g0ldyy/comet/commit/390b45223870954aa1ab63832fef8461407450eb))
* remove ellipsis from truncated node ID displays in the admin dashboard ([96e333d](https://github.com/g0ldyy/comet/commit/96e333d01960442d5671010d5559c6133a5fc488))
* remove signal handler ([bad47c4](https://github.com/g0ldyy/comet/commit/bad47c45030206329805d41a8f6127608ab57c55))
* remove the update check interval and related logic to always perform an update check ([9327826](https://github.com/g0ldyy/comet/commit/9327826ed497b8678a9989942352ef2955cad92d))
* suppress unsupported HTTP method HEAD errors from websockets logs using a new filter ([de4cd80](https://github.com/g0ldyy/comet/commit/de4cd808ba202d9b6123010f2b76ad9e8bab3cd2))
* tracker extraction result is discarded ([9448806](https://github.com/g0ldyy/comet/commit/9448806ea5a8ad1fd7581d8353ad9c72d0ae2f01))
* update anime_entries insert query to handle conflicts by updating existing data ([fa15430](https://github.com/g0ldyy/comet/commit/fa15430ba298a20f14756a81dc3bd6a37aa4e60f))
* update contribution recording logic in `PoolStore` to allow contributions to be recorded across all pools if no specific pool ID is provided, enhancing flexibility in member contribution tracking ([2887a22](https://github.com/g0ldyy/comet/commit/2887a223b995f40d6017412dc2dce09535c76f94))
* update lock expiration logic in DistributedLock to refresh based on current loop time ([372efe2](https://github.com/g0ldyy/comet/commit/372efe28b999eb85129170cae5ecd05e9d6dcb8e))


### Performance Improvements

* convert key network discovery and cryptographic operations to asynchronous to prevent event loop blocking ([73af287](https://github.com/g0ldyy/comet/commit/73af2875f2e49e64918cddc5bf49cdd5caf29926))
* offload CPU-bound cryptographic operations to an executor, optimize torrent batch processing, and cache public key data ([6a9f9fd](https://github.com/g0ldyy/comet/commit/6a9f9fdf2d16e0a4e91dd5ecb03670db29a52dcf))
* remove torrent debrid service short-circuit ([04386ed](https://github.com/g0ldyy/comet/commit/04386ed619036842e47ce2b3140d3eff198523ca))

## [2.49.0](https://github.com/g0ldyy/comet/compare/v2.48.0...v2.49.0) (2026-01-15)


### Features

* enhance error handling in multi-debrid service availability checks ([b1d621a](https://github.com/g0ldyy/comet/commit/b1d621ae18b8104d5bdf3f11445afe7bba9d352e))
* implement multi-debrid service support and enhance configuration ([6cadc4d](https://github.com/g0ldyy/comet/commit/6cadc4d57595632162118553c7952fc20c367dd6))
* implement multi-debrid service support and enhance configuration ([f6abcf5](https://github.com/g0ldyy/comet/commit/f6abcf5e4c38339f14c3cc462782b010bfc90aae))
* optimize service availability checks by filtering unique services ([9296da1](https://github.com/g0ldyy/comet/commit/9296da1aa22d13503747ad93578fc428505e4fe3))

## [2.48.0](https://github.com/g0ldyy/comet/compare/v2.47.0...v2.48.0) (2026-01-15)


### Features

* add DATABASE_FORCE_IPV4_RESOLUTION setting and update logging ([4b6c51a](https://github.com/g0ldyy/comet/commit/4b6c51af39e1b88c25771fe1b9fc2f4363c441f5))
* add hostname resolution method to ReplicaAwareDatabase ([81842ba](https://github.com/g0ldyy/comet/commit/81842bacf8d5ac4f1be1cffac46c79155e3075a8))
* add manifest and configure page caching settings ([6b9dbcd](https://github.com/g0ldyy/comet/commit/6b9dbcdab3672f7890c98895ca50e6bbdfe74832))
* add manifest and configure page caching settings ([60c90bc](https://github.com/g0ldyy/comet/commit/60c90bc2e842808c7fbf91d861bcf1a5916fc054))
* add support for downloading generic trackers at startup ([4e701d8](https://github.com/g0ldyy/comet/commit/4e701d89c8eba70fb9998e9bba932aefc9d677db))
* enhance caching policies and improve manifest handling ([463ab41](https://github.com/g0ldyy/comet/commit/463ab4154c9a1c39428c1acdbcb9b8bba04aa68a))
* enhance caching policies and improve manifest handling ([23ab2f4](https://github.com/g0ldyy/comet/commit/23ab2f44b187f72048376bd3637395d781344276))
* enhance language settings and parsing functionality ([430b630](https://github.com/g0ldyy/comet/commit/430b6308a11744cf81e41b6093facbcadacd31e1))
* implement IPv4 hostname resolution in ReplicaAwareDatabase ([ddde28f](https://github.com/g0ldyy/comet/commit/ddde28f8a51fa682d9051460c2cd8e15856309b3))
* pin python version ([566b28d](https://github.com/g0ldyy/comet/commit/566b28d13fbfc51031e3f93e83acc65c4774797f))
* remove unused index ([52609ad](https://github.com/g0ldyy/comet/commit/52609addaf54c0d6fa6c576cdc191f1a1ffc4a8e))
* update resolution options ([212d874](https://github.com/g0ldyy/comet/commit/212d8744c177ecd7cbfd636c3f8eecf25eb10cfb))
* update resolution options ([53961ef](https://github.com/g0ldyy/comet/commit/53961ef442ec689ca4bc2aa51efbba3b1323c547))


### Bug Fixes

* clear trackers list before downloading new data ([d396007](https://github.com/g0ldyy/comet/commit/d396007a1954f7f25506f7585a51b8fa1484c408))
* correct formatting in README.md ([8ff725d](https://github.com/g0ldyy/comet/commit/8ff725dc63740c330bd1fd0228d5bffac331f1b1))
* correct typo in DATABASE_FORCE_IPV4_RESOLUTION comment in .env-sample ([f80430d](https://github.com/g0ldyy/comet/commit/f80430d706c208cc2c2fd25b6ad47918a7014a37))
* handle 404 response in Peerflix scraper ([7fa0c2b](https://github.com/g0ldyy/comet/commit/7fa0c2bb4b690a860f6195d47dc1ad2572e6cecc))
* preserve quality and languages from original parsed data in DebridService ([3f93efb](https://github.com/g0ldyy/comet/commit/3f93efb19b504a30cb78b0f23f74b003fef00756))
* set default max executor workers to 1 ([e683637](https://github.com/g0ldyy/comet/commit/e6836374eccf51feb7ea2ee3e1f6e06f8bc1f013))

## [2.47.0](https://github.com/g0ldyy/comet/compare/v2.46.0...v2.47.0) (2026-01-09)


### Features

* add background and icon image assets ([d9dceab](https://github.com/g0ldyy/comet/commit/d9dceabcb071abaaa525e5a0dfae3c9ffd2a1ab2))
* anime mapping disabler ([1c0bf9b](https://github.com/g0ldyy/comet/commit/1c0bf9bcc4f8e90a5433bf18e8fabd37b309f9ef))
* enhance database indexing and improve torrent processing efficiency ([add3813](https://github.com/g0ldyy/comet/commit/add3813663a6d65c5d893f33161852cb1ba817ad))
* fix a few useless things ([1f29c4c](https://github.com/g0ldyy/comet/commit/1f29c4ca1ba074f35760ce7549098b68e6b7d778))
* fix aiostreams null infoHash ([880927c](https://github.com/g0ldyy/comet/commit/880927c9186d1bb38a6103e5c8c52bfcef41a69a))
* fix slow anime mapper loading ([67f7740](https://github.com/g0ldyy/comet/commit/67f774029d0394f53d3999943bbd04e184605065))
* implement HTTP caching mechanism ([35eab54](https://github.com/g0ldyy/comet/commit/35eab54a7caa68f152583014680e7605619f36d4))
* implement smart file selection algorithm and refine torrent record management for multi-episode content ([479b5ae](https://github.com/g0ldyy/comet/commit/479b5ae8ff2bc800b9ed8dff0f418189391e1342))
* kitsu offsets ([4af9add](https://github.com/g0ldyy/comet/commit/4af9addd7b05f69a16df317b1e5fbe2aa3e62d2b))
* log http cache environment variables on startup ([f6751e5](https://github.com/g0ldyy/comet/commit/f6751e529c2ceec3685514f35c2c73c63b143cc7))
* remove verbose logging from anime service ([8cd1e0b](https://github.com/g0ldyy/comet/commit/8cd1e0bce6795e6b6f1987355a49d38a183677ac))
* super mega powerful ultra anime mapper ([7035b24](https://github.com/g0ldyy/comet/commit/7035b24b3ca9cf7d5d190080b03e425f003849ec))


### Bug Fixes

* add check for existing info_hash in torrents before processing ([06bbe9b](https://github.com/g0ldyy/comet/commit/06bbe9bbd84cd4f632f3115500a4219864414b54))
* CachedJSONResponse overwrites body after init ([72bb885](https://github.com/g0ldyy/comet/commit/72bb8859a70152e1f4aea3188354d2e8ef66b8cc))
* ETag caching broken by random manifest ID ([e74c29f](https://github.com/g0ldyy/comet/commit/e74c29fdd187a2dc8de257d70c09e30924ea70bf))
* fix wrong ranking order in p2p mode ([d4d7e60](https://github.com/g0ldyy/comet/commit/d4d7e6091c3af431c3e5cbb9ec9bc48218b252d4))
* little opti ([c7214f2](https://github.com/g0ldyy/comet/commit/c7214f213de8e96ecd06b5a32aa39bad5e23c466))
* remove fake config from comet scraper ([b5856b6](https://github.com/g0ldyy/comet/commit/b5856b6554f04d4d9fc4cb0bc5f9d6da93dd9c36))
* update asset URLs from ibb.co to GitHub raw content ([77c9f04](https://github.com/g0ldyy/comet/commit/77c9f043c32a59eeee53ae123323ce1afb3eed75))

## [2.46.0](https://github.com/g0ldyy/comet/compare/v2.45.0...v2.46.0) (2026-01-06)


### Features

* add AnimeTosho scraper ([1b87b1e](https://github.com/g0ldyy/comet/commit/1b87b1e5017d06f59c63990d0ccb1c714a8494b5))

## [2.45.0](https://github.com/g0ldyy/comet/compare/v2.44.0...v2.45.0) (2026-01-06)


### Features

* add `RTN_FILTER_DEBUG` setting to enable verbose logging for torrent filtering rejections ([b9cc11d](https://github.com/g0ldyy/comet/commit/b9cc11d109a68bac31732ed1ec7f253ae4ba1463))
* add `RTN_FILTER_DEBUG` setting to enable verbose logging for torrent filtering rejections ([ddaafa2](https://github.com/g0ldyy/comet/commit/ddaafa22ca7b73bc62994da33f23bb0560488c3b))
* allow configuration of ProcessPoolExecutor max workers with auto-detection and logging ([c6c5c42](https://github.com/g0ldyy/comet/commit/c6c5c42b974d6fe50ab3fd196d524345015d5dee))
* allow configuration of ProcessPoolExecutor max workers with auto-detection and logging ([7e98d1f](https://github.com/g0ldyy/comet/commit/7e98d1f067b864261c40a871bdc6f98f38c7e0d5))
* introduce `update_interval` for torrent upsert logic and implement batched upsert for SQLite ([6b83124](https://github.com/g0ldyy/comet/commit/6b831243ca289b7ae9323018161e4fb9a092396c))
* introduce configurable exponential backoff for 429 rate limit errors ([b65c285](https://github.com/g0ldyy/comet/commit/b65c2857f9894a57939ee6a86833317ea730e983))
* optimize PostgreSQL database operations by adding a covering index, enhancing debrid cache upserts with conditional updates, and refactoring torrent manager's advisory locking ([3911533](https://github.com/g0ldyy/comet/commit/39115334d81de44c4cb351b05f2fa86f0bab286e))
* switch from session-level to transaction-level PostgreSQL advisory locks for database cleanup and batched upserts ([04dfd38](https://github.com/g0ldyy/comet/commit/04dfd38c8f892af72d33641ab3b8af900e68b3e3))
* update BitMagnet scraper to use IMDb ID and media type for queries ([3ef2d34](https://github.com/g0ldyy/comet/commit/3ef2d3454bbd456da43ff29c21e125e2e51a1f27))
* update BitMagnet scraper to use IMDb ID and media type for queries ([f53c53b](https://github.com/g0ldyy/comet/commit/f53c53ba1726e2f88f747b0395aee9d462130811))


### Bug Fixes

* add error handling for torrent title extraction and unreleased content in comet scraper ([7db932f](https://github.com/g0ldyy/comet/commit/7db932ff66c86c3fd30502f5bdadb2a703b88bf7))
* correctly handle `None` values for season and episode parameters in API requests for bitmagnet ([9b91793](https://github.com/g0ldyy/comet/commit/9b9179371daf0e91f345b328dfda309941f68724))
* preserve original traceback when re-raising exceptions ([b1cf30b](https://github.com/g0ldyy/comet/commit/b1cf30b4e82f51ec6695e10efde4589979220f09))
* remove 60-second minimum for live torrent cache update interval calculation ([0b27e3c](https://github.com/g0ldyy/comet/commit/0b27e3cb4d43f26d45840a4aa5efa84ada7d113c))
* remove early exit when torrent content is not digitally released ([9137096](https://github.com/g0ldyy/comet/commit/913709628874699eb6664c5a4e5005a8cf98396e))


### Performance Improvements

* add `idx_torrents_info_hash` for improved lookup performance ([b669b37](https://github.com/g0ldyy/comet/commit/b669b379c07ba28c8ea8abdff058b77786e1b350))
* add index on torrents (info_hash, season) to optimize concurrent DELETE operations ([980fe19](https://github.com/g0ldyy/comet/commit/980fe194f63003bc464fe5ac0919f804d3171698))
* remove conditional check for empty `sanitized_rows` before `execute_many` database call ([f1d3ea3](https://github.com/g0ldyy/comet/commit/f1d3ea3bdbee02a06785e9ae60f2bfd64d6f06e7))
* remove PostgreSQL covering index `idx_torrents_covering` for torrents table ([4f0be0a](https://github.com/g0ldyy/comet/commit/4f0be0a786681cfac1f8e43d98c538abdf762cb9))
* use non-blocking advisory locks and conditionally insert rows based on lock acquisition ([fb47fa5](https://github.com/g0ldyy/comet/commit/fb47fa51f862ee68b483d250555346455e8d3f56))
* use non-blocking advisory locks and conditionally insert rows based on lock acquisition ([60ff4be](https://github.com/g0ldyy/comet/commit/60ff4be61c84fabfce1d664ed464a42f3006e6eb))

## [2.44.0](https://github.com/g0ldyy/comet/compare/v2.43.0...v2.44.0) (2026-01-04)


### Features

* add new scraper configurations and clarify the `PROXY_ETHOS` `on_failure` option in the sample environment file ([4a34aad](https://github.com/g0ldyy/comet/commit/4a34aad8b7dfc32b18b868f57cbd6b60460e36bb))
* add TorrentsDB scraper and remove redundant `pass` statements in other scrapers ([f486247](https://github.com/g0ldyy/comet/commit/f4862470a50b4463f1d287bb48688c8a4ab3e343))
* add TorrentsDB scraper and remove redundant `pass` statements in other scrapers ([c26140d](https://github.com/g0ldyy/comet/commit/c26140d6ce5ef0474ca2d1f616af138560f158ed))
* enable dynamic proxy configuration by allowing extra Pydantic settings fields and setting the default proxy ethos to 'always' ([bbeafd7](https://github.com/g0ldyy/comet/commit/bbeafd7ac0e975f49bac68cbe0dd7b381b70d13e))
* enhance network manager with proxy hostname resolution for curl_cffi ([65a7464](https://github.com/g0ldyy/comet/commit/65a7464acc1cbc84953aac19f99fc8024a984bb0))
* refactor live torrent caching to differentiate between displaying existing results and triggering new scrapes, and update default cache TTLs ([7dde28d](https://github.com/g0ldyy/comet/commit/7dde28d88d409b1549a0dadd5cd4c2cc60bd3552))
* refactor live torrent caching to differentiate between displaying existing results and triggering new scrapes, and update default cache TTLs ([7af6d09](https://github.com/g0ldyy/comet/commit/7af6d0980b2f22f9f9e0cf4edb5e236945bc36dd))


### Bug Fixes

* background scraper can't scrape tv shows ([620dca3](https://github.com/g0ldyy/comet/commit/620dca33c87511e7f8c4b43feaccf3ec47c94224))

## [2.43.0](https://github.com/g0ldyy/comet/compare/v2.42.0...v2.43.0) (2026-01-02)


### Features

* add configurable `PROXY_DEBRID_STREAM_INACTIVITY_THRESHOLD` setting to enable and refine the cleanup of inactive debrid stream connections ([cfc0eae](https://github.com/g0ldyy/comet/commit/cfc0eaea847779e111b63a26d93f50714d859ab8))
* add configurable `PROXY_DEBRID_STREAM_INACTIVITY_THRESHOLD` setting to enable and refine the cleanup of inactive debrid stream connections ([aef99f4](https://github.com/g0ldyy/comet/commit/aef99f43bed584f352c469c143693fdf27eb7b21))
* add configuration and UI option to sort cached and uncached stream results together ([a472595](https://github.com/g0ldyy/comet/commit/a4725958d051a11eed651a065578691ec0d2e1d6))
* add configuration and UI option to sort cached and uncached stream results together ([5fda0df](https://github.com/g0ldyy/comet/commit/5fda0df1a98f313c49fdd3e65eafd24ae792924e))
* populate `sortCachedUncachedTogether` checkbox from settings ([a5698b2](https://github.com/g0ldyy/comet/commit/a5698b28ac64996e675dcf49867192286b5a33db))

## [2.42.0](https://github.com/g0ldyy/comet/compare/v2.41.0...v2.42.0) (2026-01-02)


### Features

* add fallback to check watch providers for movie release dates when upcoming release date is unavailable ([ac2cafb](https://github.com/g0ldyy/comet/commit/ac2cafb8abc1c46f8d44c8682f80e0ea0b0cdf2f))
* add fallback to check watch providers for movie release dates when upcoming release date is unavailable ([6b6e466](https://github.com/g0ldyy/comet/commit/6b6e466c7d053be137127d38d924e296cc03f192))
* enhance client IP detection by checking multiple headers and validating IP addresses ([a584432](https://github.com/g0ldyy/comet/commit/a5844323d9a2fee4454fc88a4202a9c0e9d43519))
* enhance client IP detection by checking multiple headers and validating IP addresses ([b64b7f6](https://github.com/g0ldyy/comet/commit/b64b7f6e57f321fad8671a8412b7d919e36285d4))

## [2.41.0](https://github.com/g0ldyy/comet/compare/v2.40.0...v2.41.0) (2026-01-01)


### Features

* improve torrent batch processing with in-memory deduplication, PostgreSQL advisory locks, and enhance metadata handling ([ac061ac](https://github.com/g0ldyy/comet/commit/ac061acd71e3d68574f2cb22cdbc06fe705d78f8))
* improve torrent batch processing with in-memory deduplication, PostgreSQL advisory locks, and enhance metadata handling ([45ff7c1](https://github.com/g0ldyy/comet/commit/45ff7c173abb49a050c4ba14103fe6beae845d32))


### Bug Fixes

* add explicit BIGINT casting to timestamp comparisons in cache cleanup queries ([a52e5c0](https://github.com/g0ldyy/comet/commit/a52e5c06a57f6c7d36d2eeb6cf68779b9a6d92f5))

## [2.40.0](https://github.com/g0ldyy/comet/compare/v2.39.0...v2.40.0) (2025-12-31)


### Features

* conditionally apply digital release filter based on settings and remove redundant internal filter check ([489ed9b](https://github.com/g0ldyy/comet/commit/489ed9bdbe79aef571cbccb3b3742c4f9162c04e))
* implement digital release filtering for movies and series using TMDB ([172fb5b](https://github.com/g0ldyy/comet/commit/172fb5b7d454fabf34670daae150d177b439de4e))
* implement digital release filtering for movies and series using TMDB ([e05a0da](https://github.com/g0ldyy/comet/commit/e05a0da0665e8bd48a3a6cf7e696f012f74c28ca))
* improve performance with process pool executor and optimize torrent caching/database writes ([5666a7d](https://github.com/g0ldyy/comet/commit/5666a7d019dcb3039ca6d30fa4f6908d6c58dca4))
* improve performance with process pool executor and optimize torrent caching/database writes ([0ce6579](https://github.com/g0ldyy/comet/commit/0ce6579f3ff740cf2a91f5c9d6d9ee8eea6b8010))


### Bug Fixes

* update digital_release_cache.release_date column type to BIGINT ([7219330](https://github.com/g0ldyy/comet/commit/7219330bbe0fdefd292f7b90cce18b5286a2d756))


### Performance Improvements

* reduce RTN parsing chunk size to 20 and rework ProcessPoolExecutor system ([32e5127](https://github.com/g0ldyy/comet/commit/32e5127dd322b487948557c5b5a077e8548802e1))

## [2.39.0](https://github.com/g0ldyy/comet/compare/v2.38.0...v2.39.0) (2025-12-28)


### Features

* update image URLs from Imgur to ImgBB ([9f3649f](https://github.com/g0ldyy/comet/commit/9f3649fe9eb191fd35b03264421163204ca044b6))
* update image URLs from Imgur to ImgBB ([01db8a8](https://github.com/g0ldyy/comet/commit/01db8a8ceb941b03dffd1c355e1461bcf151d142))

## [2.38.0](https://github.com/g0ldyy/comet/compare/v2.37.0...v2.38.0) (2025-12-27)


### Features

* add ChillLink API endpoints and refactor stream description formatting logic ([ec2aa2b](https://github.com/g0ldyy/comet/commit/ec2aa2bb170b4bef70dee0ce452f94ab53d44f94))
* add ChillLink Protocol support to README ([8d8c484](https://github.com/g0ldyy/comet/commit/8d8c4844cfe24c874260e665945af1a2ffde88da))
* ChillLink Support ([b066b54](https://github.com/g0ldyy/comet/commit/b066b5495a86191bd08d366f8f198dc190a54974))

## [2.37.0](https://github.com/g0ldyy/comet/compare/v2.36.0...v2.37.0) (2025-12-26)


### Features

* add OpenAPI tags, summaries, and descriptions to API endpoints and parameters ([da1d5a7](https://github.com/g0ldyy/comet/commit/da1d5a7fbd6feb9e0e2c9a57d9456fae5744bddb))
* introduce a Python script for Comet instance uptime monitoring and Discord notifications ([b61f196](https://github.com/g0ldyy/comet/commit/b61f196d0b868ea53759363ef3452f622450a8b3))
* update comet scraper stream endpoint path ([0f3a96f](https://github.com/g0ldyy/comet/commit/0f3a96f5fdee2cbecdd48b3090504ea5b52a13f3))

## [2.36.0](https://github.com/g0ldyy/comet/compare/v2.35.0...v2.36.0) (2025-12-25)


### Features

* add default PostgreSQL service to Docker Compose, configure Comet to use it, and introduce SQLite concurrency warnings ([24fb50b](https://github.com/g0ldyy/comet/commit/24fb50bf77645dfbcf103df78e6e8f51d4855511))


### Bug Fixes

* correct PostgreSQL data volume mount path in docker-compose.yml ([7ae3788](https://github.com/g0ldyy/comet/commit/7ae3788a5cc81764297588b83dadb939aef6cac1))

## [2.35.0](https://github.com/g0ldyy/comet/compare/v2.34.0...v2.35.0) (2025-12-21)


### Features

* add API response to IMDB metadata retrieval error logs ([242e83a](https://github.com/g0ldyy/comet/commit/242e83a7e1bad5303fa6dfa8298d2fd9c2ec4f22))
* add robust error handling to the Prowlarr scraper ([95866a6](https://github.com/g0ldyy/comet/commit/95866a68abe0bf4e38c6eab0f6f6576ebbcd37e6))
* fetch indexer statuses from response ([bd51abf](https://github.com/g0ldyy/comet/commit/bd51abf40e9173fe3a366cfa9b5bcc0f91d77767))
* implement dynamic Prowlarr indexer management with initialization wait, update Jackett management, and add related settings and logging ([1152b42](https://github.com/g0ldyy/comet/commit/1152b42c2ee072f73607bb9480f83e0993751c1c))

## [2.34.0](https://github.com/g0ldyy/comet/compare/v2.33.0...v2.34.0) (2025-12-21)


### Features

* add YGGTORRENT_PASSKEY setting and update Yggtorrent scraper to parse info hash from page HTML and use passkey for tracker sources ([c442b0c](https://github.com/g0ldyy/comet/commit/c442b0c2691c2022e5da244010e9ae20621faaf2))
* add YGGTORRENT_PASSKEY setting and update Yggtorrent scraper to parse info hash from page HTML and use passkey for tracker sources ([6e44eb2](https://github.com/g0ldyy/comet/commit/6e44eb260dcf4354308b9e7c36f61c3c55593f38))
* dev to main ([2d2ded3](https://github.com/g0ldyy/comet/commit/2d2ded3f6a70b7efce0d134d281809cd8a5f7015))
* introduce IndexerManager service to dynamically update and manage Jackett and Prowlarr indexers ([beacb2a](https://github.com/g0ldyy/comet/commit/beacb2a966fc8969aa3070cbc9e0511073d55b6d))
* introduce IndexerManager service to dynamically update and manage Jackett and Prowlarr indexers ([8265cc5](https://github.com/g0ldyy/comet/commit/8265cc55aae8b796e18ee8b738e38d98b9a9990c))

## [2.33.0](https://github.com/g0ldyy/comet/compare/v2.32.0...v2.33.0) (2025-12-12)


### Features

* add DEBRID_CACHE_CHECK_RATIO setting and update availability ch… ([0173711](https://github.com/g0ldyy/comet/commit/0173711eeda1dd0cfe121e6b153c124ded8b31b9))
* add DEBRID_CACHE_CHECK_RATIO setting and update availability check logic ([3ecd35a](https://github.com/g0ldyy/comet/commit/3ecd35aa9fcfa12de7910a7fc05c4fae05e231f6))

## [2.32.0](https://github.com/g0ldyy/comet/compare/v2.31.0...v2.32.0) (2025-12-10)


### Features

* add catalog and magnet resolve timeouts to configuration ([eb3b5e8](https://github.com/g0ldyy/comet/commit/eb3b5e87c76729ba15c9110dd068256f3f57bbf6))
* add timeouts to Prowlarr API requests ([c0d7060](https://github.com/g0ldyy/comet/commit/c0d70607b28164f6e29ca7481bf17ea0fa126b64))
* decouple Jackett and Prowlarr scraper configurations from a generic indexer manager ([d17cb2d](https://github.com/g0ldyy/comet/commit/d17cb2d7b3b1c3d72f7b2df92a47f853d71a89fd))
* decouple Jackett and Prowlarr scraper configurations from a generic indexer manager ([460fad2](https://github.com/g0ldyy/comet/commit/460fad22b48ddcf465fd4f126a244bb1295bec1d))

## [Unreleased]

### Features

* add optional PostgreSQL read replica routing with transparent primary fallback
* add optional database-backed anime mapping cache with configurable refresh interval
* add `GUNICORN_PRELOAD_APP` setting to control whether workers inherit a preloaded app or initialize independently
* add `DATABASE_STARTUP_CLEANUP_INTERVAL` to throttle heavy startup cleanup sweeps across workers
* add `DISABLE_TORRENT_STREAMS` toggle with customizable placeholder stream metadata

## [2.31.0](https://github.com/g0ldyy/comet/compare/v2.30.0...v2.31.0) (2025-12-08)


### Features

* Enhance URL encoding by using `safe=''` in `quote` calls for playback and magnet URIs ([5664a86](https://github.com/g0ldyy/comet/commit/5664a86eeb9a991796ff0d60e3c79f8bdbb5532d))
* Enhance URL encoding by using `safe=''` in `quote` calls for playback and magnet URIs ([96a962f](https://github.com/g0ldyy/comet/commit/96a962faac555390bb9c461e345542324bdeb372))


### Bug Fixes

* double quote is better ([b9845dc](https://github.com/g0ldyy/comet/commit/b9845dc50d86d33ed690b23b401a6f0efbc8abb2))
* Update YGG domain URL ([a9b223f](https://github.com/g0ldyy/comet/commit/a9b223f1cd713744693d576c830e9618471b39d0))

## [2.30.0](https://github.com/g0ldyy/comet/compare/v2.29.0...v2.30.0) (2025-12-01)


### Features

* add live torrent cache TTL and update related settings ([6e2e420](https://github.com/g0ldyy/comet/commit/6e2e420d7379824d9b76ca603703f914349cf20b))
* enhance admin dashboard with dynamic tracker limit selection and optimize database metrics query ([5df4121](https://github.com/g0ldyy/comet/commit/5df412125bbd668661473a5512f1f9c0b22d6b93))
* fix imdb_id: in media_id ([607b5b3](https://github.com/g0ldyy/comet/commit/607b5b38a39b412d73f0e021048735de8dbf4d3f))


### Bug Fixes

* Strip 'imdb_id:' prefix from media ID in stream endpoint ([cc5b6a8](https://github.com/g0ldyy/comet/commit/cc5b6a8c95d88b5769cce0e2b27d2aa586bfcc07))

## [2.29.0](https://github.com/g0ldyy/comet/compare/v2.28.0...v2.29.0) (2025-11-28)


### Features

* add YGGTorrent scraper ([4a677bf](https://github.com/g0ldyy/comet/commit/4a677bf773f6e6351aee698b137b467bb23473ae))
* add YGGTorrent scraper ([2317e88](https://github.com/g0ldyy/comet/commit/2317e884e215c4f6367d05b19d77e1971fccf612))
* enhance playback functionality with media_id and aliases support ([eb9cbdc](https://github.com/g0ldyy/comet/commit/eb9cbdc680a491575457af6f8c8ac198533f8895))


### Bug Fixes

* handle missing results count in YGGTorrent scraper ([52c7718](https://github.com/g0ldyy/comet/commit/52c7718c0eaf6eeee2264022b268075257eb36f2))
* remove double-nested append ([4763307](https://github.com/g0ldyy/comet/commit/47633073f0aec345a3fad7b63a11bb2b9d9c98cf))

## [2.28.0](https://github.com/g0ldyy/comet/compare/v2.27.0...v2.28.0) (2025-11-24)


### Features

* fix Debridio scraper ([85c0aee](https://github.com/g0ldyy/comet/commit/85c0aee7dcf7c3a641bfaa079c4c7b5428e298bf))
* fix Debridio scraper ([3f93e1e](https://github.com/g0ldyy/comet/commit/3f93e1e783a6b491c5d3837b08eae4fded1cc396))

## [2.27.0](https://github.com/g0ldyy/comet/compare/v2.26.0...v2.27.0) (2025-11-24)


### Features

* add handling for empty torrent lists in filter_manager ([aef2549](https://github.com/g0ldyy/comet/commit/aef2549f3697707a433a4ff459703a27aa1623b4))
* add logging for scraper activity in TorrentManager ([6324112](https://github.com/g0ldyy/comet/commit/63241126a2980f54230412e4a63e904b9e0804a5))
* add metrics caching functionality and update settings for metrics cache TTL ([e3c269b](https://github.com/g0ldyy/comet/commit/e3c269ba050be48a834bbf9cbe0609415362cee7))

## [2.26.0](https://github.com/g0ldyy/comet/compare/v2.25.2...v2.26.0) (2025-11-23)


### Features

* bitmagnet scraper ([26e3e5e](https://github.com/g0ldyy/comet/commit/26e3e5eb757c515c6335ad72cdb4547d833b46d7))
* bitmagnet scraper ([3619fe5](https://github.com/g0ldyy/comet/commit/3619fe50849cab54f84070e7e79a78926d9fa03f))

## [2.25.2](https://github.com/g0ldyy/comet/compare/v2.25.1...v2.25.2) (2025-11-04)


### Bug Fixes

* use get method for safer access to seeders, tracker, and sources maps ([43e92a2](https://github.com/g0ldyy/comet/commit/43e92a2990dd3002a629e61b02d035b5910a315b))

## [2.25.1](https://github.com/g0ldyy/comet/compare/v2.25.0...v2.25.1) (2025-10-22)


### Bug Fixes

* update playback route to accept path parameters ([7658949](https://github.com/g0ldyy/comet/commit/76589492dc18d2cc9846643e0fe52d5281e687c2))

## [2.25.0](https://github.com/g0ldyy/comet/compare/v2.24.1...v2.25.0) (2025-10-19)


### Features

* remove normalize_title from metadata handling ([35d2171](https://github.com/g0ldyy/comet/commit/35d217168da40f3de20e2ea03c9d29d2c1512ace))


### Bug Fixes

* update title matching logic in StremThru and TorrentManager ([657748c](https://github.com/g0ldyy/comet/commit/657748c1e1973b1000ccbad8383760f84a6bed74))

## [2.24.1](https://github.com/g0ldyy/comet/compare/v2.24.0...v2.24.1) (2025-10-18)


### Bug Fixes

* rtn ranking model ([831a0cf](https://github.com/g0ldyy/comet/commit/831a0cfeabac55b77f91a0d242ea198d95dba917))

## [2.24.0](https://github.com/g0ldyy/comet/compare/v2.23.0...v2.24.0) (2025-10-05)


### Features

* Add NO_CACHE_HEADERS and update FileResponse usage in streaming ([52dce02](https://github.com/g0ldyy/comet/commit/52dce02b98417139b2afd775e5c23b8f51eb6f0a))

## [2.23.0](https://github.com/g0ldyy/comet/compare/v2.22.0...v2.23.0) (2025-09-09)


### Features

* Implement database import/export CLI tool ([52efbf1](https://github.com/g0ldyy/comet/commit/52efbf176941b92b9497eeaa33c0b90e1dc49f00))
* Implement database import/export CLI tool ([07a0885](https://github.com/g0ldyy/comet/commit/07a0885e6928b72707980cacc13f200aec7ec1c0))

## [2.22.0](https://github.com/g0ldyy/comet/compare/v2.21.0...v2.22.0) (2025-09-07)


### Features

* different modes for scrapers (live/background/both) ([b27e6ad](https://github.com/g0ldyy/comet/commit/b27e6adebaa4a311aa6a9144595bd94259cb9bfc))
* different modes for scrapers (live/background/both) ([278a130](https://github.com/g0ldyy/comet/commit/278a130d009d07cfadd363b6e9c4e125ee30279f))

## [2.21.0](https://github.com/g0ldyy/comet/compare/v2.20.0...v2.21.0) (2025-09-07)


### Features

* enhance Nyaa scraper with anime-only option and integrate anime mapping functionality ([f9c361c](https://github.com/g0ldyy/comet/commit/f9c361cc7d05f39911822b2b2780d86613ca74d7))
* enhance Nyaa scraper with anime-only option and integrate anime… ([a424f81](https://github.com/g0ldyy/comet/commit/a424f8181a418a04c28434e8258be53bf759cbda))
* enhance Nyaa scraper with Kitsu-only option ([1b5b734](https://github.com/g0ldyy/comet/commit/1b5b7344c426b08521de4074f7d297b6a12377a5))
* Nyaa scraper ([950db79](https://github.com/g0ldyy/comet/commit/950db795699c831e0e849d6d29e6d6cf5b0ae3a4))
* Nyaa scraper ([a96986c](https://github.com/g0ldyy/comet/commit/a96986cec7a2266bf07685e1a09a63b0646bfff4))


### Bug Fixes

* remove timeout parameter from anime mapping request ([c37e959](https://github.com/g0ldyy/comet/commit/c37e959bfd81910b3ee26f13123d7f6e11773cf5))

## [2.20.0](https://github.com/g0ldyy/comet/compare/v2.19.1...v2.20.0) (2025-09-06)


### Features

* add database indexes for performance optimization across multiple tables ([c806078](https://github.com/g0ldyy/comet/commit/c80607871ee9c0101bdb5a4757eaeb2cc3d10087))

## [2.19.1](https://github.com/g0ldyy/comet/compare/v2.19.0...v2.19.1) (2025-09-01)


### Bug Fixes

* aiostreams scraper ([9cefe9b](https://github.com/g0ldyy/comet/commit/9cefe9bf4919b14bde04c1a1732b39548a9e12a8))
* aiostreams scraper ([2962686](https://github.com/g0ldyy/comet/commit/29626864a2786e80a3b4b28a5a501a712baeb656))

## [2.19.0](https://github.com/g0ldyy/comet/compare/v2.18.0...v2.19.0) (2025-08-30)


### Features

* debridio scraper ([feaca56](https://github.com/g0ldyy/comet/commit/feaca56b36b0f8215c7a000261433aaf6d48fcbb))
* debridio scraper ([95c3879](https://github.com/g0ldyy/comet/commit/95c3879f34c23e7fdb4bcfee94326cb31aedea21))
* torbox scraper ([95c457b](https://github.com/g0ldyy/comet/commit/95c457b6ca0fdea074272df57b3d096b262f14b2))
* torbox scraper ([29fe925](https://github.com/g0ldyy/comet/commit/29fe9258d0e41a2391728a3fa4b975a40b97778a))


### Bug Fixes

* enhance logging in background scraper and handle unknown seeders in debridio scraper ([43805b3](https://github.com/g0ldyy/comet/commit/43805b32813e7b90f9c7cd326c601b092ec9a196))
* handle commas in size string for size_to_bytes function ([17f2796](https://github.com/g0ldyy/comet/commit/17f27966f884b6910d9d3a0366ee9ae6c1359385))
* initialize size to 0 in debridio scraper ([b592a09](https://github.com/g0ldyy/comet/commit/b592a093b47cdf98870ece97755fd2c369561fbc))
* remove unnecessary assignment in debridio scraper ([031582d](https://github.com/g0ldyy/comet/commit/031582d546533c871bfa4a0e7a3f21653c7315a8))

## [2.18.0](https://github.com/g0ldyy/comet/compare/v2.17.1...v2.18.0) (2025-08-30)


### Features

* jackettio scraper ([b24b545](https://github.com/g0ldyy/comet/commit/b24b5451d1514b3449124598d23cd0f94ac17d49))

## [2.17.1](https://github.com/g0ldyy/comet/compare/v2.17.0...v2.17.1) (2025-08-29)


### Bug Fixes

* Add check for missing infoHash in AIOStreams torrent data to prevent errors ([7b9975d](https://github.com/g0ldyy/comet/commit/7b9975d4376e643905d0b983543c5c85b90b69bb))

## [2.17.0](https://github.com/g0ldyy/comet/compare/v2.16.0...v2.17.0) (2025-08-28)


### Features

* AIOStreams scraper ([f77a720](https://github.com/g0ldyy/comet/commit/f77a7207139df6324f576ed3c77c010621f4ceba))
* AIOStreams scraper ([cc43614](https://github.com/g0ldyy/comet/commit/cc436148b4f5f29d7ed8eeffdf4174671db4de84))


### Bug Fixes

* Update AIOSTREAMS_URL type to be optional in AppSettings ([e150015](https://github.com/g0ldyy/comet/commit/e150015f24d3ee398d2050eb245f0e73290368b1))

## [2.16.0](https://github.com/g0ldyy/comet/compare/v2.15.1...v2.16.0) (2025-08-27)


### Features

* Add StremThru scraper ([13da997](https://github.com/g0ldyy/comet/commit/13da997bb2bbf68949c3f55fcc363605614c218c))
* background scraper ([7127a8e](https://github.com/g0ldyy/comet/commit/7127a8ebe144ac6bcff44afb0d307639a51cfdef))
* background scraper ([ed52431](https://github.com/g0ldyy/comet/commit/ed5243143c60eaa5bdab434c659ebe49e6f077bf))
* Implement associate_mediafusion_urls_passwords function and upd… ([37d080f](https://github.com/g0ldyy/comet/commit/37d080fea7ea815b7d66053104b1df8c6a56248d))
* Implement associate_mediafusion_urls_passwords function and update mediafusion URL handling in main and manager modules ([de638d1](https://github.com/g0ldyy/comet/commit/de638d1f38893db3314d7f879d7edc47b9aec8a2))
* Implement multi-instance scraping support for Comet, Zilean, Torrentio, and MediaFusion ([c0eeb0d](https://github.com/g0ldyy/comet/commit/c0eeb0da6e0c0866e813176fba181659c64626eb))
* Implement multi-instance scraping support for Comet, Zilean, Torrentio, and MediaFusion ([30b6cf3](https://github.com/g0ldyy/comet/commit/30b6cf3c005a7a086ba2c43f66a702d0180faf43))


### Bug Fixes

* Adjust total_bytes column type in bandwidth_stats table based on database type ([a8c7ca5](https://github.com/g0ldyy/comet/commit/a8c7ca5a29f7c22ea499541ded6bbc9f04d3402a))
* Rename scrape_attempts to scrape_failed_attempts in BackgroundScraperWorker and database schema for clarity ([cf19560](https://github.com/g0ldyy/comet/commit/cf1956011afc21bcd22da5c9230b77612cf901eb))
* Update background scraper interval validation to enforce a minimum of 5 minutes ([f01f124](https://github.com/g0ldyy/comet/commit/f01f1245cb46801bc9965ee3a12d101ee2a8b728))
* Update log_scraper_error to include URL in error messages for better debugging ([321fd62](https://github.com/g0ldyy/comet/commit/321fd624befb117c1e7288a5895134c9c9d27dfb))

## [2.15.1](https://github.com/g0ldyy/comet/compare/v2.15.0...v2.15.1) (2025-08-25)


### Bug Fixes

* Add single announce URL to announce list ([789dcff](https://github.com/g0ldyy/comet/commit/789dcff4c4f93774d8784321dcd6b1703112a6d6))
* update manifest name to exclude 'TORRENT' from debrid extension ([d916fe0](https://github.com/g0ldyy/comet/commit/d916fe03e0ec986ceb2960085f2776f4d809dbcf))

## [2.15.0](https://github.com/g0ldyy/comet/compare/v2.14.0...v2.15.0) (2025-08-24)


### Features

* oupsie ([2678e9c](https://github.com/g0ldyy/comet/commit/2678e9c8046af38ef7b1bdae9457906636fd0c14))

## [2.14.0](https://github.com/g0ldyy/comet/compare/v2.13.1...v2.14.0) (2025-08-24)


### Features

* switch admin session management from RAM to DB ([abf80fd](https://github.com/g0ldyy/comet/commit/abf80fd260d95b079ab053793d32f67641887b91))

## [2.13.1](https://github.com/g0ldyy/comet/compare/v2.13.0...v2.13.1) (2025-08-24)


### Bug Fixes

* private trackers debrid download (now need stremthru to fix it) ([506ee5b](https://github.com/g0ldyy/comet/commit/506ee5bff22d7cea9e1856ee33d6f9259daab1b6))

## [2.13.0](https://github.com/g0ldyy/comet/compare/v2.12.0...v2.13.0) (2025-08-24)


### Features

* add metrics to dashboard ([37f3e86](https://github.com/g0ldyy/comet/commit/37f3e8664221e0f176817836e6e76c6cc284a5aa))
* add support for Debrider debrid service ([e16f817](https://github.com/g0ldyy/comet/commit/e16f817a99bd9a23066a97a8b5768540b750c261))


### Bug Fixes

* comet scraper title parsing ([b51258a](https://github.com/g0ldyy/comet/commit/b51258aeaab80e45f28416bdb4110898a2ebe50e))
* ensure PostgreSQL compatibility in admin API metrics ([38196c2](https://github.com/g0ldyy/comet/commit/38196c21a0c94b56da0408179b5fed715e38243a))

## [2.12.0](https://github.com/g0ldyy/comet/compare/v2.11.0...v2.12.0) (2025-08-24)


### Features

* bandwidth monitoring ([5f7c033](https://github.com/g0ldyy/comet/commit/5f7c03306906cabc71f1c523430868cb15443b97))

## [2.11.0](https://github.com/g0ldyy/comet/compare/v2.10.0...v2.11.0) (2025-08-23)


### Features

* implement new admin dashboard ([dfe00a2](https://github.com/g0ldyy/comet/commit/dfe00a280c37686df9cc0ad5a1bc9e0d32fcc125))


### Bug Fixes

* use torrent name as media_id instead of torrent hash ([740140e](https://github.com/g0ldyy/comet/commit/740140eb2748fa1ccde70c1fea39f6add5d03c6f))

## [2.10.0](https://github.com/g0ldyy/comet/compare/v2.9.0...v2.10.0) (2025-06-12)


### Features

* 4300% performance improvement ([4a91bb7](https://github.com/g0ldyy/comet/commit/4a91bb7fc7736f1de03706cf8381b2d0a209bfa4))


### Bug Fixes

* remove unused langage ([9516b1f](https://github.com/g0ldyy/comet/commit/9516b1fd05cc38c278cd244ef711d716c84eaacb))

## [2.9.0](https://github.com/g0ldyy/comet/compare/v2.8.0...v2.9.0) (2025-06-11)


### Features

* enhance result formatting ([afc8d6f](https://github.com/g0ldyy/comet/commit/afc8d6f3b177538d1044b244a8c749e5060476fc))

## [2.8.0](https://github.com/g0ldyy/comet/compare/v2.7.0...v2.8.0) (2025-06-10)


### Features

* add MediaFusion live search configuration ([f843929](https://github.com/g0ldyy/comet/commit/f8439296f517de8ac2a4de6e0d0c2580213c4a57))

## [2.7.0](https://github.com/g0ldyy/comet/compare/v2.6.1...v2.7.0) (2025-06-10)


### Features

* mediafusion live search is not enabled by default?? ([91c13df](https://github.com/g0ldyy/comet/commit/91c13df7d4dbbd780053aa1396aab3b45425c6b3))

## [2.6.1](https://github.com/g0ldyy/comet/compare/v2.6.0...v2.6.1) (2025-06-08)


### Bug Fixes

* update mediafusion config ([73f5fc4](https://github.com/g0ldyy/comet/commit/73f5fc40093460ef41dbb244fd1bf6a40d365ef2))

## [2.6.0](https://github.com/g0ldyy/comet/compare/v2.5.1...v2.6.0) (2025-06-07)


### Features

* add MediaFusion API password support ([f15bbf1](https://github.com/g0ldyy/comet/commit/f15bbf1d54682edfa8e915ab31c526e2e6c6725b))

## [2.5.1](https://github.com/g0ldyy/comet/compare/v2.5.0...v2.5.1) (2025-06-07)


### Bug Fixes

* more info in env-sample ([bb85887](https://github.com/g0ldyy/comet/commit/bb85887df64291a895a8425ab26765271cb39cb2))
* postgres wrong type ([a90fee8](https://github.com/g0ldyy/comet/commit/a90fee8dbfc079a24ceaf666f751af86f07580e3))

## [2.5.0](https://github.com/g0ldyy/comet/compare/v2.4.4...v2.5.0) (2025-06-05)


### Features

* lock system for comet clusters ([dd2171b](https://github.com/g0ldyy/comet/commit/dd2171bdcda8e3b7a4551882e23c6a7a15fcc218))


### Bug Fixes

* double requests for comet, torrentio and mediafusion ([5e95efc](https://github.com/g0ldyy/comet/commit/5e95efc2ca0c99ec09bb6d1b0ac280e64d9387ac))
* explicitely block tmdb requests ([7c7aaa7](https://github.com/g0ldyy/comet/commit/7c7aaa71852789233e72f5d3ad759e19f92a314a))
* gunicorn worker count ([2d1b2b9](https://github.com/g0ldyy/comet/commit/2d1b2b91bf23b876ed37b18f97e063ae165087d9))

## [2.4.4](https://github.com/g0ldyy/comet/compare/v2.4.3...v2.4.4) (2025-05-03)


### Bug Fixes

* add URL encoding and decoding for stream and torrent names ([b17d727](https://github.com/g0ldyy/comet/commit/b17d72789fc621275dbf344bb132fba7232e4b62))
* parse media_id for kitsu movies correctly ([f97a436](https://github.com/g0ldyy/comet/commit/f97a436f0b237d437fd405fdcc9140d7142653d4))
* pin rank-torrent-name to last working version ([2d4ea89](https://github.com/g0ldyy/comet/commit/2d4ea89f6757c768b8b8038bd9ffc45aea314bf2))
* pin rank-torrent-name to last working version ([ec06722](https://github.com/g0ldyy/comet/commit/ec06722fb3e841362903f2ba23ef823b887195b4))
* revert unwanted change in docker-compose.yml ([03e385d](https://github.com/g0ldyy/comet/commit/03e385d1350b081e9b6b108dbe6abca374414d47))
* revert unwanted change in docker-compose.yml ([3e1a7a6](https://github.com/g0ldyy/comet/commit/3e1a7a62c15dfb495d2b16c6fc2744042841f76d))
* support kitsu special episodes without episode number ([ed2fcf4](https://github.com/g0ldyy/comet/commit/ed2fcf424810b0d461a905b0b79a6bf6181e4520))
* support kitsu special episodes without episode number ([40fcf8e](https://github.com/g0ldyy/comet/commit/40fcf8e410c0abbee1814301b97822e0b5c3e75d))
* update models to be compatible with latest RTN ([7fb18b2](https://github.com/g0ldyy/comet/commit/7fb18b2383f3c24c40525b70544fd6ab8fc7803c))
* update models to be compatible with latest RTN ([3967c42](https://github.com/g0ldyy/comet/commit/3967c42bd095a7a297389adafc86025bb5e413bb))
* update RTN dep to 1.8.2 ([a9350ea](https://github.com/g0ldyy/comet/commit/a9350ea8aa3e82bbd2b40efaba494915d55c99ae))
* update RTN dep to 1.8.2 ([11ca645](https://github.com/g0ldyy/comet/commit/11ca6459e9c2f9b53fed834629b31f36eb5d9764))
* update RTN dep to 1.8.3 ([3b1759c](https://github.com/g0ldyy/comet/commit/3b1759cac969e2df222619de93d3149bdc9bb81e))
* update RTN dep to 1.8.3 ([7c995e5](https://github.com/g0ldyy/comet/commit/7c995e535ed882fd7cdd9b33bfd3abff00ecd6d9))

## [2.4.3](https://github.com/g0ldyy/comet/compare/v2.4.2...v2.4.3) (2025-03-30)


### Bug Fixes

* oupsie ([2c3b08f](https://github.com/g0ldyy/comet/commit/2c3b08f857e06748df06834da08e4db135165e66))

## [2.4.2](https://github.com/g0ldyy/comet/compare/v2.4.1...v2.4.2) (2025-03-30)


### Bug Fixes

* info_hash parsing ([a521a04](https://github.com/g0ldyy/comet/commit/a521a04a4629c673e5c1f880b2545f33a155549b))

## [2.4.1](https://github.com/g0ldyy/comet/compare/v2.4.0...v2.4.1) (2025-03-15)


### Bug Fixes

* pikpak typo ([33c7fbd](https://github.com/g0ldyy/comet/commit/33c7fbd8a25ecca6fa2042551ca231c700aa3ad0))

## [2.4.0](https://github.com/g0ldyy/comet/compare/v2.3.0...v2.4.0) (2025-03-13)


### Features

* better file choosing logic ([ad13278](https://github.com/g0ldyy/comet/commit/ad132781df58bf6eefef8c4bbe4ad32f7f874519))

## [2.3.0](https://github.com/g0ldyy/comet/compare/v2.2.0...v2.3.0) (2025-03-11)


### Features

* add comet scraper ([a11a65e](https://github.com/g0ldyy/comet/commit/a11a65e37fd1a37cd119df7e17f4d8fcf93a6ab7))
* fix sqlite journal_mode ([f052167](https://github.com/g0ldyy/comet/commit/f052167a41fb137c93b10a5843b2ec02897f26fe))

## [2.2.0](https://github.com/g0ldyy/comet/compare/v2.1.0...v2.2.0) (2025-03-04)


### Features

* ios video players support (ex: infuse) ([acc8112](https://github.com/g0ldyy/comet/commit/acc8112a2a5966ac62b2b2c54105425680b98ccf))

## [2.1.0](https://github.com/g0ldyy/comet/compare/v2.0.3...v2.1.0) (2025-03-03)


### Features

* choose max size file if no file found ([3ed5057](https://github.com/g0ldyy/comet/commit/3ed5057b0856ba95ab823a66ae0fdc64882c16bb))

## [2.0.3](https://github.com/g0ldyy/comet/compare/v2.0.2...v2.0.3) (2025-03-01)


### Bug Fixes

* correct addon version ([176899d](https://github.com/g0ldyy/comet/commit/176899d4b5e618013e9d15f00c03547d28f6df2c))

## [2.0.2](https://github.com/g0ldyy/comet/compare/v2.0.1...v2.0.2) (2025-02-28)


### Bug Fixes

* torrent queue ([37b12af](https://github.com/g0ldyy/comet/commit/37b12af956b9eb709a3b785c6ee1e81edcf3b16d))

## [2.0.1](https://github.com/g0ldyy/comet/compare/v2.0.0...v2.0.1) (2025-02-28)


### Bug Fixes

* postgres transaction ([936e1a2](https://github.com/g0ldyy/comet/commit/936e1a2c597c35878957d6238581972e00c02b3b))

## [2.0.0](https://github.com/g0ldyy/comet/compare/v1.54.0...v2.0.0) (2025-02-28)


### ⚠ BREAKING CHANGES

* 2025 rewrite complete

### Code Refactoring

* 2025 rewrite complete ([e62e7c4](https://github.com/g0ldyy/comet/commit/e62e7c4b1eed37e7950b3effaf6b38ddd47e9c6a))

## [1.54.0](https://github.com/g0ldyy/comet/compare/v1.53.0...v1.54.0) (2025-02-28)


### Features

* 🏎️💨 ([3850b04](https://github.com/g0ldyy/comet/commit/3850b044a9e5ec7d4f124bf7425fe18c667e427d))
* 🏎️💨 ([833a7ae](https://github.com/g0ldyy/comet/commit/833a7aea3333497555d31824a15fdd632b0e5f52))
* allow_english_in_languages in webui ([886ec29](https://github.com/g0ldyy/comet/commit/886ec29dedcde8895c424a5297cc98f4e3ab116c))
* availability cache logic finished ([a2f1ac1](https://github.com/g0ldyy/comet/commit/a2f1ac14b8477ce1a9594575dd5e3358d3541a0d))
* better cache ttl ([9dacb30](https://github.com/g0ldyy/comet/commit/9dacb309bcd50a6111e983c4311ece709e0a3909))
* better db - everything ([7f02c5e](https://github.com/g0ldyy/comet/commit/7f02c5ee511cbacaf37945242a3c5b8ffc1279e7))
* better everything ([fa1ce1a](https://github.com/g0ldyy/comet/commit/fa1ce1ae0f179511888298d030dcb8ca7e1bdee1))
* better file choice when playback ([b17c7c9](https://github.com/g0ldyy/comet/commit/b17c7c905e39a7d5df2981aa9f26b1d549f55068))
* better kitsu ([f0d997e](https://github.com/g0ldyy/comet/commit/f0d997e137942c74b001a6ba34511a515bb1268e))
* better kitsu aliases ([b676895](https://github.com/g0ldyy/comet/commit/b676895da758bd3ab512d26d949e2a1283f8e409))
* better logging ([19a7e9e](https://github.com/g0ldyy/comet/commit/19a7e9ef9526717226a029a4e0e9bc757602fbde))
* caching operations now in background ([2726a50](https://github.com/g0ldyy/comet/commit/2726a507548fe59413fc7a187aa30ae1967dff22))
* code cleaning ([cfe7126](https://github.com/g0ldyy/comet/commit/cfe7126524f2690ef38eb329e2591f7ca2ef2918))
* custom rtn settings ([c640068](https://github.com/g0ldyy/comet/commit/c640068975b831e516e25db075a6c521a36e8744))
* dashboard endpoint to see proxy active connections ([6c14253](https://github.com/g0ldyy/comet/commit/6c14253ee7fbc5f23e9110a15217cdb95cf038d7))
* database migration system ([29a27ad](https://github.com/g0ldyy/comet/commit/29a27adc2894c44046e144d0e11c8cba6532e782))
* docker images for rewrite branch too yeee ([0d89333](https://github.com/g0ldyy/comet/commit/0d893338aa76426ffa9d871182e5d4107aaca8b4))
* FORCE_STREMTHRU ([52f1a45](https://github.com/g0ldyy/comet/commit/52f1a4502c21dd1a0a3edfcf1443f67c0e374b5d))
* full direct torrent support ([013e294](https://github.com/g0ldyy/comet/commit/013e294b804fc8b515ad7c0e50865e685e1e8453))
* get file index from debrid services ([8308ba8](https://github.com/g0ldyy/comet/commit/8308ba8870a5ad0abc0aad29a9f74722f5b2cdab))
* guvicorn + gevent ([c514cbb](https://github.com/g0ldyy/comet/commit/c514cbb0e16ec1942386437bbf20c4c1e3b7c38f))
* guvicorn support ([6304079](https://github.com/g0ldyy/comet/commit/63040796009895ed30d5f3e17644ad4091b29d36))
* hide debrid stream proxy password input if disabled ([7074893](https://github.com/g0ldyy/comet/commit/70748933e26f275e350d93fe15f39fa6c0861de0))
* improved file index update logic ([f5c4524](https://github.com/g0ldyy/comet/commit/f5c45244355c33abaf5ac82732f7f639f7c6b4c2))
* improved logger ([58f6b44](https://github.com/g0ldyy/comet/commit/58f6b44a4a30ae2bdac8924fc7ffdd50a74a4272))
* improved torrent file index finder logic ([f05e03c](https://github.com/g0ldyy/comet/commit/f05e03c95a74d7559f54f69bce68e118c88df89d))
* init readme for rewrite ([322c063](https://github.com/g0ldyy/comet/commit/322c063e002ab0149d11d4d3b2362bb1f323df3e))
* ip limit for stream proxy ([dab5166](https://github.com/g0ldyy/comet/commit/dab5166f764aaeee34240c96d3982b9d68397904))
* more info ([5e6c265](https://github.com/g0ldyy/comet/commit/5e6c2656d3a4c8f1147925023f163c3d45b6f796))
* offcloud support ([a318450](https://github.com/g0ldyy/comet/commit/a3184507dfe665571502ee5c00c3d6517bfa9b0e))
* postgre support ([a98459e](https://github.com/g0ldyy/comet/commit/a98459eb42e76212c5a9bfcd9f1e78befc385dbb))
* prowlarr/jackett support ([490f0fa](https://github.com/g0ldyy/comet/commit/490f0fa0783eb8a53ab0c915d70823d7a9d2e7c2))
* qol ([5132210](https://github.com/g0ldyy/comet/commit/51322104501bcb25daf51d3d82a37f4aaa7ebcd7))
* remove samples ([161351f](https://github.com/g0ldyy/comet/commit/161351fce0ffbf4681012034c4eadcd349f78a2b))
* remove_unknown_languages in webui ([96c2fd3](https://github.com/g0ldyy/comet/commit/96c2fd339809e7b39ad4cd5cbf9df5e644154ae0))
* set download torrent files to false by default ([ddac1d0](https://github.com/g0ldyy/comet/commit/ddac1d0e0a14629dcc8eb3e9788f6a4a2b5b395b))
* stremthru ([1b5758b](https://github.com/g0ldyy/comet/commit/1b5758b610b9c82a3a28b3e23d4181addb8bc084))
* stremthru improvements ([4ac90aa](https://github.com/g0ldyy/comet/commit/4ac90aa23b93e73ca3776cce9f0e0551b33409d2))
* update readme ([c151bb4](https://github.com/g0ldyy/comet/commit/c151bb446d826e84f3c3313084de4cee0ffafd50))
* webui config ([31144be](https://github.com/g0ldyy/comet/commit/31144bef6ddbed102c670abcc16413aa79911c36))


### Bug Fixes

* back to uvicorn workers ([2d96b46](https://github.com/g0ldyy/comet/commit/2d96b463ab0b512eecbd06c3477b5dab80855f35))
* change STREMTHRU_DEFAULT_URL ([1ed3567](https://github.com/g0ldyy/comet/commit/1ed35676cd4fdefd5c0d902bcb9ca1deaa64daa2))
* default config + prevent old configs ([8d118c9](https://github.com/g0ldyy/comet/commit/8d118c90a4a3f9269d6d709d449cacd0dc57892d))
* dependencies ([38deff7](https://github.com/g0ldyy/comet/commit/38deff7172d932801494f3306b129e9c5b5c8f68))
* docker compose ([9d5f21f](https://github.com/g0ldyy/comet/commit/9d5f21f6085d0da77ff7d937ed9317a768499486))
* dockerfile ([f5bdca1](https://github.com/g0ldyy/comet/commit/f5bdca136a25ba1650b6a2f99b24bdd28c38c64a))
* duplicated title aliases ([2038919](https://github.com/g0ldyy/comet/commit/20389193b1acca625c186f94855ac28649df8b42))
* gevent monkey patch ([1f9c260](https://github.com/g0ldyy/comet/commit/1f9c2609144642de96cb54684c8f07d6b28ea328))
* kitsu media_id invalid type ([d84744b](https://github.com/g0ldyy/comet/commit/d84744be79fdecedb308163076969bf72d67bb69))
* kitsu movies ([58517d5](https://github.com/g0ldyy/comet/commit/58517d509c3cd64ce5d693737470c6d6a63772b5))
* kitsu with postgres ([cf69538](https://github.com/g0ldyy/comet/commit/cf69538b7df1d0807c8838cd57f8d0771434b4f7))
* last try with gevent ([0b71505](https://github.com/g0ldyy/comet/commit/0b7150502bb2fff5d08eaa881e932d9b61930077))
* mb ([fba81f4](https://github.com/g0ldyy/comet/commit/fba81f457c38913d4909e24aa6fc1fc8dc317ea7))
* mediafusion ([5b17dc6](https://github.com/g0ldyy/comet/commit/5b17dc6be41abe50abd94cc832c31c0a3771c97d))
* only get file index and size from torrent_file_indexes if in direct torrent mode ([c98c3d4](https://github.com/g0ldyy/comet/commit/c98c3d450e10a49885cf028aadb3668a55cb7c1f))
* oupsie ([7db04de](https://github.com/g0ldyy/comet/commit/7db04de843b5b4f8568e8c12f1066c64b573bd49))
* playback proxy with postgre ([8620bce](https://github.com/g0ldyy/comet/commit/8620bce8b4740f52e0a01821c01d04cbdce36922))
* postgre stupid ([5c4fa67](https://github.com/g0ldyy/comet/commit/5c4fa67bedb0d854b0997ae98f0f13710d65f96a))
* probably not it yet ([a3c4176](https://github.com/g0ldyy/comet/commit/a3c4176cd2851afaf49c92c4ee9d7344968d34ff))
* remove torrent as a debrid service ([225a2fa](https://github.com/g0ldyy/comet/commit/225a2fae5fd36919330f32ffe1475f72422935eb))
* rtn default settings ([5347499](https://github.com/g0ldyy/comet/commit/5347499d21bade1e38446a9918f8e2b72ebd99e8))
* stremthru ([5264244](https://github.com/g0ldyy/comet/commit/52642449288444ae649e1409aa33ed593c45b0a2))
* stremthru token build ([81a9c6d](https://github.com/g0ldyy/comet/commit/81a9c6dbd9df5a98dfd9fec76e6da5585978d352))
* stupid ([5148997](https://github.com/g0ldyy/comet/commit/51489971e6eb009114db3aa91b251013a422d9fe))
* update gitignore ([2d9ffcc](https://github.com/g0ldyy/comet/commit/2d9ffcc6db85d49ec3e40477675ebedaa9dd4f60))
* update sample ([3de418f](https://github.com/g0ldyy/comet/commit/3de418f19ff1be77d898e2800877d6b83b0cb8b1))
* wrong rtn setting model ([2240aed](https://github.com/g0ldyy/comet/commit/2240aed22abadcce5f36a97af79a2bf6471017fa))
* wut ([c020f2c](https://github.com/g0ldyy/comet/commit/c020f2cd8d9909b67f0b424d7787e584480ac43e))

## [1.51.0](https://github.com/g0ldyy/comet/compare/v1.50.1...v1.51.0) (2024-11-28)


### Features

* remove the sketchy one ([d692201](https://github.com/g0ldyy/comet/commit/d6922010290ebe62b56bc782859e8ad07e14daeb))

## [1.50.1](https://github.com/g0ldyy/comet/compare/v1.50.0...v1.50.1) (2024-11-27)


### Bug Fixes

* easydebrid tv shows ([e40163f](https://github.com/g0ldyy/comet/commit/e40163f509fdbf41b914a136d6eea5ec2bacb891))
* shit fix before easydebrid new api ([979c85d](https://github.com/g0ldyy/comet/commit/979c85d751d7fd8978559f9843abc5780a30e83b))

## [1.50.0](https://github.com/g0ldyy/comet/compare/v1.49.0...v1.50.0) (2024-11-27)


### Features

* easydebrid support ([15b22e7](https://github.com/g0ldyy/comet/commit/15b22e75aa4c4e0d32e3dbc8d76cd1c45eeedde6))
* revert back to old jackett queries ([d721246](https://github.com/g0ldyy/comet/commit/d7212465f18554f6d9e41b2faa94026a8eb10208))

## [1.49.0](https://github.com/g0ldyy/comet/compare/v1.48.1...v1.49.0) (2024-11-26)


### Features

* torbox speed improvement + torbox proxy stream fix ([30ecbce](https://github.com/g0ldyy/comet/commit/30ecbcec4560510b260c43ec2ed04a82e724c743))

## [1.48.1](https://github.com/g0ldyy/comet/compare/v1.48.0...v1.48.1) (2024-11-26)


### Bug Fixes

* we don't want to spam debrid-link shit ([f9c0ec0](https://github.com/g0ldyy/comet/commit/f9c0ec0a84b6fa9a1e791990b690b6f8ffe0922d))

## [1.48.0](https://github.com/g0ldyy/comet/compare/v1.47.0...v1.48.0) (2024-11-26)


### Features

* GG Debrid-Link, restrictions defeated 🤓☝️ ([49cd90b](https://github.com/g0ldyy/comet/commit/49cd90bd0092fd25fe866c3a9120e966a855cd76))

## [1.47.0](https://github.com/g0ldyy/comet/compare/v1.46.0...v1.47.0) (2024-11-26)


### Features

* random addon id ([1b6a80b](https://github.com/g0ldyy/comet/commit/1b6a80bbe5800771774adeb469d861919dc3f70d))

## [1.46.0](https://github.com/g0ldyy/comet/compare/v1.45.0...v1.46.0) (2024-11-24)


### Features

* remove the need to have a pro torbox acc ([253bfea](https://github.com/g0ldyy/comet/commit/253bfeae4f37595bd59263cde8e1eca26d2fa8e2))

## [1.45.0](https://github.com/g0ldyy/comet/compare/v1.44.1...v1.45.0) (2024-11-24)


### Features

* debrid-link is now default ([253eadc](https://github.com/g0ldyy/comet/commit/253eadc322e8138f61b638567aaaed9d3c23b85a))

## [1.44.1](https://github.com/g0ldyy/comet/compare/v1.44.0...v1.44.1) (2024-11-24)


### Bug Fixes

* postgresql support ([b61480b](https://github.com/g0ldyy/comet/commit/b61480b3091ed62b23e5a42be8a5cc7ad3679692))

## [1.44.0](https://github.com/g0ldyy/comet/compare/v1.43.4...v1.44.0) (2024-11-23)


### Features

* new db structure ([8c3a81a](https://github.com/g0ldyy/comet/commit/8c3a81a61fb9f0195c7edd67efcd577dfc2cf458))


### Bug Fixes

* db caching issue ([8122689](https://github.com/g0ldyy/comet/commit/812268971d0dcd030df36b004083ee8a922fda5d))

## [1.43.4](https://github.com/g0ldyy/comet/compare/v1.43.3...v1.43.4) (2024-11-21)


### Bug Fixes

* correctly handle unsuccessful api calls ([c14195e](https://github.com/g0ldyy/comet/commit/c14195e6027e9fa70cd16245b5bd916b4806901e))
* redundant get_files on duplicate hashes ([854846f](https://github.com/g0ldyy/comet/commit/854846fb4bff4cff1df9159b2dc4997c35443857))

## [1.43.3](https://github.com/g0ldyy/comet/compare/v1.43.2...v1.43.3) (2024-11-20)


### Bug Fixes

* a ([949b83b](https://github.com/g0ldyy/comet/commit/949b83b5f5c53dea7231cd57d829a85f8da533d5))
* b ([665e840](https://github.com/g0ldyy/comet/commit/665e8407e3fcab3fe3ccb84b60b12c733fc609e8))
* remove duplicate file extension because wtf ([f39a684](https://github.com/g0ldyy/comet/commit/f39a684f10668a1e2e2d9326bcf3ff5a54e71ac9))
* year_end for kitsu ([f1f4224](https://github.com/g0ldyy/comet/commit/f1f422440df81cf0b02b469ff37ce9b65a963bea))

## [1.43.2](https://github.com/g0ldyy/comet/compare/v1.43.1...v1.43.2) (2024-11-19)


### Bug Fixes

* adult content filter ([f5afeff](https://github.com/g0ldyy/comet/commit/f5afeff6af73098bb11d08988f368d057764bb4c))

## [1.43.1](https://github.com/g0ldyy/comet/compare/v1.43.0...v1.43.1) (2024-11-19)


### Bug Fixes

* maxSize reset to 0 ([05ea53b](https://github.com/g0ldyy/comet/commit/05ea53b5b1cbef18b757afacc0c4806d3d90aa51))

## [1.43.0](https://github.com/g0ldyy/comet/compare/v1.42.0...v1.43.0) (2024-11-19)


### Features

* ability for the user to toggle trash removal ([89fa2d0](https://github.com/g0ldyy/comet/commit/89fa2d024dca6c7ccab0438893f6a5e45c475d07))

## [1.42.0](https://github.com/g0ldyy/comet/compare/v1.41.1...v1.42.0) (2024-11-19)


### Features

* improve year check ([ede4e98](https://github.com/g0ldyy/comet/commit/ede4e98a40839e47de9e201f20ed2619cc03966b))


### Bug Fixes

* adult content filter fixed ([ad415dc](https://github.com/g0ldyy/comet/commit/ad415dc9b8ae5276e26a57984cd9a0302568cdf4))

## [1.41.1](https://github.com/g0ldyy/comet/compare/v1.41.0...v1.41.1) (2024-11-19)


### Bug Fixes

* improve torrentio/mediafusion parsing ([1318772](https://github.com/g0ldyy/comet/commit/13187728d691b02b60820ba027b200272693c2d8))

## [1.41.0](https://github.com/g0ldyy/comet/compare/v1.40.1...v1.41.0) (2024-11-19)


### Features

* title match check aliases system ([36323ac](https://github.com/g0ldyy/comet/commit/36323ac028dc4833c57200100f4e36a170bdb7c2))

## [1.40.1](https://github.com/g0ldyy/comet/compare/v1.40.0...v1.40.1) (2024-11-18)


### Bug Fixes

* oupsie ([43b7e37](https://github.com/g0ldyy/comet/commit/43b7e37db6f31e47bba15ef9f7b41b856c9ff1b7))

## [1.40.0](https://github.com/g0ldyy/comet/compare/v1.39.0...v1.40.0) (2024-11-18)


### Features

* reverse order results ([15098f3](https://github.com/g0ldyy/comet/commit/15098f379e0e2012e229d275095afe618304567f))

## [1.39.0](https://github.com/g0ldyy/comet/compare/v1.38.0...v1.39.0) (2024-11-18)


### Features

* shit filter and adult content check ([fe3ddb6](https://github.com/g0ldyy/comet/commit/fe3ddb6702aac23de8e3ad6b51c56899f3f3ea24))


### Bug Fixes

* webui config ([0487af5](https://github.com/g0ldyy/comet/commit/0487af542e3f03a2d95719b4cb7243bc21f8e119))

## [1.38.0](https://github.com/g0ldyy/comet/compare/v1.37.0...v1.38.0) (2024-11-17)


### Features

* ability to disable max connection ([9ebcb71](https://github.com/g0ldyy/comet/commit/9ebcb71ef1cd48ed9ac3079838cf4b2da1575cc2))
* ability to disable max connection [#2](https://github.com/g0ldyy/comet/issues/2) ([412699f](https://github.com/g0ldyy/comet/commit/412699fb55a77069ef17c0e5f826d87cca2d7c8d))
* ability to disable max connection [#3](https://github.com/g0ldyy/comet/issues/3) ([05fe062](https://github.com/g0ldyy/comet/commit/05fe06294b0471a2cd8e7cfb3c3b96a9863e1f6a))
* ability to disable max connection [#4](https://github.com/g0ldyy/comet/issues/4) ([a18dceb](https://github.com/g0ldyy/comet/commit/a18dceb01c49f4a0fee50b3afda11b5008da3085))
* add max results per resolution configuration option ([fefc260](https://github.com/g0ldyy/comet/commit/fefc260b083e46ac062ede04abd694c9841f7c42))
* add search term ([fdb62ab](https://github.com/g0ldyy/comet/commit/fdb62ab60adb046e92279f7f5c2578d9b54fd8bc))
* language unknown support ([6f7c79f](https://github.com/g0ldyy/comet/commit/6f7c79f91dffabd28bcce667be6c72837711414b))


### Bug Fixes

* fix 2160p issue ([5ca4aa5](https://github.com/g0ldyy/comet/commit/5ca4aa59a0b704f77ab45190f2e03fc83e75023e))

## [1.37.0](https://github.com/g0ldyy/comet/compare/v1.36.1...v1.37.0) (2024-11-15)


### Features

* different error message for no cache direct torrent ([10318de](https://github.com/g0ldyy/comet/commit/10318decebf4989fe6416181fceb03ef10b0186f))
* error message in case its not configured ([c572274](https://github.com/g0ldyy/comet/commit/c572274fc9fc520c85d93bff407fcd033c0f0937))


### Bug Fixes

* index error ([e94c8e2](https://github.com/g0ldyy/comet/commit/e94c8e278a610f7ee88d513958407d389d2c0515))

## [1.36.1](https://github.com/g0ldyy/comet/compare/v1.36.0...v1.36.1) (2024-11-14)


### Bug Fixes

* something ([5b5a9ce](https://github.com/g0ldyy/comet/commit/5b5a9ce3c341a9e67c29cd6a623640aa3fd7d0cf))

## [1.36.0](https://github.com/g0ldyy/comet/compare/v1.35.3...v1.36.0) (2024-11-14)


### Features

* elfhosted debugging ([85912a2](https://github.com/g0ldyy/comet/commit/85912a273b12a5a3fae1b7a4d5546022d304b4de))

## [1.35.3](https://github.com/g0ldyy/comet/compare/v1.35.2...v1.35.3) (2024-11-14)


### Bug Fixes

* add trackers to results ([9295fd2](https://github.com/g0ldyy/comet/commit/9295fd2f8315e0e1d4586a059d2910a9c348a62a))
* magical db fix improving speed by 5000% ([67a20be](https://github.com/g0ldyy/comet/commit/67a20be6b1bac652085d97743e0251fbdaec01ae))

## [1.35.2](https://github.com/g0ldyy/comet/compare/v1.35.1...v1.35.2) (2024-11-14)


### Bug Fixes

* sample detection ([fe93bac](https://github.com/g0ldyy/comet/commit/fe93bacf23ee9911d450cd37631b813a7572af2b))

## [1.35.1](https://github.com/g0ldyy/comet/compare/v1.35.0...v1.35.1) (2024-11-14)


### Bug Fixes

* json dumps ([267a764](https://github.com/g0ldyy/comet/commit/267a76468ed90382ae2776089da7051cc4ccca05))
* json lib is not used anymore ([af6d009](https://github.com/g0ldyy/comet/commit/af6d009651cca60b8a5fb3c04a4a585ac40039b0))

## [1.35.0](https://github.com/g0ldyy/comet/compare/v1.34.0...v1.35.0) (2024-11-14)


### Features

* faster json stuff and better result ordering ([d8571d1](https://github.com/g0ldyy/comet/commit/d8571d189bda8bf3130695e921d5225dabddf698))

## [1.34.0](https://github.com/g0ldyy/comet/compare/v1.33.0...v1.34.0) (2024-11-14)


### Features

* direct torrent!!! omg!!!! no way what ([e621c84](https://github.com/g0ldyy/comet/commit/e621c84237b3514d9b954666c182b135cc686f99))

## [1.33.0](https://github.com/g0ldyy/comet/compare/v1.32.2...v1.33.0) (2024-11-14)


### Features

* add media fusion hashes ([ece4600](https://github.com/g0ldyy/comet/commit/ece4600ddec3bfc64cd39ce3fdb667b022c2cd35))
* custom MediaFusion urls for elfhosted &lt;3 ([bc29eb9](https://github.com/g0ldyy/comet/commit/bc29eb9582dca426ad58cc770f7d7d4d0cd6acd0))


### Bug Fixes

* add missing env var ([77ef84a](https://github.com/g0ldyy/comet/commit/77ef84a89306fe1bb63dc0178024a6c28fe6c262))
* I just want to release ([9bb4a4e](https://github.com/g0ldyy/comet/commit/9bb4a4ecc0c0e9449fc57b37b142025c314c5dda))
* update hashinfo ([c15206b](https://github.com/g0ldyy/comet/commit/c15206b4b410fc685fbbcd7f31df574553ca6cd5))

## [1.32.2](https://github.com/g0ldyy/comet/compare/v1.32.1...v1.32.2) (2024-10-04)


### Bug Fixes

* ip debrid ([057126b](https://github.com/g0ldyy/comet/commit/057126bbd00f818075969e5e73eff3e834a8ef1f))
* missing year metadata ([4a4dfb6](https://github.com/g0ldyy/comet/commit/4a4dfb68923d4c496b8e837eb3a238d6ba21ae95))
* realdebrid ip stuff ([6ee6b92](https://github.com/g0ldyy/comet/commit/6ee6b928a57351a82af284b3c6446bcbf01a8e10))
* weird shit ([1176b2a](https://github.com/g0ldyy/comet/commit/1176b2a84233f705033b142b1492c2bdc7da5b77))

## [1.32.1](https://github.com/g0ldyy/comet/compare/v1.32.0...v1.32.1) (2024-10-01)


### Bug Fixes

* title parsing ([732021c](https://github.com/g0ldyy/comet/commit/732021c99b8004192b15d29d5b63a39bce893568))

## [1.32.0](https://github.com/g0ldyy/comet/compare/v1.31.4...v1.32.0) (2024-09-18)


### Features

* autplay ([1147845](https://github.com/g0ldyy/comet/commit/1147845640a628bda61e0355f186d425587cb989))

## [1.31.4](https://github.com/g0ldyy/comet/compare/v1.31.3...v1.31.4) (2024-09-17)


### Bug Fixes

* bye samples ([efcf44d](https://github.com/g0ldyy/comet/commit/efcf44dbdff688031771e6834ca4d3244489fc3c))

## [1.31.3](https://github.com/g0ldyy/comet/compare/v1.31.2...v1.31.3) (2024-09-06)


### Bug Fixes

* latin and spanish separated ([2421667](https://github.com/g0ldyy/comet/commit/24216678b8fca0ab8edd48d239fb772f06712b5f))

## [1.31.2](https://github.com/g0ldyy/comet/compare/v1.31.1...v1.31.2) (2024-09-04)


### Bug Fixes

* fail build ([9e7407d](https://github.com/g0ldyy/comet/commit/9e7407d7ced4a1d570e11c350dd5b6ce4a878db6))

## [1.31.1](https://github.com/g0ldyy/comet/compare/v1.31.0...v1.31.1) (2024-09-04)


### Bug Fixes

* metadata ([23966f2](https://github.com/g0ldyy/comet/commit/23966f22887e1d744f2342a97d7b39652155ddb1))

## [1.31.0](https://github.com/g0ldyy/comet/compare/v1.30.1...v1.31.0) (2024-09-01)


### Features

* realdebrid real ip ([479aa8e](https://github.com/g0ldyy/comet/commit/479aa8e527ebfeec01ffcde5c825b0a5af19538e))

## [1.30.1](https://github.com/g0ldyy/comet/compare/v1.30.0...v1.30.1) (2024-08-31)


### Bug Fixes

* torrentio ([165cb25](https://github.com/g0ldyy/comet/commit/165cb256d2c4e27020eb775d52de299cddbdc0f5))
* zilean ([6a272fc](https://github.com/g0ldyy/comet/commit/6a272fcf7638581a171ea7ea20aaa5854c3552cc))

## [1.30.0](https://github.com/g0ldyy/comet/compare/v1.29.1...v1.30.0) (2024-08-31)


### Features

* add multi to languages ([da4d1d7](https://github.com/g0ldyy/comet/commit/da4d1d7cc2a4fc590c6acb0a7c0f43be9fc54568))

## [1.29.1](https://github.com/g0ldyy/comet/compare/v1.29.0...v1.29.1) (2024-08-31)


### Bug Fixes

* improves prowlarr compatibility ([7c0ee96](https://github.com/g0ldyy/comet/commit/7c0ee96dafb4a14db02c66e8cfaf2dd5ac05273c))

## [1.29.0](https://github.com/g0ldyy/comet/compare/v1.28.5...v1.29.0) (2024-08-31)


### Features

* faster than ever, better results ([19f9cc4](https://github.com/g0ldyy/comet/commit/19f9cc48ae83e27b43fdbb8541ec8d6b6f2878df))

## [1.28.5](https://github.com/g0ldyy/comet/compare/v1.28.4...v1.28.5) (2024-08-29)


### Bug Fixes

* debrid download link caching not working in certain case ([271dbb6](https://github.com/g0ldyy/comet/commit/271dbb6f43481c509e7a74018d0c804c263eadcd))

## [1.28.4](https://github.com/g0ldyy/comet/compare/v1.28.3...v1.28.4) (2024-08-28)


### Bug Fixes

* metadata ([809e32b](https://github.com/g0ldyy/comet/commit/809e32bb9c727076be12ab88b073b86213b50e12))

## [1.28.3](https://github.com/g0ldyy/comet/compare/v1.28.2...v1.28.3) (2024-08-28)


### Bug Fixes

* sqlite download_link caching ([34c93c6](https://github.com/g0ldyy/comet/commit/34c93c6d3b0d77a8573e810e1f6cefff71a83e85))

## [1.28.2](https://github.com/g0ldyy/comet/compare/v1.28.1...v1.28.2) (2024-08-28)


### Bug Fixes

* metadata retriever ([8f4e133](https://github.com/g0ldyy/comet/commit/8f4e13392ac65ce6b6d23f26f4bfafb629201574))

## [1.28.1](https://github.com/g0ldyy/comet/compare/v1.28.0...v1.28.1) (2024-08-27)


### Bug Fixes

* typo causing errors ([7584af6](https://github.com/g0ldyy/comet/commit/7584af66dd19d3a2c089f75b9f6cf1646ddbc25c))

## [1.28.0](https://github.com/g0ldyy/comet/compare/v1.27.1...v1.28.0) (2024-08-27)


### Features

* PostgreSQL support ([8f46d7f](https://github.com/g0ldyy/comet/commit/8f46d7f1829148190d27adf0d35ed64bf233d029))

## [1.27.1](https://github.com/g0ldyy/comet/compare/v1.27.0...v1.27.1) (2024-08-27)


### Bug Fixes

* Jinja2 missing sometimes ([11a96b7](https://github.com/g0ldyy/comet/commit/11a96b705b29b7a8dd72d81c6d2f6957360bfea9))
* jinja2????? ([55fe625](https://github.com/g0ldyy/comet/commit/55fe625c595ac2b8eea5ac3ee98219bbc6adeddf))

## [1.27.0](https://github.com/g0ldyy/comet/compare/v1.26.0...v1.27.0) (2024-08-27)


### Features

* disable indexer select if indexer manager disabled ([2907a0e](https://github.com/g0ldyy/comet/commit/2907a0e25ca3da405b309e0c90ce7d191e7825f7))

## [1.26.0](https://github.com/g0ldyy/comet/compare/v1.25.2...v1.26.0) (2024-08-27)


### Features

* new admin dashboard for debrid proxy stream + title year check + ip-based max connections for debrid proxy stream ([1d11ac4](https://github.com/g0ldyy/comet/commit/1d11ac46bde0c951ee0c3afa746e83a46bb8474f))


### Bug Fixes

* remove fullstacksample docker compose as it needs to be changed a lot ([7533ecb](https://github.com/g0ldyy/comet/commit/7533ecb4b08a349b25a1bce2e70b1481fc3407bd))

## [1.25.2](https://github.com/g0ldyy/comet/compare/v1.25.1...v1.25.2) (2024-08-16)


### Bug Fixes

* zilean ([40b62b2](https://github.com/g0ldyy/comet/commit/40b62b29dc351775da3c7d914203579abfede646))

## [1.25.1](https://github.com/g0ldyy/comet/compare/v1.25.0...v1.25.1) (2024-08-15)


### Bug Fixes

* zilean 1.5 ([12a9bc5](https://github.com/g0ldyy/comet/commit/12a9bc59ef545ff2ff86637791609fd8e7ffec57))

## [1.25.0](https://github.com/g0ldyy/comet/compare/v1.24.0...v1.25.0) (2024-08-06)


### Features

* Add metadata to result title ([33f3f96](https://github.com/g0ldyy/comet/commit/33f3f96b8c0d2416c8cdb1db8614970f714708ae))

## [1.24.0](https://github.com/g0ldyy/comet/compare/v1.23.2...v1.24.0) (2024-08-05)


### Features

* custom results title ([fa800ef](https://github.com/g0ldyy/comet/commit/fa800ef6b6a0f8ba91ebe2ebfa21147f9cb667a1))

## [1.23.2](https://github.com/g0ldyy/comet/compare/v1.23.1...v1.23.2) (2024-07-29)


### Bug Fixes

* alldebrid support ([1ea4464](https://github.com/g0ldyy/comet/commit/1ea446471ce7e9673724952a24075e64672d3510))

## [1.23.1](https://github.com/g0ldyy/comet/compare/v1.23.0...v1.23.1) (2024-07-24)


### Bug Fixes

* db error ([d1232ce](https://github.com/g0ldyy/comet/commit/d1232cec61b7ecc7d639098d949dc8696822a274))

## [1.23.0](https://github.com/g0ldyy/comet/compare/v1.22.0...v1.23.0) (2024-07-24)


### Features

* ability to enable/disable title match check (TITLE_MATCH_CHECK env var) ([755c4fb](https://github.com/g0ldyy/comet/commit/755c4fb8d21e74576aa615c7d84b399ebedeb45a))
* show language emoji in results ([d9e15e7](https://github.com/g0ldyy/comet/commit/d9e15e7e88244eca1c264221ca71e38b4dab7dbc))

## [1.22.0](https://github.com/g0ldyy/comet/compare/v1.21.2...v1.22.0) (2024-07-24)


### Features

* debrid download links are now cached for 1h (massive speed improvement) ([aab0b91](https://github.com/g0ldyy/comet/commit/aab0b918bd205b668ce15510c13d672efb5ed923))


### Bug Fixes

* imdb metadata wrong name ([0ecba3e](https://github.com/g0ldyy/comet/commit/0ecba3e64aaf6b3f5dad47f4f5527f0a8732d57a))
* torrentio proxy ([b098bb1](https://github.com/g0ldyy/comet/commit/b098bb1ed9a60af89e173c527bf4bb579a91a29c))

## [1.21.2](https://github.com/g0ldyy/comet/compare/v1.21.1...v1.21.2) (2024-07-24)


### Bug Fixes

* revert IMDb metadata bug ([595c2c0](https://github.com/g0ldyy/comet/commit/595c2c0da9f6a94a930e82d1e555cafefa343ad2))

## [1.21.1](https://github.com/g0ldyy/comet/compare/v1.21.0...v1.21.1) (2024-07-23)


### Bug Fixes

* coding blind ([2a5002a](https://github.com/g0ldyy/comet/commit/2a5002a3a134429fb9cffc708ac1973dceff0b40))
* IMDb metadata retriever ([914c5d2](https://github.com/g0ldyy/comet/commit/914c5d2d455cc4f12f738a4e3aa014aa816c619b))
* not retrieving saved stremio settings if all selected ([42c5863](https://github.com/g0ldyy/comet/commit/42c5863297807f231e04f2eb25510b146c6f789a))
* proxy stream ([1ceeff5](https://github.com/g0ldyy/comet/commit/1ceeff57223f521b006b4311c314ed62f56096fe))
* try to fix Nvidia shield debrid stream proxying ([bdb22dc](https://github.com/g0ldyy/comet/commit/bdb22dcc63b4ca6fd014faaf287b7d8034093c12))
* where is the client ([c532656](https://github.com/g0ldyy/comet/commit/c532656b3bba916a5df8cbdcf306b822ef94a871))

## [1.21.0](https://github.com/g0ldyy/comet/compare/v1.20.0...v1.21.0) (2024-07-22)


### Features

* fetch previous settings from stremio configure ([b42f4fb](https://github.com/g0ldyy/comet/commit/b42f4fbc96622a48f035de10e711bae2c9a9e2d2))
* save settings on Stremio configure button ([6dd22cc](https://github.com/g0ldyy/comet/commit/6dd22ccce7858967c287ad028ea011caefffd2af))

## [1.20.0](https://github.com/g0ldyy/comet/compare/v1.19.5...v1.20.0) (2024-07-22)


### Features

* use debrid proxy for torrentio (bypass Cloudflare server IP blacklist) ([5d02247](https://github.com/g0ldyy/comet/commit/5d02247021ca897aa79842bde1bbb56817a58967))

## [1.19.5](https://github.com/g0ldyy/comet/compare/v1.19.4...v1.19.5) (2024-07-21)


### Bug Fixes

* prowlarr indexer ([4003fb4](https://github.com/g0ldyy/comet/commit/4003fb4fc06c70364fb7d64a077a334d303de085))

## [1.19.4](https://github.com/g0ldyy/comet/compare/v1.19.3...v1.19.4) (2024-07-21)


### Bug Fixes

* debrid stream proxy on some devices ([240364b](https://github.com/g0ldyy/comet/commit/240364bd883070cf2556fb2b7bdd89fa4a1230f6))

## [1.19.3](https://github.com/g0ldyy/comet/compare/v1.19.2...v1.19.3) (2024-07-21)


### Bug Fixes

* wtf ([b47676c](https://github.com/g0ldyy/comet/commit/b47676cc86758964bb9feb5a4ebcccc97ae7ef02))

## [1.19.2](https://github.com/g0ldyy/comet/compare/v1.19.1...v1.19.2) (2024-07-21)


### Bug Fixes

* Delete poetry.lock ([833bb33](https://github.com/g0ldyy/comet/commit/833bb332c4b4899372b08007c57db4084752339e))

## [1.19.1](https://github.com/g0ldyy/comet/compare/v1.19.0...v1.19.1) (2024-07-21)


### Bug Fixes

* torrentio cloudflare bypass ([d9c98a7](https://github.com/g0ldyy/comet/commit/d9c98a75cb4e101d00395022549499b9a7d3965b))

## [1.19.0](https://github.com/g0ldyy/comet/compare/v1.18.3...v1.19.0) (2024-07-20)


### Features

* torrentio scraper ([94c21d9](https://github.com/g0ldyy/comet/commit/94c21d962d784405d0c09e2986393b90c497dfd9))


### Bug Fixes

* add torrentio scraper to features ([27d23a5](https://github.com/g0ldyy/comet/commit/27d23a5fa6bb15a92be61ec7701207554864e422))

## [1.18.3](https://github.com/g0ldyy/comet/compare/v1.18.2...v1.18.3) (2024-07-19)


### Bug Fixes

* slightly improve anime results ([c9f230c](https://github.com/g0ldyy/comet/commit/c9f230c9cb8649dbe5f0dfab923039785b4c885b))

## [1.18.2](https://github.com/g0ldyy/comet/compare/v1.18.1...v1.18.2) (2024-07-19)


### Bug Fixes

* faster debrid file parsing ([bf846fa](https://github.com/g0ldyy/comet/commit/bf846fa8a8d8444d13f404ec1651e7f0d71b4697))

## [1.18.1](https://github.com/g0ldyy/comet/compare/v1.18.0...v1.18.1) (2024-07-19)


### Bug Fixes

* torrents faster ([ef53efe](https://github.com/g0ldyy/comet/commit/ef53efe7337679cc1cf1843d1efa956c09cad62a))

## [1.18.0](https://github.com/g0ldyy/comet/compare/v1.17.2...v1.18.0) (2024-07-18)


### Features

* Kitsu support ([b641c69](https://github.com/g0ldyy/comet/commit/b641c69c04173b82386a4e150fccba2e078a85be))

## [1.17.2](https://github.com/g0ldyy/comet/compare/v1.17.1...v1.17.2) (2024-07-18)


### Bug Fixes

* update manifest for animes support ([8ee3220](https://github.com/g0ldyy/comet/commit/8ee32205f14ad098dc96c1f9aea3f78314807431))

## [1.17.1](https://github.com/g0ldyy/comet/compare/v1.17.0...v1.17.1) (2024-07-18)


### Bug Fixes

* metadata ([81ff5bc](https://github.com/g0ldyy/comet/commit/81ff5bc71b1b8a683f662fa9af7c364cf6046b85))

## [1.17.0](https://github.com/g0ldyy/comet/compare/v1.16.0...v1.17.0) (2024-07-18)


### Features

* error videos added to playback ([83668ea](https://github.com/g0ldyy/comet/commit/83668ea3c15858504dfbb58fae5764995dc30bf9))


### Bug Fixes

* faster (we title match check before doing the rest) ([19fc235](https://github.com/g0ldyy/comet/commit/19fc235e838bcfb6d3d7e06f95309ff72c491fd5))

## [1.16.0](https://github.com/g0ldyy/comet/compare/v1.15.5...v1.16.0) (2024-07-18)


### Features

* add real torrent_size in results ([33ef5b1](https://github.com/g0ldyy/comet/commit/33ef5b119fa9f01bb914ba969bf19f95b61a761f))
* config string now sent to console when copy/install button clicked ([9f46fa9](https://github.com/g0ldyy/comet/commit/9f46fa98ddfdd9a0466f200144cd72dc09a8e160))

## [1.15.5](https://github.com/g0ldyy/comet/compare/v1.15.4...v1.15.5) (2024-07-16)


### Bug Fixes

* prowlarr infoHash ([71738ee](https://github.com/g0ldyy/comet/commit/71738ee3a65115694a219b4e55e645df2ccef138))

## [1.15.4](https://github.com/g0ldyy/comet/compare/v1.15.3...v1.15.4) (2024-07-16)


### Bug Fixes

* I fixed it I think guys ([86ea5d5](https://github.com/g0ldyy/comet/commit/86ea5d5a3d2af47e3698c04f160e195cf8def614))

## [1.15.3](https://github.com/g0ldyy/comet/compare/v1.15.2...v1.15.3) (2024-07-16)


### Bug Fixes

* hope ([0086f3f](https://github.com/g0ldyy/comet/commit/0086f3f90ed31dba29d5e5ad0f6b6451f9144e91))

## [1.15.2](https://github.com/g0ldyy/comet/compare/v1.15.1...v1.15.2) (2024-07-15)


### Bug Fixes

* alldebrid .fr to .com ([22c4445](https://github.com/g0ldyy/comet/commit/22c444573766bba40ac5d40fe83a0bc2fbf04ec3))
* int to float ([a0d72d8](https://github.com/g0ldyy/comet/commit/a0d72d8849b289a6f7c7be4e4219a46ccdd82ee2))

## [1.15.1](https://github.com/g0ldyy/comet/compare/v1.15.0...v1.15.1) (2024-07-15)


### Bug Fixes

* do the maths client side ([b0aa42c](https://github.com/g0ldyy/comet/commit/b0aa42c519767b61ed9d3dfdec7eceed79e127ae))

## [1.15.0](https://github.com/g0ldyy/comet/compare/v1.14.2...v1.15.0) (2024-07-15)


### Features

* faster, prowlarr pack support, max size, tracker name in results and more... ([0fabd79](https://github.com/g0ldyy/comet/commit/0fabd79a6e07b33fde76074ebbb5193cea7b9d10))
* realTitle in results for prowlarr support ([57440a9](https://github.com/g0ldyy/comet/commit/57440a9d08d2630ed084cfd07b374c9d9a96ef19))


### Bug Fixes

* valid debrid api key not needed anymore if results already cached ([e94e224](https://github.com/g0ldyy/comet/commit/e94e2249a0ac9bd99eaee1f90dd3cdfd41bc43ba))

## [1.14.2](https://github.com/g0ldyy/comet/compare/v1.14.1...v1.14.2) (2024-07-13)


### Bug Fixes

* divising by 0 ([dcef22a](https://github.com/g0ldyy/comet/commit/dcef22a46d6331befae31d51a977ed714868366f))

## [1.14.1](https://github.com/g0ldyy/comet/compare/v1.14.0...v1.14.1) (2024-07-12)


### Bug Fixes

* speed up playback ([683a826](https://github.com/g0ldyy/comet/commit/683a82656aa35a90a891ef902f59acb967c078e4))

## [1.14.0](https://github.com/g0ldyy/comet/compare/v1.13.0...v1.14.0) (2024-07-12)


### Features

* Debrid-Link support ([dadca78](https://github.com/g0ldyy/comet/commit/dadca784b1ddddbbde1d7db59e2d12039a99f60a))


### Bug Fixes

* alldebrid get files ([4bf8e88](https://github.com/g0ldyy/comet/commit/4bf8e88c0be7e82349581c8027f4ecb407ec99de))

## [1.13.0](https://github.com/g0ldyy/comet/compare/v1.12.4...v1.13.0) (2024-07-11)


### Features

* torbox integration ([db1a003](https://github.com/g0ldyy/comet/commit/db1a003a7b6225ea06dfd118cb8247dd2b385a1e))


### Bug Fixes

* torbox faster (removed useless checks) ([dfaf12b](https://github.com/g0ldyy/comet/commit/dfaf12b6afcf7d1aa2d7d998bdf9dedef8ec0502))
* torbox ignore empty data ([f971255](https://github.com/g0ldyy/comet/commit/f971255c0f6da4207e015d0a7d779b9a89afdb91))
* useless repeated code ([fa8ce05](https://github.com/g0ldyy/comet/commit/fa8ce0546575eb7e93560dc25649ea9620a2e554))

## [1.12.4](https://github.com/g0ldyy/comet/compare/v1.12.3...v1.12.4) (2024-07-10)


### Bug Fixes

* realdebrid pack caching issue ([0a9eb03](https://github.com/g0ldyy/comet/commit/0a9eb03c5895fffaff8f13c6eb96d546487dcccd))

## [1.12.3](https://github.com/g0ldyy/comet/compare/v1.12.2...v1.12.3) (2024-07-08)


### Bug Fixes

* zilean multithreaded ([73b0a35](https://github.com/g0ldyy/comet/commit/73b0a35d38dd29c6b9ba4dad116f8f86f5086064))

## [1.12.2](https://github.com/g0ldyy/comet/compare/v1.12.1...v1.12.2) (2024-07-08)


### Bug Fixes

* lower not needed ([ff60d91](https://github.com/g0ldyy/comet/commit/ff60d91091f465db1c5b06eeb3ad8efd064f2c24))

## [1.12.1](https://github.com/g0ldyy/comet/compare/v1.12.0...v1.12.1) (2024-07-08)


### Bug Fixes

* wrong debrid stream proxy password warning being showed everytime ([0514484](https://github.com/g0ldyy/comet/commit/05144847976207a9780460a92e7c1b1ff20f10f1))

## [1.12.0](https://github.com/g0ldyy/comet/compare/v1.11.2...v1.12.0) (2024-07-08)


### Features

* wrong password warning for debrid stream proxy ([6b8303b](https://github.com/g0ldyy/comet/commit/6b8303b2745bf3191a60f0a9c25d0d2bd81c4436))

## [1.11.2](https://github.com/g0ldyy/comet/compare/v1.11.1...v1.11.2) (2024-07-07)


### Bug Fixes

* prowlarr ([a6179bf](https://github.com/g0ldyy/comet/commit/a6179bf3b029619286edbc1a7746091d0d254d7b))

## [1.11.1](https://github.com/g0ldyy/comet/compare/v1.11.0...v1.11.1) (2024-07-07)


### Bug Fixes

* webui copy button for non secured environments ([8eda672](https://github.com/g0ldyy/comet/commit/8eda6720ff3b27bc748b9329fc0a5442310aa291))

## [1.11.0](https://github.com/g0ldyy/comet/compare/v1.10.0...v1.11.0) (2024-07-07)


### Features

* pack support for alldebrid ([30fe1ef](https://github.com/g0ldyy/comet/commit/30fe1ef6bd97aec5545aa808bf77db9ae4a8d4fa))
* zilean only mode ([123378f](https://github.com/g0ldyy/comet/commit/123378ff351ed4b6df15f0a33c6d1227f6f91fc5))


### Bug Fixes

* zilean filtering speed fixed ([dad68c2](https://github.com/g0ldyy/comet/commit/dad68c256e93df99a3aa5ed810dc495dc67d739d))

## [1.10.0](https://github.com/g0ldyy/comet/compare/v1.9.1...v1.10.0) (2024-07-07)


### Features

* premiumize support ([ab846a7](https://github.com/g0ldyy/comet/commit/ab846a76134fa2c0ebf6091a878bf3f0aeb93ff4))


### Bug Fixes

* proxy is not needed with premiumize ([82fb354](https://github.com/g0ldyy/comet/commit/82fb354ed10a471f1ac3d9b807af71c1ae6c80de))

## [1.9.1](https://github.com/g0ldyy/comet/compare/v1.9.0...v1.9.1) (2024-07-06)


### Bug Fixes

* easier for elfhosted to process ([6443fa1](https://github.com/g0ldyy/comet/commit/6443fa1b5fe601cbdf27e208bd80feaf3ac3156d))
* log error ([9fb92eb](https://github.com/g0ldyy/comet/commit/9fb92eb3780e8b312cdb97dd566f699846eaec4e))

## [1.9.0](https://github.com/g0ldyy/comet/compare/v1.8.1...v1.9.0) (2024-07-06)


### Features

* ui improvements ([9a94f0f](https://github.com/g0ldyy/comet/commit/9a94f0fc272c07185f16ecbb0463335a04b6a77f))

## [1.8.1](https://github.com/g0ldyy/comet/compare/v1.8.0...v1.8.1) (2024-07-06)


### Bug Fixes

* debrid stream proxy dynamic input field ([2e0a219](https://github.com/g0ldyy/comet/commit/2e0a219ca523e4f9f4e1fdd1301e651f2846470e))

## [1.8.0](https://github.com/g0ldyy/comet/compare/v1.7.0...v1.8.0) (2024-07-06)


### Features

* improve debrid stream proxy ([60710fa](https://github.com/g0ldyy/comet/commit/60710faa41d732c441fdd76a03c20b8649c7274d))


### Bug Fixes

* logs ([8743e49](https://github.com/g0ldyy/comet/commit/8743e4973b0db85d252fca321f04f23f0ca54a55))

## [1.7.0](https://github.com/g0ldyy/comet/compare/v1.6.2...v1.7.0) (2024-07-05)


### Features

* cosmetics + important fixes ([a1441c8](https://github.com/g0ldyy/comet/commit/a1441c8d9a0bc788caad359e823034a709c1cc2c))

## [1.6.2](https://github.com/g0ldyy/comet/compare/v1.6.1...v1.6.2) (2024-07-05)


### Bug Fixes

* I forgot something I need to release for elfhosted bruh ([14d6ebf](https://github.com/g0ldyy/comet/commit/14d6ebf0f43c87d6e199e6fdd57a02bfa6acc973))

## [1.6.1](https://github.com/g0ldyy/comet/compare/v1.6.0...v1.6.1) (2024-07-05)


### Bug Fixes

* alldebrid stream proxy ([1502d4c](https://github.com/g0ldyy/comet/commit/1502d4cf17e785b5972d781a185eda1dade119dd))

## [1.6.0](https://github.com/g0ldyy/comet/compare/v1.5.1...v1.6.0) (2024-07-05)


### Features

* alldebrid support ([afd27c0](https://github.com/g0ldyy/comet/commit/afd27c083da37204797246450619a4f9c1a7fc89))

## [1.5.1](https://github.com/g0ldyy/comet/compare/v1.5.0...v1.5.1) (2024-07-05)


### Bug Fixes

* imdb metadata api returning "Summer Watch Guide" ([e9af38b](https://github.com/g0ldyy/comet/commit/e9af38bdc1c1d0aea225ef10f201658ce4b749c6))

## [1.5.0](https://github.com/g0ldyy/comet/compare/v1.4.0...v1.5.0) (2024-07-04)


### Features

* insane new debrid stream proxy (allows to use debrid service on multiple IPs at same time) ([ba1f78e](https://github.com/g0ldyy/comet/commit/ba1f78eb84fda5e4d3b9f488d5fbde46fa57317d))


### Bug Fixes

* try to fix session unclosed issue with debrid stream proxy ([1f63113](https://github.com/g0ldyy/comet/commit/1f63113d8d418a8101cea925625f6d1d0cef5cf7))

## [1.4.0](https://github.com/g0ldyy/comet/compare/v1.3.3...v1.4.0) (2024-07-04)


### Features

* add option to toggle title checking ([bbb257d](https://github.com/g0ldyy/comet/commit/bbb257dda87875bfcb0d236d1cd82e9a3781fdef))

## [1.3.3](https://github.com/g0ldyy/comet/compare/v1.3.2...v1.3.3) (2024-07-03)


### Bug Fixes

* 0.30ms faster :pro: ([6b1daa6](https://github.com/g0ldyy/comet/commit/6b1daa61a0106d6557047059348ab6e7caecb693))

## [1.3.2](https://github.com/g0ldyy/comet/compare/v1.3.1...v1.3.2) (2024-07-03)


### Bug Fixes

* only show stars for non mobile users (performance issue) ([7415984](https://github.com/g0ldyy/comet/commit/741598497eedbb4be3c75e89470077e68ed3528d))

## [1.3.1](https://github.com/g0ldyy/comet/compare/v1.3.0...v1.3.1) (2024-07-03)


### Bug Fixes

* that's how you release it hehe ([ad65d1f](https://github.com/g0ldyy/comet/commit/ad65d1f99e9996f06d88b27c168d70b3b3998126))

## [1.3.0](https://github.com/g0ldyy/comet/compare/v1.2.2...v1.3.0) (2024-07-03)


### Features

* new debrid manager and cleaning (ruff) ([c48e466](https://github.com/g0ldyy/comet/commit/c48e4661298d7425627ec03510efbcde19c46b2a))

## [1.2.2](https://github.com/g0ldyy/comet/compare/v1.2.1...v1.2.2) (2024-07-03)


### Bug Fixes

* aiohttp max number of simultaneous connections ([9309e15](https://github.com/g0ldyy/comet/commit/9309e15a121feb69310cb8a3533447cde3b453ad))
* real debrid error ([704f841](https://github.com/g0ldyy/comet/commit/704f8419cd3107bc5efaa5e9befa36063bb96dd5))

## [1.2.1](https://github.com/g0ldyy/comet/compare/v1.2.0...v1.2.1) (2024-07-03)


### Bug Fixes

* smooth loading + fixed empty select (web) ([f9dd917](https://github.com/g0ldyy/comet/commit/f9dd91719883d08dbac531cec8ac0ca7a4b45e88))

## [1.2.0](https://github.com/g0ldyy/comet/compare/v1.1.0...v1.2.0) (2024-07-02)


### Features

* lot of fixes, caching system changed, camel to snake etc... ([9cf3346](https://github.com/g0ldyy/comet/commit/9cf3346ed5992ba5ecc88c82530fab91331cc089))

## [1.1.0](https://github.com/g0ldyy/comet/compare/v1.0.0...v1.1.0) (2024-07-02)


### Features

* environment variable for custom addon ID (ADDON_ID) ([d2b7ee8](https://github.com/g0ldyy/comet/commit/d2b7ee84bd96f5f8d4f5bbb0abd96ab4d3f24833))

## 1.0.0 (2024-07-02)


### Features

* add logger. refactor main.py. move settings. ([36f2a82](https://github.com/g0ldyy/comet/commit/36f2a827bf48fa4c5d5bdb581aa00b503a9a1be4))
* create test ([df020e4](https://github.com/g0ldyy/comet/commit/df020e4f614d5a4506e4f08f96c96d82463de876))
* restructured/refactored/reorganized ([299c721](https://github.com/g0ldyy/comet/commit/299c7217e4df00814cc2635fb5d05e54f6874df9))
* restructured/refactored/reorganized ([b02d84c](https://github.com/g0ldyy/comet/commit/b02d84c36e53faf23760abe88ebb43a4cb9f3432))


### Bug Fixes

* add additional logging ([1d0597a](https://github.com/g0ldyy/comet/commit/1d0597ac1641aae1867741906b03428f45725faa))
* correct compose files ([11430b4](https://github.com/g0ldyy/comet/commit/11430b447e42aacc0c126db871154c2c3a0e2eec))
* please fix ([184e24f](https://github.com/g0ldyy/comet/commit/184e24f865c5f9bf4ce90eaa59095b64f4ecc372))
* remove random print ([2b74a50](https://github.com/g0ldyy/comet/commit/2b74a5053d79a498a2a216cd0107bda2ce177d9a))
* remove the useless test file ([f8a9998](https://github.com/g0ldyy/comet/commit/f8a9998f7ad906795660af0137ba7cf8d452bbe8))
* replaced os with pydantic for env getters ([d5b7632](https://github.com/g0ldyy/comet/commit/d5b76327db2eb0c40d90c97b86573260b145b1d0))
* test ([174aa65](https://github.com/g0ldyy/comet/commit/174aa655053262b1f3b6174463b8f11ed3f2b298))
* title ([e841f08](https://github.com/g0ldyy/comet/commit/e841f081bda10fbe74284ed87e5655bd60b8f3b2))
* unset local variable ([c8b6736](https://github.com/g0ldyy/comet/commit/c8b673643a7da9c49f81f0965c620350c81ba799))
