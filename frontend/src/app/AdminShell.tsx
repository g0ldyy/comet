import * as Dialog from "@radix-ui/react-dialog";
import { useQuery } from "@tanstack/react-query";
import { Link, Outlet, useNavigate } from "@tanstack/react-router";
import {
  ArrowRight,
  ChartNoAxesCombined,
  ChevronLeft,
  CircleGauge,
  FileClock,
  LogOut,
  type LucideIcon,
  Menu,
  Network,
  RadioTower,
  Search,
  ServerCog,
  Settings,
  ShieldCheck,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Brand } from "../components/Brand";
import { CommunityLinks } from "../components/CommunityLinks";
import { Button } from "../components/ui/Button";
import { clearAdminSession } from "../features/auth/AdminBoundary";
import { logoutAdmin } from "../features/auth/api";
import { getSettings } from "../features/settings/api";
import { LanguageSelector } from "../i18n/LanguageSelector";
import { normalizeSearch, searchScore, translationDocuments } from "./admin-search";

const navigation = [
  {
    icon: CircleGauge,
    key: "nav.overview",
    scopes: ["overview", "metrics"],
    to: "/admin/overview",
  },
  { icon: FileClock, key: "nav.logs", scopes: ["events"], to: "/admin/logs" },
  {
    icon: ChartNoAxesCombined,
    key: "nav.analytics",
    scopes: ["analytics", "metrics"],
    to: "/admin/analytics",
  },
  {
    icon: RadioTower,
    key: "nav.usenet",
    scopes: ["usenet"],
    to: "/admin/usenet",
    tone: "usenet",
  },
  { icon: ShieldCheck, key: "nav.proxy", scopes: ["proxy"], to: "/admin/proxy" },
  {
    icon: SlidersHorizontal,
    key: "nav.scraping",
    scopes: ["scraping"],
    to: "/admin/scraping",
  },
  { icon: Network, key: "nav.cometnet", scopes: ["cometnet"], to: "/admin/cometnet" },
  { icon: Settings, key: "nav.settings", scopes: ["settings"], to: "/admin/settings" },
  { icon: ServerCog, key: "nav.system", scopes: ["system"], to: "/admin/system" },
] as const;

function Navigation({ collapsed, onNavigate }: { collapsed: boolean; onNavigate?: () => void }) {
  const { t } = useTranslation();
  return (
    <nav aria-label={t("nav.admin")} className="sidebar__navigation">
      {navigation.map((item) => {
        const Icon = item.icon;
        const tone = "tone" in item ? item.tone : undefined;
        return (
          <Link
            activeProps={{ className: "sidebar__link sidebar__link--active" }}
            className={`sidebar__link ${tone ? `sidebar__link--${tone}` : ""}`}
            key={item.to}
            onClick={onNavigate}
            title={collapsed ? t(item.key) : undefined}
            to={item.to}
          >
            <Icon aria-hidden="true" size={19} strokeWidth={1.8} />
            <span>{t(item.key)}</span>
          </Link>
        );
      })}
    </nav>
  );
}

interface QuickRouteItem {
  detail: string;
  icon: LucideIcon;
  id: string;
  kind: "route";
  label: string;
  search: string;
  to: (typeof navigation)[number]["to"];
}

interface QuickTargetItem {
  detail: string;
  icon: LucideIcon;
  id: string;
  kind: "target";
  label: string;
  search: string;
  spotlight: HTMLElement;
  target: HTMLElement;
}

interface QuickDeepItem {
  detail: string;
  icon: LucideIcon;
  id: string;
  kind: "deep";
  label: string;
  search: string;
  targetLabel: string;
  to: (typeof navigation)[number]["to"];
}

type QuickItem = QuickDeepItem | QuickRouteItem | QuickTargetItem;

const searchableElements = [
  "h2",
  "h3",
  "p",
  "label",
  "button",
  "a",
  "summary",
  "dt",
  "th",
  ".metric-card__label",
].join(",");

function elementLabel(element: HTMLElement): string {
  return (element.getAttribute("aria-label") ?? element.textContent ?? "")
    .replace(/\s+/g, " ")
    .trim();
}

function elementTarget(element: HTMLElement): HTMLElement {
  return element instanceof HTMLLabelElement && element.control instanceof HTMLElement
    ? element.control
    : element;
}

