# Comet Kodi Add-on

Kodi plugin (`plugin.video.comet`) and its update repository (`repository.comet`).

## Build

```sh
cd kodi
make          # Full build: add-on + repository
make package  # Add-on zip only
```

Outputs in `kodi/dist/`:
```
dist/
├── addons.xml + addons.xml.md5
├── plugin.video.comet/
│   ├── addon.xml
│   └── plugin.video.comet-X.Y.Z.zip
├── repository.comet/
│   ├── addon.xml
│   └── repository.comet-X.Y.Z.zip
└── index.html
```

## Install in Kodi

### Via Repository (Recommended)

Enables automatic updates.

1. **Settings → File manager → Add source** → enter `https://g0ldyy.github.io/comet`
2. **Add-ons → Install from zip file** → select `Comet` → install `repository.comet-X.Y.Z.zip`
3. **Install from repository → Comet Repository → Video add-ons → Comet → Install**
4. Configure: **Add-ons → My add-ons → Video add-ons → Comet → Configure**

### Manual

No automatic updates with this method.

1. Download the latest plugin zip from the [Comet Repository Page](https://g0ldyy.github.io/comet/)
2. **Add-ons → Install from zip file** → select the downloaded zip
3. Configure: **Add-ons → My add-ons → Video add-ons → Comet → Configure**
