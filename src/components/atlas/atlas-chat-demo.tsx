import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { ArrowLeft, MessageCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Conversation,
  ConversationContent,
  ConversationScrollButton,
} from "@/components/ai-elements/conversation";
import { Message, MessageContent, MessageResponse } from "@/components/ai-elements/message";
import {
  PromptInput,
  PromptInputBody,
  PromptInputFooter,
  PromptInputTextarea,
  PromptInputSubmit,
} from "@/components/ai-elements/prompt-input";
import { allEdges, diseases, getNode } from "@/lib/atlas";
import { askChat, stripCitations, useLocalApi } from "@/lib/atlas-api";
import { loadAtlasGraph } from "@/lib/atlas-db";

type Turn = {
  role: "user" | "assistant";
  text: string;
  cited?: number;
  outOfScope?: boolean;
  failed?: boolean;
};
const sampleSuggestions = [
  "What does the dashed line mean?",
  "Explain the strongest connection",
  "Can a treatment transfer?",
];
const liveSuggestions = (name: string) => [
  `Is there a clinical trial for ${name} outside the US?`,
  `Which patient groups work on ${name}?`,
  `What causes ${name}?`,
];

/** Footnote under a live answer: how many cited records it rests on, or why it could not answer. */
function Grounding({ turn }: { turn: Turn }) {
  if (turn.role !== "assistant" || turn.cited === undefined) return null;
  if (turn.outOfScope)
    return (
      <p className="chat-grounding">
        Outside what the atlas records can answer, so no answer was made up.
      </p>
    );
  return (
    <p className="chat-grounding">
      Based on {turn.cited} cited atlas record{turn.cited === 1 ? "" : "s"}.
    </p>
  );
}

function answer(question: string, diseaseId: string) {
  const condition = diseases.find((d) => d.id === diseaseId);
  const links = allEdges.filter((edge) => edge.source === diseaseId || edge.target === diseaseId);
  const q = question.toLowerCase();
  if (/dashed|dotted|solid|legend|line/.test(q))
    return "In this demonstration, a solid line marks a curated record; a dashed line marks an AI-extracted record; a dotted line marks an inferred relationship that needs review. The amber line is a caution, not evidence that a treatment transfers. Select a line on the chart to inspect its record.";
  if (/treatment|therapy|medicine|transfer|cure/.test(q))
    return "No. A shared symptom or biological process does not establish that a treatment works across conditions. This demonstration does not contain verified treatment-transfer evidence. Review the source record and discuss clinical decisions with a qualified care team.";
  if (/strong|connect|relationship|evidence|source/.test(q)) {
    const edge =
      links.find((item) => item.evidence_type === "curated" && !item.negated) ??
      links.find((item) => !item.negated);
    if (!edge)
      return `There are no mapped connections for ${condition?.plain_label ?? "this condition"} in this demonstration. I cannot infer one from the chart.`;
    const other = getNode(edge.source === diseaseId ? edge.target : edge.source);
    return `One illustrated connection from ${condition?.plain_label ?? "this condition"} is to ${other?.plain_label ?? "another condition"}: ${edge.plain_explanation} The line is labeled “${edge.relationship_label}” and classified as ${edge.evidence_type === "curated" ? "curated" : edge.evidence_type === "extracted" ? "AI-extracted" : "inferred"}. Its source is an illustrative mock record, not a verified scientific citation.`;
  }
  return `This test assistant only explains the sample chart for ${condition?.plain_label ?? "the selected condition"}. It cannot verify medical claims or answer questions beyond the displayed records. Try asking about a connection, a line style, or treatment transfer.`;
}