function elementSpotlight(target: HTMLElement): HTMLElement {
  return (
    target.closest<HTMLElement>(
      ".setting-card, .dashboard-panel, .metric-card, .operations-table-wrap, .field",
    ) ?? target
  );
}

function reveal(target: HTMLElement, spotlight: HTMLElement) {
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  target.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "center" });
  target.focus({ preventScroll: true });
  if (!reducedMotion) {
    spotlight.animate(
      [
        { boxShadow: "0 0 0 1px rgb(255 90 54 / 72%), 0 0 0 6px rgb(255 90 54 / 14%)" },
        { boxShadow: "0 0 0 1px rgb(255 90 54 / 0%), 0 0 0 0 rgb(255 90 54 / 0%)" },
      ],
      { duration: 900, easing: "ease-out" },
    );
  }
}

function pageTargets(): QuickTargetItem[] {
  const content = document.getElementById("main-content") as HTMLElement;
  const detail = content.querySelector("h1")?.textContent?.trim() ?? "Comet";
  const labels = new Set<string>();
  const items: QuickTargetItem[] = [];

  for (const [index, element] of content
    .querySelectorAll<HTMLElement>(searchableElements)
    .entries()) {
    const label = elementLabel(element);
    const key = normalizeSearch(label);
    if (key === "" || labels.has(key)) continue;
    labels.add(key);

    const target = elementTarget(element);
    items.push({
      detail,
      icon: ArrowRight,
      id: `target-${index}`,
      kind: "target",
      label,
      search: normalizeSearch(`${label} ${detail}`),
      spotlight: elementSpotlight(target),
      target,
    });
  }
  return items;
}

