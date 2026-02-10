ADDON_DIR = plugin.video.comet
BUILD_DIR = build
DIST_DIR = dist
ASSETS_DIR = ../comet/assets
VERSION := $(shell sed -n 's/^<addon[^>]*version=\"\([^\"]*\)\".*/\1/p' $(ADDON_DIR)/addon.xml)
ZIP_FILE := $(ADDON_DIR)-$(VERSION).zip

all: package

package: clean
	@mkdir -p $(BUILD_DIR)/$(ADDON_DIR)
	@mkdir -p $(DIST_DIR)
	@cp -r $(ADDON_DIR)/* $(BUILD_DIR)/$(ADDON_DIR)
	@cp $(ASSETS_DIR)/icon.png $(BUILD_DIR)/$(ADDON_DIR)/resources/icon.png
	@cp $(ASSETS_DIR)/background.png $(BUILD_DIR)/$(ADDON_DIR)/resources/fanart.png
	@find $(BUILD_DIR)/$(ADDON_DIR) -type d -name __pycache__ -prune -exec rm -rf {} +
	@find $(BUILD_DIR)/$(ADDON_DIR) -type f -name '*.py[co]' -delete
	@cd $(BUILD_DIR) && zip -r ../$(DIST_DIR)/$(ZIP_FILE) $(ADDON_DIR)
	@echo "Built $(DIST_DIR)/$(ZIP_FILE)"

package-flat: clean
	@mkdir -p $(BUILD_DIR)/$(ADDON_DIR)
	@mkdir -p $(DIST_DIR)
	@cp -r $(ADDON_DIR)/* $(BUILD_DIR)/$(ADDON_DIR)
	@cp $(ASSETS_DIR)/icon.png $(BUILD_DIR)/$(ADDON_DIR)/resources/icon.png
	@cp $(ASSETS_DIR)/background.png $(BUILD_DIR)/$(ADDON_DIR)/resources/fanart.png
	@find $(BUILD_DIR)/$(ADDON_DIR) -type d -name __pycache__ -prune -exec rm -rf {} +
	@find $(BUILD_DIR)/$(ADDON_DIR) -type f -name '*.py[co]' -delete
	@cd $(BUILD_DIR)/$(ADDON_DIR) && zip -r ../../$(DIST_DIR)/$(ADDON_DIR)-$(VERSION)-flat.zip .
	@echo "Built $(DIST_DIR)/$(ADDON_DIR)-$(VERSION)-flat.zip"

clean:
	@rm -rf $(BUILD_DIR) $(DIST_DIR)

.PHONY: all package package-flat clean
