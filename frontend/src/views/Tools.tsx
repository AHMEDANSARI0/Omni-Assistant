import { useMemo, useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, PageBody, PageHeader, TextField } from "@/views/common";
import { useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import type { ToolsPayload } from "@/lib/types";

export function Tools() {
  const tools = useResource<ToolsPayload>("tools");
  const [filter, setFilter] = useState("");
  const [permission, setPermission] = useState("all");

  const permissions = useMemo(() => {
    const set = new Set<string>();
    for (const tool of tools.data?.tools ?? []) for (const item of tool.permissions) set.add(item);
    return ["all", ...Array.from(set).sort()];
  }, [tools.data]);

  const filtered = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (tools.data?.tools ?? []).filter((tool) => {
      const matchesText =
        !needle ||
        tool.id.toLowerCase().includes(needle) ||
        tool.name.toLowerCase().includes(needle) ||
        tool.description.toLowerCase().includes(needle);
      const matchesPermission = permission === "all" || tool.permissions.includes(permission);
      return matchesText && matchesPermission;
    });
  }, [tools.data, filter, permission]);

  return (
    <>
      <PageHeader
        title="Tools & Skills"
        subtitle="The deny-by-default tool registry. Every entry states the permissions it needs; the safety kernel enforces them before any call runs."
        actions={<Button onClick={() => void refresh("tools")}>Refresh</Button>}
      />
      <PageBody>
        <Panel
          title="Registry"
          subtitle={tools.data ? `${filtered.length} of ${tools.data.tools.length} tools` : "Loading"}
          actions={
            <div className="flex flex-wrap items-center gap-1">
              {permissions.slice(0, 6).map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => setPermission(item)}
                  className={`rounded px-1.5 py-[2px] text-[10px] transition ${
                    permission === item ? "bg-accent/15 text-accent" : "text-faint hover:text-ink"
                  }`}
                >
                  {item}
                </button>
              ))}
            </div>
          }
        >
          <div className="mb-2 max-w-[320px]">
            <TextField label="Filter" value={filter} onChange={setFilter} placeholder="office, browser, workspace…" />
          </div>
          <DataGate
            state={tools}
            isEmpty={() => filtered.length === 0}
            emptyDetail="No tool matches this filter."
          >
            {() => (
              <div className="max-h-[520px] overflow-y-auto pe-1">
                <table className="w-full border-collapse text-start">
                  <thead className="sticky top-0 bg-elevated/90 backdrop-blur">
                    <tr className="hud-label">
                      <th className="py-1 pe-2 font-normal">Tool</th>
                      <th className="py-1 pe-2 font-normal">Permissions</th>
                      <th className="py-1 pe-2 font-normal">Timeout</th>
                      <th className="py-1 font-normal">Provenance</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filtered.map((tool) => (
                      <tr key={tool.id} className="border-b border-line/30 align-top">
                        <td className="py-1.5 pe-2">
                          <p className="font-mono text-[11px] text-ink">{tool.id}</p>
                          <p className="max-w-[52ch] text-[10.5px] leading-relaxed text-faint">{tool.description}</p>
                        </td>
                        <td className="py-1.5 pe-2">
                          <div className="flex flex-wrap gap-1">
                            {tool.permissions.length === 0 ? <span className="text-[10px] text-faint">none</span> : null}
                            {tool.permissions.map((item) => (
                              <Badge key={item} tone={item === "write_file" || item === "subprocess" ? "warn" : "line"}>
                                {item}
                              </Badge>
                            ))}
                          </div>
                        </td>
                        <td className="py-1.5 pe-2 font-mono text-[10.5px] text-muted">{tool.timeout_seconds}s</td>
                        <td className="py-1.5 text-[10.5px] text-muted">
                          {tool.provenance}
                          {tool.network_required ? <span className="ms-1 text-warn">· network</span> : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </DataGate>
        </Panel>
      </PageBody>
    </>
  );
}