function QuickNavigation() {
  const { i18n, t } = useTranslation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [targets, setTargets] = useState<QuickTargetItem[]>([]);
  const activatedResult = useRef(false);
  const activeOption = useRef<HTMLButtonElement>(null);
  const searchInput = useRef<HTMLInputElement>(null);
  const pendingObserver = useRef<MutationObserver>(null);
  const pendingTimeout = useRef<number>(undefined);
  const normalizedQuery = normalizeSearch(query);
  const settings = useQuery({
    enabled: open,
    queryFn: getSettings,
    queryKey: ["admin", "settings"],
    staleTime: 60_000,
  });
  const routes: QuickRouteItem[] = navigation.map((item) => ({
    detail: t("nav.admin"),
    icon: item.icon,
    id: item.to,
    kind: "route",
    label: t(item.key),
    search: normalizeSearch(`${t(item.key)} ${t("nav.admin")}`),
    to: item.to,
  }));
  const globalTargets = useMemo<QuickDeepItem[]>(() => {
    const resources = i18n.getResourceBundle(
      i18n.resolvedLanguage ?? "en",
      "translation",
    ) as Record<string, unknown>;
    const pages = navigation.map((item) => ({
      detail: t(item.key),
      scopes: item.scopes,
      to: item.to,
    }));
    const icons = new Map(navigation.map((item) => [item.to, item.icon]));
    return translationDocuments(resources, pages).map((item) => ({
      ...item,
      icon: icons.get(item.to as QuickDeepItem["to"]) as LucideIcon,
      kind: "deep",
      to: item.to as QuickDeepItem["to"],
    }));
  }, [i18n, i18n.resolvedLanguage, t]);
  const settingTargets: QuickDeepItem[] = (settings.data?.settings ?? []).map((setting) => {
    const category = t(`settings.categories.${setting.catalog.category}`);
    return {
      detail: `${t("nav.settings")} · ${category}`,
      icon: Settings,
      id: `setting-${setting.catalog.key}`,
      kind: "deep",
      label: setting.catalog.key,
      search: normalizeSearch(
        `${setting.catalog.key} ${category} ${setting.catalog.value_kind} ${t("nav.settings")}`,
      ),
      targetLabel: setting.catalog.key,
      to: "/admin/settings",
    };
  });
  const matches = (() => {
    if (normalizedQuery === "") return routes;
    const scored = [...routes, ...targets, ...globalTargets, ...settingTargets]
      .map((item) => ({ item, score: searchScore(item.search, item.label, normalizedQuery) }))
      .filter(({ score }) => score > 0)
      .sort(
        (left, right) =>
          right.score - left.score ||
          (left.item.kind === "target" ? -1 : 0) - (right.item.kind === "target" ? -1 : 0),
      );
    const unique = new Map<string, QuickItem>();
    for (const { item } of scored) {
      const key = normalizeSearch(`${item.label} ${item.detail}`);
      if (!unique.has(key)) unique.set(key, item);
    }
    return [...unique.values()].slice(0, 48);
  })();

  const stopPendingReveal = useCallback(() => {
    pendingObserver.current?.disconnect();
    pendingObserver.current = null;
    if (pendingTimeout.current !== undefined) window.clearTimeout(pendingTimeout.current);
    pendingTimeout.current = undefined;
  }, []);

  const revealDeepTarget = (targetLabel: string) => {
    stopPendingReveal();
    const content = document.getElementById("main-content") as HTMLElement;
    const needle = normalizeSearch(targetLabel.replace(/\{\{[^}]+\}\}/g, ""));
    const locate = () => {
      const candidates = [...content.querySelectorAll<HTMLElement>(searchableElements)];
      const element =
        content.querySelector<HTMLElement>(`[data-search-target="${CSS.escape(targetLabel)}"]`) ??
        candidates.find((candidate) => normalizeSearch(elementLabel(candidate)) === needle) ??
        candidates.find((candidate) => normalizeSearch(elementLabel(candidate)).includes(needle));
      if (element === undefined) return false;
      const target = elementTarget(element);
      reveal(target, elementSpotlight(target));
      stopPendingReveal();
      return true;
    };
    if (locate()) return;
    pendingObserver.current = new MutationObserver(locate);
    pendingObserver.current.observe(content, { childList: true, subtree: true });
    pendingTimeout.current = window.setTimeout(stopPendingReveal, 10_000);
  };

  const activate = (item: QuickItem) => {
    activatedResult.current = true;
    setOpen(false);
    if (item.kind === "target") {
      window.requestAnimationFrame(() => reveal(item.target, item.spotlight));
      return;
    }
    if (item.kind === "route") {
      void navigate({ to: item.to });
      return;
    }
    void navigate({ resetScroll: false, to: item.to }).then(() =>
      revealDeepTarget(item.targetLabel),
    );
  };

  const moveActive = (direction: -1 | 1) => {
    setActiveIndex((current) => {
      const next = Math.max(0, Math.min(current + direction, matches.length - 1));
      window.requestAnimationFrame(() =>
        activeOption.current?.scrollIntoView({ block: "nearest" }),
      );
      return next;
    });
  };

  useEffect(() => {
    const openFromKeyboard = (event: KeyboardEvent) => {
      if (event.key.toLocaleLowerCase() === "k" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        setOpen(true);
      }
    };
    window.addEventListener("keydown", openFromKeyboard);
    return () => window.removeEventListener("keydown", openFromKeyboard);
  }, []);

  useEffect(() => {
    if (open) {
      setTargets(pageTargets());
      setActiveIndex(0);
    }
  }, [open]);

  useEffect(() => stopPendingReveal, [stopPendingReveal]);

  return (
    <Dialog.Root
      onOpenChange={(nextOpen) => {
        setOpen(nextOpen);
      }}
      open={open}
    >
      <Dialog.Trigger asChild>
        <button
          aria-label={t("actions.search")}
          className="quick-navigation__trigger"
          type="button"
        >
          <Search aria-hidden="true" size={16} />
          <span>{t("actions.search")}</span>
          <kbd>Ctrl K</kbd>
        </button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="dialog__overlay" />
        <Dialog.Content
          aria-describedby={undefined}
          className="quick-navigation"
          onCloseAutoFocus={(event) => {
            if (activatedResult.current) {
              event.preventDefault();
              activatedResult.current = false;
            }
          }}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            searchInput.current?.focus();
            searchInput.current?.select();
          }}
        >
          <Dialog.Title className="visually-hidden">{t("nav.admin")}</Dialog.Title>
          <label className="quick-navigation__search">
            <Search aria-hidden="true" size={19} />
            <span className="visually-hidden">{t("actions.search")}</span>
            <input
              onChange={(event) => {
                setQuery(event.target.value);
                setActiveIndex(0);
              }}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  moveActive(1);
                } else if (event.key === "ArrowUp") {
                  event.preventDefault();
                  moveActive(-1);
                } else if (event.key === "Enter") {
                  const item = matches[activeIndex];
                  if (item) {
                    event.preventDefault();
                    activate(item);
                  }
                }
              }}
              placeholder={t("actions.search")}
              ref={searchInput}
              value={query}
            />
          </label>
          <div aria-label={t("nav.admin")} className="quick-navigation__results" role="listbox">
            {matches.map((item, index) => {
              const Icon = item.icon;
              return (
                <button
                  aria-selected={index === activeIndex}
                  key={item.id}
                  onClick={() => activate(item)}
                  onMouseEnter={() => setActiveIndex(index)}
                  ref={index === activeIndex ? activeOption : undefined}
                  role="option"
                  type="button"
                >
                  <Icon aria-hidden="true" size={18} strokeWidth={1.7} />
                  <span className="quick-navigation__result-copy">
                    <strong className="quick-navigation__result-label">{item.label}</strong>
                    <small className="quick-navigation__result-detail">{item.detail}</small>
                  </span>
                  {index === activeIndex ? <kbd>↵</kbd> : null}
                </button>
              );
            })}
            {matches.length === 0 ? (
              <p className="quick-navigation__empty">{t("actions.noResults")}</p>
            ) : null}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export function AdminShell() {
  const { t } = useTranslation();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => window.localStorage.getItem("comet-sidebar") === "collapsed",
  );

  const toggleCollapsed = () => {
    setCollapsed((current) => {
      const next = !current;
      window.localStorage.setItem("comet-sidebar", next ? "collapsed" : "expanded");
      return next;
    });
  };

  const logout = async () => {
    await logoutAdmin();
    await clearAdminSession();
  };

  return (
    <div className={`admin-layout ${collapsed ? "admin-layout--collapsed" : ""}`}>
      <a className="skip-link" href="#main-content">
        {t("actions.skipToContent")}
      </a>
      <aside className="sidebar">
        <div className="sidebar__brand">
          {collapsed ? (
            <button
              aria-label={t("actions.openMenu")}
              className="sidebar__brand-toggle"
              onClick={toggleCollapsed}
              title={t("actions.openMenu")}
              type="button"
            >
              <Brand compact />
            </button>
          ) : (
            <>
              <Brand />
              <button
                aria-label={t("actions.collapse")}
                className="icon-button sidebar__collapse"
                onClick={toggleCollapsed}
                type="button"
              >
                <ChevronLeft aria-hidden="true" size={17} />
              </button>
            </>
          )}
        </div>
        <Navigation collapsed={collapsed} />
        <CommunityLinks collapsed={collapsed} variant="sidebar" />
      </aside>

      <header className="topbar">
        <Dialog.Root onOpenChange={setMobileOpen} open={mobileOpen}>
          <Dialog.Trigger asChild>
            <button
              aria-label={t("actions.openMenu")}
              className="icon-button topbar__menu"
              type="button"
            >
              <Menu aria-hidden="true" />
            </button>
          </Dialog.Trigger>
          <Dialog.Portal>
            <Dialog.Overlay className="drawer__overlay" />
            <Dialog.Content aria-describedby={undefined} className="drawer__content">
              <Dialog.Title className="visually-hidden">{t("actions.openMenu")}</Dialog.Title>
              <div className="drawer__header">
                <Brand />
                <Dialog.Close asChild>
                  <button aria-label={t("actions.close")} className="icon-button" type="button">
                    <X aria-hidden="true" />
                  </button>
                </Dialog.Close>
              </div>
              <Navigation collapsed={false} onNavigate={() => setMobileOpen(false)} />
              <CommunityLinks variant="sidebar" />
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
        <Brand />
        <QuickNavigation />
        <div className="topbar__actions">
          <LanguageSelector />
          <Button aria-label={t("actions.logout")} onClick={() => void logout()} variant="ghost">
            <LogOut aria-hidden="true" size={17} />
            <span>{t("actions.logout")}</span>
          </Button>
        </div>
      </header>

      <main className="admin-content" id="main-content">
        <Outlet />
      </main>
    </div>
  );
}
