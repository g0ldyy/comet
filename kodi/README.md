# ☄️ Comet Kodi Add-on

Kodi plugin (`plugin.video.comet`) and its update repository (`repository.comet`). This add-on allows you to stream your favorite torrents and debrid services directly within the Kodi media player.

## 🚀 Installation (Recommended)

Using the repository ensures that you receive automatic updates.

1.  **Add Source**: Go to **Settings** ➔ **File manager** ➔ **Add source**.
2.  **Enter URL**: Enter `https://g0ldyy.github.io/comet` and name it `Comet`.
3.  **Install Repository**: Go to **Add-ons** ➔ **Install from zip file** ➔ select `Comet` ➔ install `repository.comet-X.Y.Z.zip`.
4.  **Install Add-on**: Go to **Install from repository** ➔ **Comet Repository** ➔ **Video add-ons** ➔ **Comet** ➔ **Install**.

## ⚙️ Configuration

Once installed, you need to link the add-on to your Comet instance:

1.  Go to **Add-ons** ➔ **My add-ons** ➔ **Video add-ons** ➔ **Comet** ➔ **Configure**.
2.  In the **Comet** category, click on **Configure/Reconfigure**.
3.  A window will appear with a **6-character Setup Code** (e.g., `1A2B3C`).
4.  Go to your Comet configuration page in your browser.
5.  Fill in your settings (Real-Debrid, resolutions, etc.).
6.  Click the **Setup Kodi** button at the bottom.
7.  Enter the code shown in Kodi and click **Setup**.

## 📦 Manual Installation

*Note: You will not receive automatic updates with this method.*

1.  Download the latest plugin zip from the [Comet Repository Page](https://g0ldyy.github.io/comet/).
2.  Go to **Add-ons** ➔ **Install from zip file** ➔ select the downloaded zip.
3.  Open the add-on and follow the **Configuration** steps above.

---

## 🛠️ Development & Building

If you want to build the add-on from source:

```sh
cd kodi
make          # Full build: add-on + repository
make package  # Add-on zip only
```

### Build Outputs (`kodi/dist/`)
```text
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
