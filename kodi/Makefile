DIST    = dist
BUILD   = build
ADDON   = plugin.video.comet
REPO    = repository.comet
ASSETS  = ../comet/assets

V_ADDON := $(shell sed -n 's/^<addon.*version="\([^"]*\)".*/\1/p' $(ADDON)/addon.xml)
V_REPO  := $(shell sed -n 's/^<addon.*version="\([^"]*\)".*/\1/p' $(REPO)/addon.xml)

all: clean
	@echo "🚀 Building Comet Kodi Repository (Plugin v$(V_ADDON) / Repo v$(V_REPO))..."
	@$(MAKE) build-zip ID=$(ADDON) VER=$(V_ADDON) IS_ADDON=1 --no-print-directory
	@$(MAKE) build-zip ID=$(REPO)  VER=$(V_REPO) --no-print-directory
	@echo "📦 Generating repository index..."
	@python3 generate_repository.py
	@sed -e 's|ADDON_URL|$(ADDON)/$(ADDON)-$(V_ADDON).zip|g' \
		 -e 's|V_ADDON|$(V_ADDON)|g' \
		 -e 's|REPO_URL|$(REPO)/$(REPO)-$(V_REPO).zip|g' \
		 -e 's|V_REPO|$(V_REPO)|g' \
		 index.html.template > $(DIST)/index.html
	@rm -rf $(BUILD)
	@echo "✨ Success: $(DIST)/ is ready."

build-zip:
	@mkdir -p $(BUILD)/$(ID) $(DIST)/$(ID)
	@cp -a $(ID)/. $(BUILD)/$(ID)
	@IMG_DIR=$(if $(IS_ADDON),resources/,); \
	mkdir -p $(BUILD)/$(ID)/$${IMG_DIR} $(DIST)/$(ID)/$${IMG_DIR}; \
	cp $(ASSETS)/icon.png $(BUILD)/$(ID)/$${IMG_DIR}icon.png; \
	cp $(ASSETS)/background.png $(BUILD)/$(ID)/$${IMG_DIR}fanart.png; \
	cp $(ASSETS)/icon.png $(DIST)/$(ID)/icon.png; \
	cp $(ASSETS)/background.png $(DIST)/$(ID)/fanart.png; \
	cp $(ASSETS)/icon.png $(DIST)/$(ID)/$${IMG_DIR}icon.png; \
	cp $(ASSETS)/background.png $(DIST)/$(ID)/$${IMG_DIR}fanart.png
	@$(if $(IS_ADDON),find $(BUILD)/$(ID) -name "__pycache__" -exec rm -rf {} +)
	@cp $(ID)/addon.xml $(DIST)/$(ID)/
	@cd $(BUILD) && zip -r9q ../$(DIST)/$(ID)/$(ID)-$(VER).zip $(ID)
	@echo "  ✅ Built $(ID)"

clean:
	@rm -rf $(BUILD) $(DIST)

.PHONY: all clean build-zip