export function AtlasChatDemo({
  diseaseId,
  variant = "page",
}: {
  diseaseId: string;
  variant?: "page" | "drawer";
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const [thinking, setThinking] = useState(false);
  const [liveName, setLiveName] = useState<{ label: string; short: string } | null>(null);
  const mock = diseases.find((d) => d.id === diseaseId);
  const condition =
    useLocalApi && liveName ? { label: liveName.label, plain_label: liveName.short } : mock;
  const suggestions = useLocalApi
    ? liveSuggestions(liveName?.short ?? "this condition")
    : sampleSuggestions;
  useEffect(() => {
    input.current?.focus();
  }, []);
  useEffect(() => {
    if (!useLocalApi) return;
    let active = true;
    setTurns([]);
    loadAtlasGraph()
      .then((g) => {
        const d = g.diseases.find((x) => x.id === diseaseId);
        if (active && d) setLiveName({ label: d.label, short: d.plain_label });
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [diseaseId]);
  async function askLive(question: string, history: Turn[]) {
    setThinking(true);
    try {
      const messages = [
        ...history.filter((t) => !t.failed),
        { role: "user" as const, text: question },
      ].map((t) => ({ role: t.role, content: t.text }));
      const r = await askChat(diseaseId, messages);
      const out = !r.grounded || r.out_of_scope;
      setTurns((current) => [
        ...current,
        {
          role: "assistant",
          text: stripCitations(r.answer),
          cited: r.citations.length,
          outOfScope: out,
        },
      ]);
    } catch (e) {
      setTurns((current) => [
        ...current,
        {
          role: "assistant",
          text: `The atlas assistant could not answer: ${e instanceof Error ? e.message : String(e)}`,
          failed: true,
        },
      ]);
    } finally {
      setThinking(false);
      input.current?.focus();
    }
  }
  function ask(question: string) {
    const trimmed = question.trim();
    if (!trimmed || thinking) return;
    if (useLocalApi) {
      const history = turns;
      setTurns((current) => [...current, { role: "user", text: trimmed }]);
      setText("");
      void askLive(trimmed, history);
      return;
    }
    setTurns((current) => [
      ...current,
      { role: "user", text: trimmed },
      { role: "assistant", text: answer(trimmed, diseaseId) },
    ]);
    setText("");
    input.current?.focus();
  }
  const note = useLocalApi
    ? "Answers from cited atlas records · not medical advice · not saved"
    : "Test answers · not medical advice · not saved";
  const intro = useLocalApi
    ? `Answers come only from the sourced atlas records for ${condition?.plain_label ?? "this condition"}.`
    : "Answers use the illustrative records shown in the map.";
  const thinkingRow = thinking ? (
    <div className="chat-waiting" role="status">
      Thinking… checking the atlas records
    </div>
  ) : null;
  const liveLink = useLocalApi ? null : (
    <Link to="/chat" search={{ diseaseId, demo: false }} className="chat-live-link">
      Use live assistant (sign-in) →
    </Link>
  );
  const chips = (
    <div className="chat-suggestions" aria-label="Sample questions">
      {suggestions.map((suggestion) => (
        <Button
          key={suggestion}
          type="button"
          variant="outline"
          size="sm"
          onClick={() => ask(suggestion)}
        >
          {suggestion}
        </Button>
      ))}
    </div>
  );
  if (variant === "drawer")
    return (
      <div className="chat-drawer-body">
        {chips}
        <div className="chat-dialog">
          <Conversation aria-label="Test conversation">
            <ConversationContent>
              {turns.length === 0 && (
                <div className="chat-intro">
                  <MessageCircle size={22} aria-hidden="true" />
                  <p>{intro}</p>
                </div>
              )}
              {turns.map((turn, index) => (
                <Message key={index} from={turn.role}>
                  <MessageContent>
                    <MessageResponse>{turn.text}</MessageResponse>
                    <Grounding turn={turn} />
                  </MessageContent>
                </Message>
              ))}
              {thinkingRow}
            </ConversationContent>
            <ConversationScrollButton aria-label="Scroll to latest message" />
          </Conversation>
          <div className="chat-composer">
            <PromptInput onSubmit={({ text: submitted }) => ask(submitted)}>
              <PromptInputBody>
                <PromptInputTextarea
                  ref={input}
                  value={text}
                  onChange={(event) => setText(event.target.value)}
                  placeholder="Ask about a connection…"
                  aria-label="Ask about the chart"
                />
              </PromptInputBody>
              <PromptInputFooter>
                <span>{note}</span>
                <PromptInputSubmit disabled={!text.trim() || thinking} />
              </PromptInputFooter>
            </PromptInput>
          </div>
        </div>
        {liveLink}
      </div>
    );
  return (
    <div className="chat-workspace chat-demo-workspace">
      <section className="chat-main">
        <div className="chat-main-heading">
          <div>
            <Link to="/atlas/$id" params={{ id: diseaseId }} className="back-link">
              <ArrowLeft size={14} /> Back to chart
            </Link>
            <h1>{useLocalApi ? "Atlas assistant" : "Chart assistant · test"}</h1>
            <p>
              {condition?.label ?? "Chart"} ·{" "}
              {useLocalApi ? "answers from cited atlas records" : "sample answers only"}
            </p>
          </div>
          {useLocalApi ? null : (
            <Link to="/chat" search={{ diseaseId, demo: false }} className="chat-live-link">
              Use live assistant →
            </Link>
          )}
        </div>
        <div className="chat-dialog">
          <Conversation aria-label="Test conversation">
            <ConversationContent>
              {turns.length === 0 && (
                <div className="chat-intro">
                  <MessageCircle size={24} aria-hidden="true" />
                  <h2>Ask about this chart</h2>
                  <p>{intro}</p>
                </div>
              )}
              {turns.map((turn, index) => (
                <Message key={index} from={turn.role}>
                  <MessageContent>
                    <MessageResponse>{turn.text}</MessageResponse>
                    <Grounding turn={turn} />
                  </MessageContent>
                </Message>
              ))}
              {thinkingRow}
            </ConversationContent>
            <ConversationScrollButton aria-label="Scroll to latest message" />
          </Conversation>
          <div className="chat-suggestions" aria-label="Sample questions">
            {suggestions.map((suggestion) => (
              <Button
                key={suggestion}
                type="button"
                variant="outline"
                onClick={() => ask(suggestion)}
              >
                {suggestion}
              </Button>
            ))}
          </div>
          <div className="chat-composer">
            <PromptInput onSubmit={({ text: submitted }) => ask(submitted)}>
              <PromptInputBody>
                <PromptInputTextarea
                  ref={input}
                  value={text}
                  onChange={(event) => setText(event.target.value)}
                  placeholder="Ask about a connection…"
                  aria-label="Ask about the test chart"
                />
              </PromptInputBody>
              <PromptInputFooter>
                <span>{note}</span>
                <PromptInputSubmit disabled={!text.trim() || thinking} />
              </PromptInputFooter>
            </PromptInput>
          </div>
        </div>
      </section>
    </div>
  );
}
