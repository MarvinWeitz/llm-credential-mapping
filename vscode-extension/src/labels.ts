// Which workspace folder's labels apply to a chat request, how they are shown, and a short cache of the CLI's answers.
import type { CliLabels } from "./cli";

export const CACHE_MILLISECONDS = 30000;

export type Resolution = { folder: string; labels?: CliLabels; error?: Error };

export type Choice =
  | { kind: "folder"; folder: string; labels: CliLabels }
  | { kind: "choose"; folders: Resolution[] }
  | { kind: "none" };

/** Resolve each folder's labels at most every 30 s, so `repo bind` and rule changes are picked up quickly. */
export class LabelCache {
  private readonly entries = new Map<string, { at: number; value: Promise<CliLabels> }>();

  constructor(private readonly resolve: (folder: string) => Promise<CliLabels>, private readonly now: () => number = Date.now) {}

  get(folder: string): Promise<CliLabels> {
    const entry = this.entries.get(folder);
    if (entry && this.now() - entry.at < CACHE_MILLISECONDS) return entry.value;
    const value = this.resolve(folder);
    value.catch(() => this.entries.delete(folder));
    this.entries.set(folder, { at: this.now(), value });
    return value;
  }

  clear(): void {
    this.entries.clear();
  }
}

/** Folders are equivalent when their labels have the same values (their repositories may differ). */
const valuesKey = (labels: CliLabels) => JSON.stringify(labels.labels.map((label) => [label.key, label.value]));

/**
 * The folder whose labels a request uses: the only folder; the active editor's folder; the folder chosen
 * earlier; or, when every folder has the same label values, the first one. Otherwise the user must choose.
 */
export async function choose(
  folders: readonly string[], active: string | undefined, chosen: string | undefined, cache: LabelCache,
): Promise<Choice> {
  if (!folders.length) return { kind: "none" };
  const single = folders.length === 1 ? folders[0] : active && folders.includes(active) ? active
    : chosen && folders.includes(chosen) ? chosen : undefined;
  if (single !== undefined) return { kind: "folder", folder: single, labels: await cache.get(single) };
  const resolved: Resolution[] = await Promise.all(folders.map(async (folder) => {
    try {
      return { folder, labels: await cache.get(folder) };
    } catch (error) {
      return { folder, error: error as Error };
    }
  }));
  const keys = new Set(resolved.map((entry) => entry.labels && valuesKey(entry.labels)));
  if (resolved.every((entry) => entry.labels) && keys.size === 1) {
    return { kind: "folder", folder: resolved[0].folder, labels: resolved[0].labels! };
  }
  return { kind: "choose", folders: resolved };
}

/** Text shown where VS Code renders `$(name)` as an icon (status bar, quick pick), escaped so it shows as typed. */
export const escapeIcons = (text: string) => text.replaceAll("$(", "\\$(");

/**
 * The status bar text (icons escaped): the values of all labels with a value, in order, by their names, joined by " · ", then the
 * missing required ones; `warning` while nothing may be sent. "no labels" when none are defined.
 */
export function summary(labels: CliLabels): { text: string; warning: boolean } {
  if (!labels.labels.length) return { text: "no labels", warning: !labels.complete };
  const parts = labels.labels.flatMap((label) => (label.value === null ? [] : [escapeIcons(label.name ?? label.value)]));
  if (labels.missing.length) parts.push(`missing ${labels.missing.map(escapeIcons).join(", ")}`);
  else if (!labels.complete) parts.push("labels unusable");
  return { text: parts.join(" · ") || "no values", warning: !labels.complete };
}

/** The tooltip: each label with its value, description and source, then what blocks requests and the notices. */
export function tooltip(labels: CliLabels, folder: string): string {
  const lines = [`Labels of Copilot Chat requests with vkm models (${folder})`];
  for (const label of labels.labels) {
    const value = label.value === null ? (label.required ? "no value (required)" : "no value")
      : label.name && label.name !== label.value ? `${label.name} (${label.value})` : label.value;
    lines.push(`${label.key}: ${value}${label.description ? ` · ${label.description}` : ""}`);
    if (label.source) lines.push(`    ${label.source}: ${label.detail ?? ""}`);
  }
  for (const ignored of labels.ignored ?? []) lines.push(`Ignored ${ignored.key} from ${ignored.detail}: ${ignored.reason}`);
  if (labels.message) lines.push(labels.message);
  lines.push(...(labels.notices ?? []));
  return lines.join("\n");
}
