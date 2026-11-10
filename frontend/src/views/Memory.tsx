import { useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Grid, KeyValue, PageBody, PageHeader, TextField } from "@/views/common";
import { useResource } from "@/store/hooks";
import { refresh } from "@/store/store";
import { searchMemory } from "@/lib/actions";
import { notify } from "@/store/toast";
import { shorten } from "@/lib/format";
import type { KnowledgeStatsPayload, MemorySearchPayload } from "@/lib/types";

export function Memory() {
  const knowledge = useResource<KnowledgeStatsPayload>("knowledge");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<MemorySearchPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const search = async () => {
    const needle = query.trim();
    if (!needle) {
      notify("info", "Type a query", "Memory search needs at least one term; it will not guess.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      setResult(await searchMemory(needle, 8));
    } catch (caught) {
      setResult(null);
      setError(caught instanceof Error ? caught.message : String(caught));
    }
    setBusy(false);
  };

  return (
    <>
      <PageHeader
        title="Memory"
        subtitle="Vector + keyword recall over your own documents and notes. Embeddings are computed locally; nothing leaves the machine."
        actions={<Button onClick={() => void refresh("knowledge")}>Refresh stats</Button>}
      />
      <PageBody>
        <Grid cols={2}>
          <Panel title="Recall" subtitle="GET /v1/memory/search">
            <div className="flex items-end gap-2">
              <div className="min-w-0 flex-1">
                <TextField label="Query" value={query} onChange={setQuery} placeholder="backup policy" onEnter={() => void search()} />
              </div>
              <Button tone="accent" onClick={() => void search()} busy={busy}>
                Search
              </Button>
            </div>
            <div className="mt-2 min-h-[160px]">
              {error ? (
                <p className="rounded-lg border border-danger/40 bg-danger/5 px-2 py-1.5 text-[11.5px] text-danger">{error}</p>
              ) : result === null ? (
                <p className="py-6 text-center text-[11.5px] text-faint">
                  Run a query to see scored chunks. An empty result means nothing matched — not that memory is disabled.
                </p>
              ) : (
                <>
                  <p className="mb-1 text-[10.5px] text-faint">
                    mode <span className="font-mono text-muted">{result.mode}</span> · model{" "}
                    <span className="font-mono text-muted">{result.embedding_model}</span> · chunks searched{" "}
                    <span className="font-mono text-muted">{result.searched_chunks}</span>
                  </p>
                  {result.results.length === 0 ? (
                    <p className="rounded-lg border border-line/45 bg-inset/30 px-2 py-1.5 text-[11.5px] text-faint">
                      No chunk matched “{result.query}”.
                    </p>
                  ) : (
                    <div className="space-y-1.5">
                      {result.results.map((hit, index) => (
                        <div key={index} className="rounded-lg border border-line/45 bg-inset/30 p-2">
                          <div className="flex items-center justify-between gap-2">
                            <p className="truncate text-[11.5px] font-medium text-ink">
                              {String(hit.source_name ?? hit.document_id ?? "result")}
                            </p>
                            <Badge tone="accent">{typeof hit.score === "number" ? hit.score.toFixed(3) : "—"}</Badge>
                          </div>
                          <p className="mt-0.5 text-[10.5px] leading-relaxed text-faint">{shorten(String(hit.text ?? ""), 260)}</p>
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
            </div>
          </Panel>

          <div className="space-y-2.5">
            <Panel title="Index" subtitle="Live counters from /v1/knowledge/stats">
              <DataGate state={knowledge} onRetry={() => void refresh("knowledge")}>
                {(data) => (
                  <>
                    <KeyValue label="Documents" value={data.knowledge.documents} />
                    <KeyValue label="Chunks" value={data.knowledge.chunks} />
                    <KeyValue label="Notes indexed" value={data.knowledge.memories.note} />
                    <KeyValue label="Workflow memories" value={data.knowledge.memories.workflow} />
                    <KeyValue label="Embedded notes" value={data.knowledge.embedded_notes} />
                    <KeyValue label="Active model" value={data.knowledge.active_embedding_model} />
                    <KeyValue label="Runs locally" value={data.knowledge.embedding_local ? "yes" : "no"} />
                    <p className="mt-2 text-[10px] text-faint">
                      Supported: {data.knowledge.supported_suffixes.join(" ")}
                    </p>
                  </>
                )}
              </DataGate>
            </Panel>
            <Panel title="What memory does not do" subtitle="Stated rather than implied">
              <ul className="space-y-1 text-[11px] leading-relaxed text-faint">
                <li>· It does not invent recall: an empty index returns an empty result set.</li>
                <li>· Notes and workflow memories are written by the CLI and the engine, not by this screen.</li>
                <li>· Cross-device recall requires encrypted sync to be enabled and paired first.</li>
              </ul>
            </Panel>
          </div>
        </Grid>
      </PageBody>
    </>
  );
}
