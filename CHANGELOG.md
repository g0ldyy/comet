# Changelog

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
