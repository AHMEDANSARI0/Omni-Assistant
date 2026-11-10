import { useEffect, useState } from "react";
import { DataGate, Panel } from "@/components/Panel";
import { Button, Grid, KeyValue, KeyValuePanel, PageBody, PageHeader } from "@/views/common";
import { useResource } from "@/store/hooks";
import { ensureFresh, refresh } from "@/store/store";
import { setLocale } from "@/lib/actions";
import { attempt } from "@/store/toast";
import { applyAutoDirection, useUi } from "@/store/ui";
import type { A11yStatus, I18nStatus, ProfilesPayload, SettingsPayload } from "@/lib/types";

export function Settings() {
  const settings = useResource<SettingsPayload>("settings");
  const a11y = useResource<A11yStatus>("a11y");
  const i18n = useResource<I18nStatus>("i18n");
  const profiles = useResource<ProfilesPayload>("profiles");
  const { ui, setTheme, setMotion, setDirection, toggleSidebar } = useUi();
  const [filter, setFilter] = useState("");

  useEffect(() => {
    ensureFresh("settings", 60000);
    ensureFresh("a11y", 60000);
    ensureFresh("i18n", 60000);
    ensureFresh("profiles", 60000);
  }, []);

  const rows = Object.entries(settings.data?.settings ?? {}).filter(([key, value]) =>
    !filter.trim() ? true : `${key} ${value}`.toLowerCase().includes(filter.trim().toLowerCase()),
  );

  return (
    <>
      <PageHeader
        title="Settings"
        subtitle="Display preferences are stored in this browser; engine settings come from the environment and are read-only here, which is why they are shown with their real values instead of fake toggles."
        actions={<Button onClick={() => { void refresh("settings"); void refresh("a11y"); }}>Reload</Button>}
      />
      <PageBody>
        <Grid cols={2}>
          <Panel title="Display" subtitle="Applies instantly and persists in omni.ui.v1">
            <div className="space-y-2">
              <div className="flex items-center justify-between rounded-lg border border-line/50 bg-inset/30 px-2.5 py-1.5">
                <span className="text-[11.5px] text-ink">Theme</span>
                <div className="flex rounded-md border border-line/60 p-0.5">
                  {(["dark", "light"] as const).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => setTheme(mode)}
                      className={`rounded px-2 py-0.5 text-[11px] capitalize transition ${ui.theme === mode ? "bg-accent/15 text-accent" : "text-faint hover:text-ink"}`}
                    >
                      {mode}
                    </button>
                  ))}
                </div>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-line/50 bg-inset/30 px-2.5 py-1.5">
                <span className="text-[11.5px] text-ink">Motion</span>
                <div className="flex rounded-md border border-line/60 p-0.5">
                  {(["system", "full", "reduce"] as const).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => setMotion(mode)}
                      className={`rounded px-2 py-0.5 text-[11px] capitalize transition ${ui.motion === mode ? "bg-accent/15 text-accent" : "text-faint hover:text-ink"}`}
                    >
                      {mode}
                    </button>
                  ))}
                </div>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-line/50 bg-inset/30 px-2.5 py-1.5">
                <span className="text-[11.5px] text-ink">Text direction</span>
                <div className="flex rounded-md border border-line/60 p-0.5">
                  {(["auto", "ltr", "rtl"] as const).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => {
                        setDirection(mode);
                        // "auto" re-resolves from the engine's locale report.
                        if (mode === "auto" && a11y.data) applyAutoDirection(a11y.data.direction);
                      }}
                      className={`rounded px-2 py-0.5 text-[11px] uppercase transition ${ui.direction === mode ? "bg-accent/15 text-accent" : "text-faint hover:text-ink"}`}
                    >
                      {mode}
                    </button>
                  ))}
                </div>
              </div>
              <div className="flex items-center justify-between rounded-lg border border-line/50 bg-inset/30 px-2.5 py-1.5">
                <span className="text-[11.5px] text-ink">Sidebar</span>
                <Button onClick={toggleSidebar}>{ui.sidebarCollapsed ? "Expand" : "Collapse"}</Button>
              </div>
              <p className="text-[10px] leading-relaxed text-faint">
                “System” follows the operating system's reduced-motion setting. Keyboard: Ctrl+K palette, Ctrl+B sidebar, Esc closes overlays,
                Enter sends an instruction.
              </p>
            </div>
          </Panel>

          <KeyValuePanel title="Accessibility & locale" subtitle="From /v1/a11y/status" state={a11y}>
            {(data) => (
              <>
                <KeyValue label="Locale" value={`${data.locale} (${data.language_name})`} />
                <KeyValue label="Direction" value={data.direction.toUpperCase()} />
                <KeyValue label="Scale" value={data.scale} />
                <KeyValue label="High contrast (engine)" value={data.high_contrast ? "on" : "off"} />
                <KeyValue label="Reduced motion (engine)" value={data.reduce_motion ? "on" : "off"} />
                <KeyValue label="Keyboard shortcuts" value={data.keyboard_shortcuts ? "on" : "off"} />
                <KeyValue label="Translation coverage" value={`${data.coverage.translated_keys}/${data.coverage.total_keys} (${data.coverage.percent}%)`} />
                <div className="mt-2">
                  <p className="hud-label mb-1">Switch locale (POST /v1/i18n/locale)</p>
                  <div className="flex flex-wrap gap-1">
                    {data.locales.map((locale) => (
                      <button
                        key={locale.locale}
                        type="button"
                        onClick={async () => {
                          await attempt(`Locale set to ${locale.name}`, () => setLocale(locale.locale));
                          await refresh("a11y");
                          await refresh("i18n");
                        }}
                        className={`rounded border px-1.5 py-[2px] text-[10.5px] transition ${
                          data.locale === locale.locale ? "border-accent/50 bg-accent/10 text-accent" : "border-line/60 text-muted hover:border-accent/45"
                        }`}
                      >
                        {locale.name} {locale.direction === "rtl" ? "RTL" : ""}
                      </button>
                    ))}
                  </div>
                  <p className="mt-1 text-[10px] text-faint">
                    The engine stores the locale for its own output; this React shell is written in English and does not machine-translate itself. What it does do is mirror: choosing an RTL locale (or RTL under Display → Text direction) flips the whole layout, sidebar included.
                  </p>
                </div>
              </>
            )}
          </KeyValuePanel>

          <Panel title="Engine settings" subtitle={settings.data ? `${Object.keys(settings.data.settings).length} keys · read-only view` : "Loading"}>
            <input
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="Filter keys…"
              className="mb-2 w-full rounded-lg border border-line/60 bg-inset/50 px-2.5 py-1.5 text-[11.5px] text-ink placeholder:text-faint focus:border-accent/60 focus:outline-none"
            />
            <DataGate state={settings}>
              {() => (
                <div className="max-h-[360px] overflow-y-auto pe-1">
                  {rows.map(([key, value]) => (
                    <KeyValue key={key} label={key.replace(/_/g, " ")} value={value} />
                  ))}
                </div>
              )}
            </DataGate>
          </Panel>

          <KeyValuePanel title="Profiles & i18n" subtitle="Local configuration profiles" state={profiles}>
            {(data) => (
              <>
                <KeyValue label="Active profile" value={data.active ?? "none"} />
                <KeyValue label="Profiles" value={data.profiles.length} />
                {data.profiles.slice(0, 8).map((profile) => (
                  <KeyValue key={profile.name} label={profile.name} value="saved" />
                ))}
                {i18n.data ? <KeyValue label="Engine locale" value={`${i18n.data.locale} · ${i18n.data.name}`} /> : null}
                <p className="mt-2 text-[10px] leading-relaxed text-faint">
                  Profiles are created from the CLI (<span className="font-mono">pc-agent profiles save</span>) so a UI action can never silently
                  change the environment a task runs in.
                </p>
              </>
            )}
          </KeyValuePanel>
        </Grid>
      </PageBody>
    </>
  );
}
