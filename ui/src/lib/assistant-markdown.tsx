import type { ReactNode } from "react";

// Light Markdown for assistant replies: paragraphs, bullet and numbered lists,
// **bold**, *italic*, `code`. Everything is emitted as React text nodes, so no
// HTML in a reply can reach the DOM. Underscore italics are deliberately not
// supported: labware names such as `opentrons_96_tiprack_300ul` are full of them.

export type MarkdownBlock =
  | { kind: "p"; text: string }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "h"; text: string };

const BULLET = /^\s*[-*•]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const HEADING = /^#{1,6}\s+(.*)$/;
const INLINE = /\*\*([^\n]+?)\*\*|`([^`\n]+)`|(^|[^\w*])\*([^\s*][^*\n]*?)\*(?![\w*])/g;

export function parseBlocks(text: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  let open: MarkdownBlock | null = null;
  for (const raw of text.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trimEnd();
    if (!line.trim()) { open = null; continue; }
    const bullet = BULLET.exec(line);
    const numbered = NUMBERED.exec(line);
    const heading = HEADING.exec(line);
    if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      const item = (bullet ?? numbered)![1];
      if (open && open.kind === kind) open.items.push(item);
      else { open = { kind, items: [item] }; blocks.push(open); }
    } else if (heading) {
      blocks.push({ kind: "h", text: heading[1] });
      open = null;
    } else if (open && open.kind === "p") {
      open.text += "\n" + line.trim();
    } else {
      open = { kind: "p", text: line.trim() };
      blocks.push(open);
    }
  }
  return blocks;
}

export function inlineNodes(text: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let key = 0;
  for (const match of text.matchAll(INLINE)) {
    const start = match.index ?? 0;
    if (start > last) nodes.push(text.slice(last, start));
    if (match[1] !== undefined) {
      nodes.push(<strong key={key++} className="font-semibold">{inlineNodes(match[1])}</strong>);
    } else if (match[2] !== undefined) {
      nodes.push(
        <code key={key++} className="rounded bg-black/10 px-1 font-mono text-[12px] dark:bg-white/10">
          {match[2]}
        </code>,
      );
    } else {
      if (match[3]) nodes.push(match[3]);
      nodes.push(<em key={key++}>{match[4]}</em>);
    }
    last = start + match[0].length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

export function AssistantMarkdown({ text }: { text: string }) {
  const blocks = parseBlocks(text);
  if (blocks.length === 0) return null;
  return (
    <div className="space-y-1.5 break-words">
      {blocks.map((block, i) => {
        if (block.kind === "h") return <p key={i} className="font-semibold">{inlineNodes(block.text)}</p>;
        if (block.kind === "p") return <p key={i} className="whitespace-pre-wrap">{inlineNodes(block.text)}</p>;
        const Tag = block.kind;
        return (
          <Tag key={i} className={`${block.kind === "ul" ? "list-disc" : "list-decimal"} space-y-0.5 pl-4`}>
            {block.items.map((item, j) => <li key={j}>{inlineNodes(item)}</li>)}
          </Tag>
        );
      })}
    </div>
  );
}
