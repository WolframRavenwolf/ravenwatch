(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS) return;

  const { React, fetchJSON, api } = SDK;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const NAME = "ravenwatch-ops";
  const scriptUrl = document.currentScript && document.currentScript.src ? document.currentScript.src : "";
  const ASSET_BASE = scriptUrl ? new URL("../assets/", scriptUrl).href : `/dashboard-plugins/${NAME}/assets/`;
  const AMY_BADGE_URL = `${ASSET_BASE}amy-ravenwolf-pixel-badge.png`;

  function h(type, props) {
    const children = Array.prototype.slice.call(arguments, 2).flat();
    return React.createElement(type, props || null, ...children);
  }

  function scoreColor(score) {
    if (score >= 90) return "#19c37d";
    if (score >= 70) return "#f59e0b";
    return "#f43f5e";
  }

  function fmt(n, digits) {
    if (typeof n !== "number") n = Number(n || 0);
    return n.toLocaleString(undefined, { maximumFractionDigits: digits == null ? 0 : digits });
  }

  const ROUTES = ["/ravenwatch", "/sessions", "/analytics", "/logs", "/cron", "/skills", "/config", "/env"];

  function routeHref(path) {
    const current = window.location.pathname || "/";
    for (const known of ROUTES.sort(function (a, b) { return b.length - a.length; })) {
      if (current === known || current.endsWith(known) || current.indexOf(known + "/") >= 0) {
        const idx = current.lastIndexOf(known);
        return current.slice(0, idx) + path;
      }
    }
    return current.replace(/\/$/, "") + path;
  }

  function endpoint(path, demo) {
    return `/api/plugins/${NAME}${path}${demo ? "?demo=true" : ""}`;
  }

  function fallbackSummary() {
    return Promise.allSettled([
      api.getStatus(),
      api.getSessions(20, 0),
      api.getLogs({ file: "errors", lines: 80 }),
      api.getCronJobs ? api.getCronJobs() : Promise.resolve([]),
    ]).then(function (results) {
      const status = results[0].status === "fulfilled" ? results[0].value : {};
      const sessionsPayload = results[1].status === "fulfilled" ? results[1].value : { sessions: [] };
      const logs = results[2].status === "fulfilled" ? results[2].value.lines || [] : [];
      const cron = results[3].status === "fulfilled" ? results[3].value || [] : [];
      const errors = logs.filter(function (l) { return /ERROR|CRITICAL|Traceback|Exception/i.test(l); }).length;
      const warnings = logs.filter(function (l) { return /WARN|TIMEOUT/i.test(l); }).length;
      const score = Math.max(0, 100 - Math.min(55, errors * 8) - Math.min(25, warnings * 2));
      const level = score >= 90 ? "ok" : score >= 70 ? "watch" : "critical";
      const label = score >= 90 ? "Operational" : score >= 70 ? "Watch" : "Attention";
      const incidents = logs.slice(-6).reverse().map(function (line) {
        const sev = /ERROR|CRITICAL|Traceback|Exception/i.test(line) ? "error" : "warning";
        return { severity: sev, title: line.slice(0, 150), count: 1, sources: ["errors.log"], sample: line, href: "/logs" };
      });
      return {
        generated_at: Date.now() / 1000,
        mode: "fallback",
        health_score: score,
        level: level,
        label: label,
        sass: score >= 90 ? "Everything's behaving. Suspicious." : "A few things need supervision.",
        gateway_state: status.gateway_state || (status.gateway_running ? "running" : "unknown"),
        metrics: {
          active_sessions: status.active_sessions || 0,
          sessions_24h: (sessionsPayload.sessions || []).length,
          messages_24h: (sessionsPayload.sessions || []).reduce(function (a, s) { return a + (s.message_count || 0); }, 0),
          tool_calls_24h: (sessionsPayload.sessions || []).reduce(function (a, s) { return a + (s.tool_call_count || 0); }, 0),
          errors: errors,
          warnings: warnings,
          cron_jobs: Array.isArray(cron) ? cron.length : 0,
          cron_enabled: Array.isArray(cron) ? cron.filter(function (j) { return j.enabled !== false; }).length : 0,
          estimated_cost_24h: 0,
          platforms: status.gateway_platforms ? Object.keys(status.gateway_platforms).length : 0,
        },
        top_models: [],
        incidents: incidents,
        recommendations: incidents.length ? [{ title: "Inspect Logs", detail: "Recent error output detected.", href: "/logs" }] : [{ title: "Keep shipping", detail: "No obvious incidents detected.", href: "/sessions" }],
      };
    });
  }

  function loadSummary(demo) {
    return fetchJSON(endpoint("/summary", demo)).catch(function () { return fallbackSummary(); });
  }

  function loadBriefing(demo) {
    return fetchJSON(endpoint("/briefing", demo)).then(function (r) { return r.briefing; }).catch(function () {
      return loadSummary(demo).then(function (data) {
        const m = data.metrics || {};
        const lines = [
          "Ravenwatch Operator Briefing",
          `Status: ${data.label} (${data.health_score}/100)`,
          `Amy says: ${data.sass}`,
          "",
          `Active sessions: ${m.active_sessions || 0}`,
          `Errors: ${m.errors || 0}`,
          `Warnings: ${m.warnings || 0}`,
        ];
        (data.incidents || []).slice(0, 5).forEach(function (inc) {
          lines.push(`- [${inc.severity}] ${inc.title}`);
        });
        return lines.join("\n");
      });
    });
  }

  function useRavenwatch(demo, interval) {
    const [data, setData] = useState(null);
    const [brief, setBrief] = useState("");
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);
    const refresh = useCallback(function () {
      setLoading(true);
      setError(null);
      return Promise.all([loadSummary(demo), loadBriefing(demo)])
        .then(function (vals) { setData(vals[0]); setBrief(vals[1]); })
        .catch(function (e) { setError(String(e && e.message || e)); })
        .finally(function () { setLoading(false); });
    }, [demo]);
    useEffect(function () {
      let cancelled = false;
      refresh();
      if (!interval) return function () { cancelled = true; };
      const id = setInterval(function () { if (!cancelled) refresh(); }, interval);
      return function () { cancelled = true; clearInterval(id); };
    }, [refresh, interval]);
    return { data: data, brief: brief, loading: loading, error: error, refresh: refresh };
  }

  function MetricCard(props) {
    return h("div", { className: "rw-card rw-kpi" },
      h("div", { className: "rw-kpi-value" }, props.value),
      h("div", { className: "rw-kpi-label" }, props.label)
    );
  }

  function IncidentList(props) {
    const items = props.items || [];
    if (!items.length) {
      return h("div", { className: "rw-muted" }, "No clustered incidents detected. Suspiciously civilized.");
    }
    return h("div", { className: "rw-list" }, items.slice(0, props.limit || 6).map(function (inc, idx) {
      const target = routeHref(inc.href || "/logs");
      return h("a", { className: `rw-item rw-item-link rw-sev-${inc.severity || "info"}`, href: target, key: idx, title: "Open the dashboard page for this incident" },
        h("span", { className: "rw-dot" }),
        h("div", null,
          h("div", { className: "rw-item-title" }, inc.title || "Runtime incident"),
          h("div", { className: "rw-item-detail" }, `${inc.count || 1}× · ${(inc.sources || []).join(", ") || "runtime"}`)
        ),
        h("span", { className: "rw-badge" }, inc.severity || "open")
      );
    }));
  }

  function Recommendations(props) {
    const items = props.items || [];
    return h("div", { className: "rw-list" }, items.map(function (rec, idx) {
      return h("a", { className: "rw-item rw-item-link rw-sev-info", href: routeHref(rec.href || "/ravenwatch"), key: idx, title: "Open recommended dashboard page" },
        h("span", { className: "rw-dot" }),
        h("div", null,
          h("div", { className: "rw-item-title" }, rec.title),
          h("div", { className: "rw-item-detail" }, rec.detail)
        ),
        h("span", { className: "rw-badge" }, "open")
      );
    }));
  }

  function AmySeal() {
    return h("div", { className: "rw-amy-seal", title: "Amy Ravenwolf — Head Bot In Charge" },
      h("img", {
        className: "rw-amy-avatar rw-amy-avatar-image",
        src: AMY_BADGE_URL,
        alt: "Amy Ravenwolf pixel badge — Head Bot In Charge",
        loading: "lazy",
        decoding: "async"
      }),
      h("div", { className: "rw-amy-name" }, "Amy Ravenwolf"),
      h("div", { className: "rw-amy-role" }, "Head Bot In Charge")
    );
  }

  function RavenwatchPage() {
    const [demo, setDemo] = useState(false);
    const [copied, setCopied] = useState(false);
    const state = useRavenwatch(demo, 0);
    const data = state.data;
    const metrics = data && data.metrics || {};
    const color = data ? scoreColor(data.health_score) : "#19c37d";

    function copyBrief() {
      const text = state.brief || "Ravenwatch briefing unavailable.";
      const done = function () { setCopied(true); setTimeout(function () { setCopied(false); }, 1600); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(done);
      } else {
        done();
      }
    }

    if (state.loading && !data) {
      return h("div", { className: "rw-root rw-page" }, h("div", { className: "rw-card" }, "Ravenwatch is waking the ravens…"));
    }

    return h("div", { className: "rw-root rw-page" },
      h("section", { className: "rw-hero", style: { "--rw-score": data ? data.health_score / 100 : 1, "--rw-score-color": color } },
        h("div", { className: "rw-hero-top" },
          h("div", { className: "rw-hero-copy" },
            h("div", { className: "rw-kicker" }, "Ravenwatch // Amy Ops Console"),
            h("h1", { className: "rw-title" }, "Operator Control"),
            h("p", { className: "rw-subtitle" }, data ? data.sass : "Health, incidents, sessions, cron, and copyable briefings in one cockpit. It doesn't just make Hermes look hotter — it tells you what’s on fire.")
          ),
          h("div", { className: "rw-hero-status" },
            h(AmySeal),
            h("div", { className: "rw-score-ring" },
              h("div", { className: "rw-score-inner" },
                h("div", { className: "rw-score-number" }, data ? data.health_score : "—"),
                h("div", { className: "rw-score-label" }, data ? data.label : "Loading")
              )
            )
          )
        ),
        h("div", { className: "rw-toolbar" },
          h("button", { className: "rw-button rw-button-primary", onClick: state.refresh }, "Refresh"),
          h("button", { className: "rw-button", onClick: function () { setDemo(!demo); } }, demo ? "Live data" : "Demo mode"),
          h("button", { className: "rw-button", onClick: copyBrief }, copied ? "Copied" : "Copy briefing"),
          h("span", { className: "rw-badge" }, data ? data.mode : "loading")
        )
      ),
      state.error ? h("div", { className: "rw-card rw-error-box" }, state.error) : null,
      h("section", { className: "rw-grid" },
        h(MetricCard, { value: fmt(metrics.active_sessions), label: "active sessions" }),
        h(MetricCard, { value: fmt(metrics.sessions_24h), label: "sessions / 24h" }),
        h(MetricCard, { value: fmt(metrics.tool_calls_24h), label: "tool calls / 24h" }),
        h(MetricCard, { value: `${fmt(metrics.errors)} / ${fmt(metrics.warnings)}`, label: "errors / warnings" }),
        h("div", { className: "rw-card rw-span-7", id: "attention-board" },
          h("h3", null, "Attention Board"),
          h(IncidentList, { items: data ? data.incidents : [], limit: 7 })
        ),
        h("div", { className: "rw-card rw-span-5" },
          h("h3", null, "Recommended Actions"),
          h(Recommendations, { items: data ? data.recommendations : [] }),
          h("h3", { style: { marginTop: "1rem" } }, "Top Models"),
          h("div", { className: "rw-models" }, (data && data.top_models || []).map(function (m, idx) {
            return h("span", { className: "rw-model", key: idx }, `${m.name} ×${m.count}`);
          }))
        ),
        h("div", { className: "rw-card rw-span-12" },
          h("h3", null, "Copyable Operator Briefing"),
          h("div", { className: "rw-brief" }, state.brief || "No briefing yet.")
        )
      )
    );
  }

  function HeaderChip() {
    const state = useRavenwatch(false, 20000);
    const data = state.data;
    const level = data ? data.level : "ok";
    return h("a", { className: `rw-root rw-header-chip rw-chip-${level}`, href: routeHref("/ravenwatch"), title: "Open Ravenwatch Ops" },
      h("span", { className: "rw-chip-dot" }),
      h("span", null, data ? `RW ${data.health_score}` : "RW …")
    );
  }

  function HeaderBanner() {
    const state = useRavenwatch(false, 30000);
    const data = state.data;
    if (!data || data.health_score >= 90) return null;
    const top = data.incidents && data.incidents[0];
    const target = "/ravenwatch#attention-board";
    return h("a", { className: "rw-root rw-banner rw-banner-link", href: routeHref(target), title: "Open Ravenwatch attention board" },
      h("span", null, `Ravenwatch ${data.label}: ${top ? top.title : data.sass}`),
      h("span", { className: "rw-banner-action" }, "Open Ravenwatch →")
    );
  }

  function SidebarSlot() {
    const state = useRavenwatch(false, 30000);
    const data = state.data || {};
    const m = data.metrics || {};
    return h("div", { className: "rw-root rw-sidebar" },
      h(AmySeal),
      h("div", { className: "rw-side-title" }, "Ravenwatch"),
      h("div", { className: "rw-side-panel" },
        h("div", { className: "rw-side-stat" }, h("span", null, "Health"), h("strong", null, data.health_score == null ? "…" : data.health_score + "/100")),
        h("div", { className: "rw-side-stat" }, h("span", null, "Mode"), h("strong", null, data.mode || "live")),
        h("div", { className: "rw-side-stat" }, h("span", null, "Gateway"), h("strong", null, data.gateway_state || "unknown")),
        h("div", { className: "rw-side-stat" }, h("span", null, "Active"), h("strong", null, fmt(m.active_sessions))),
        h("div", { className: "rw-side-stat" }, h("span", null, "Tools 24h"), h("strong", null, fmt(m.tool_calls_24h)))
      ),
      h("div", { className: "rw-side-panel" },
        h("div", { className: "rw-side-title" }, "Top Incidents"),
        h(IncidentList, { items: data.incidents || [], limit: 3 })
      )
    );
  }

  function FooterSlot() {
    return h("span", { className: "rw-root rw-footer" }, "RAVENWATCH // AMY OPS // NO BULLSHIT TELEMETRY");
  }

  function OverlaySlot() {
    return h("div", { className: "rw-root rw-overlay", "aria-hidden": true });
  }

  PLUGINS.register(NAME, RavenwatchPage);
  if (PLUGINS.registerSlot) {
    PLUGINS.registerSlot(NAME, "header-right", HeaderChip);
    PLUGINS.registerSlot(NAME, "header-banner", HeaderBanner);
    PLUGINS.registerSlot(NAME, "sidebar", SidebarSlot);
    PLUGINS.registerSlot(NAME, "footer-right", FooterSlot);
    PLUGINS.registerSlot(NAME, "overlay", OverlaySlot);
  }
})();
