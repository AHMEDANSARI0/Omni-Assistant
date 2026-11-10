import { useEffect, useState } from "react";
import { Badge, DataGate, Panel } from "@/components/Panel";
import { Button, Grid, PageBody, PageHeader, Rows, Row, TextField } from "@/views/common";
import { useResource } from "@/store/hooks";
import { ensureFresh, refresh } from "@/store/store";
import { askKnowledge, ingestKnowledge, removeKnowledgeDocument, searchKnowledge } from "@/lib/actions";
import { attempt, notify } from "@/store/toast";
import { shorten } from "@/lib/format";
import type { KnowledgeDocumentsPayload } from "@/lib/types";

export function Knowledge() {
  const documents = useResource<KnowledgeDocumentsPayload>("knowledgeDocs");
  const [path, setPath] = useState("");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<{ answer?: string; citations?: unknown[] } | null>(null);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<unknown[] | null>(null);
  const [busy, setBusy] = useState<null | "ingest" | "ask" | "search">(null);

  useEffect(() => {
    ensureFresh("knowledgeDocs", 20000);
  }, []);

  const ingest = async () => {
    if (!path.trim()) return;
    setBusy("ingest");
    const result = await attempt("Document ingested", () => ingestKnowledge(path.trim()));
    setBusy(null);
    if (result) {
      setPath("");
      await refresh("knowledgeDocs");
      await refresh("knowledge");
      await refresh("events");
    }
  };

  const ask = async () => {
    if (!question.trim()) return;
    setBusy("ask");
    try {
      setAnswer(await askKnowledge(question.trim()));
    } catch (error) {
      setAnswer(null);
      notify("error", "Question failed", error instanceof Error ? error.message : String(error));
    }
    setBusy(null);
  };

  const search = async () => {
    if (!query.trim()) return;
    setBusy("search");
    try {
      const payload = await searchKnowledge(query.trim(), 8);
      setHits((payload.results as unknown[]) ?? []);
    } catch (error) {
      setHits(null);
      notify("error", "Search failed", error instanceof Error ? error.message : String(error));
    }
    setBusy(null);
  };

  return (
    <>
      <PageHeader
        title="Knowledge Base"
        subtitle="Ingest local files, search chunks, or ask a grounded question. Ingestion paths are resolved inside the workspace sandbox."
        actions={<Button onClick={() => void refresh("knowledgeDocs")}>Refresh</Button>}
      />
      <PageBody>
        <Grid cols={2}>
          <Panel title="Documents" subtitle={documents.data ? `${documents.data.total} indexed` : "Loading"} className="min-h-[280px]">
            <DataGate state={documents} isEmpty={(data) => data.documents.length === 0} emptyDetail="The index is empty. Ingest a markdown, text, CSV, PDF or Office file to start.">
              {(data) => (
                <div className="max-h-[380px] overflow-y-auto pe-1">
                  <Rows>
                    {data.documents.map((document) => (
                      <Row key={document.id}>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-[11.5px] text-ink">{document.title || document.source_name}</p>
                          <p className="truncate font-mono text-[10px] text-faint">
                            #{document.id} · {document.kind} · {document.chunk_count} chunks · {document.characters} chars
                          </p>
                        </div>
                        <Badge tone="accent">{document.embedding_model}</Badge>
                        <Button
                          tone="danger"
                          onClick={async () => {
                            await attempt("Document removed", () => removeKnowledgeDocument(document.id));
                            await refresh("knowledgeDocs");
                            await refresh("knowledge");
                          }}
                        >
                          ✕
                        </Button>
                      </Row>
                    ))}
                  </Rows>
                </div>
              )}
            </DataGate>
          </Panel>

          <div className="space-y-2.5">
            <Panel title="Ingest" subtitle="Path inside the workspace or an absolute path the sandbox allows">
              <div className="flex items-end gap-2">
                <div className="min-w-0 flex-1">
                  <TextField label="File path" value={path} onChange={setPath} placeholder="D:\\omni-assistant\\workspace\\notes.md" mono onEnter={() => void ingest()} />
                </div>
                <Button tone="accent" onClick={() => void ingest()} busy={busy === "ingest"} disabled={!path.trim()}>
                  Ingest
                </Button>
              </div>
            </Panel>

            <Panel title="Ask" subtitle="Answers cite retrieved chunks; if the model is unavailable the engine says so">
              <div className="flex items-end gap-2">
                <div className="min-w-0 flex-1">
                  <TextField label="Question" value={question} onChange={setQuestion} placeholder="What does the release note say about the interface?" onEnter={() => void ask()} />
                </div>
                <Button tone="violet" onClick={() => void ask()} busy={busy === "ask"} disabled={!question.trim()}>
                  Ask
                </Button>
              </div>
              {answer ? (
                <div className="mt-2 rounded-lg border border-line/45 bg-inset/30 p-2">
                  <p className="text-[11.5px] leading-relaxed text-ink">{answer.answer ?? "The engine returned no answer field."}</p>
                  {answer.citations && answer.citations.length > 0 ? (
                    <p className="mt-1 text-[10px] text-faint">{answer.citations.length} citation(s) returned.</p>
                  ) : null}
                </div>
              ) : null}
            </Panel>

            <Panel title="Search chunks" subtitle="GET /v1/knowledge/search">
              <div className="flex items-end gap-2">
                <div className="min-w-0 flex-1">
                  <TextField label="Query" value={query} onChange={setQuery} placeholder="backup" onEnter={() => void search()} />
                </div>
                <Button onClick={() => void search()} busy={busy === "search"} disabled={!query.trim()}>
                  Search
                </Button>
              </div>
              {hits ? (
                hits.length === 0 ? (
                  <p className="mt-2 text-[11px] text-faint">No chunk matched.</p>
                ) : (
                  <ul className="mt-2 max-h-[180px] space-y-1 overflow-y-auto">
                    {hits.map((hit, index) => (
                      <li key={index} className="rounded border border-line/40 bg-inset/25 px-2 py-1 font-mono text-[10px] text-muted">
                        {shorten(JSON.stringify(hit), 180)}
                      </li>
                    ))}
                  </ul>
                )
              ) : null}
            </Panel>
          </div>
        </Grid>
      </PageBody>
    </>
  );
}
